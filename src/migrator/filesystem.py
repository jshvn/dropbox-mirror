from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

from .hashing import hash_file


def comparison_key(path: str) -> str:
    normalized = unicodedata.normalize("NFC", path.replace(os.sep, "/").lstrip("/"))
    return "/" + normalized.casefold()


@dataclass(frozen=True)
class LocalObject:
    relative_path: str
    comparison_key: str
    object_type: str
    size: int | None
    mtime_ns: int | None
    atime_ns: int | None
    ctime_ns: int | None = None
    device: int | None = None
    inode: int | None = None
    sha256: str | None = None
    sha1: str | None = None
    dropbox_content_hash: str | None = None
    symlink_target: str | None = None
    stat_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _error(path: Path, root: Path, exc: BaseException) -> LocalObject:
    rel = _relative(path, root) if path != root else "."
    return LocalObject(
        relative_path=rel,
        comparison_key=comparison_key(rel),
        object_type="stat_error",
        size=None,
        mtime_ns=None,
        atime_ns=None,
        ctime_ns=None,
        device=None,
        inode=None,
        stat_error=f"{type(exc).__name__}: {exc}",
    )


def walk_tree(root: str | Path, *, hash_files: bool = True) -> Iterator[LocalObject]:
    base = Path(root)

    def visit(directory: Path) -> Iterator[LocalObject]:
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: os.fsencode(entry.name))
        except OSError as exc:
            yield _error(directory, base, exc)
            return

        for entry in entries:
            path = Path(entry.path)
            rel = _relative(path, base)
            key = comparison_key(rel)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                yield _error(path, base, exc)
                continue

            mode = st.st_mode
            if stat.S_ISLNK(mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    yield _error(path, base, exc)
                    continue
                try:
                    target_stat = path.stat()
                except FileNotFoundError:
                    kind = "broken_symlink"
                except OSError:
                    kind = "broken_symlink"
                else:
                    kind = (
                        "directory_symlink"
                        if stat.S_ISDIR(target_stat.st_mode)
                        else "file_symlink"
                    )
                yield LocalObject(
                    relative_path=rel,
                    comparison_key=key,
                    object_type=kind,
                    size=st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    atime_ns=st.st_atime_ns,
                    ctime_ns=st.st_ctime_ns,
                    device=st.st_dev,
                    inode=st.st_ino,
                    symlink_target=target,
                )
                continue

            if stat.S_ISDIR(mode):
                yield LocalObject(
                    relative_path=rel,
                    comparison_key=key,
                    object_type="directory",
                    size=0,
                    mtime_ns=st.st_mtime_ns,
                    atime_ns=st.st_atime_ns,
                    ctime_ns=st.st_ctime_ns,
                    device=st.st_dev,
                    inode=st.st_ino,
                )
                yield from visit(path)
                continue

            if stat.S_ISREG(mode):
                try:
                    hashes = hash_file(path) if hash_files else None
                    if hash_files:
                        st = path.stat()
                except OSError as exc:
                    yield _error(path, base, exc)
                    continue
                yield LocalObject(
                    relative_path=rel,
                    comparison_key=key,
                    object_type="file",
                    size=hashes.size if hashes else st.st_size,
                    mtime_ns=st.st_mtime_ns,
                    atime_ns=st.st_atime_ns,
                    ctime_ns=st.st_ctime_ns,
                    device=st.st_dev,
                    inode=st.st_ino,
                    sha256=hashes.sha256 if hashes else None,
                    sha1=hashes.sha1 if hashes else None,
                    dropbox_content_hash=(
                        hashes.dropbox_content_hash if hashes else None
                    ),
                )
                continue

            yield LocalObject(
                relative_path=rel,
                comparison_key=key,
                object_type="other",
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                atime_ns=st.st_atime_ns,
                ctime_ns=st.st_ctime_ns,
                device=st.st_dev,
                inode=st.st_ino,
            )

    if not base.is_dir():
        raise FileNotFoundError(f"archive root is not a directory: {base}")
    yield from visit(base)


def lexists(path: str | Path) -> bool:
    return os.path.lexists(Path(path))
