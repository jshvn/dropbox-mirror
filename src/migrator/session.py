from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from . import crypt
from .env import Runtime
from .paths import WorkPaths
from .phases.base import PhaseError
from .store import Store

SESSION_KEY = ".state/session.tar.age"
SESSION_FILES = ("auth-session.json", "clientUid.json")
# Process-global: each phase runs in its own process, so the first write-back in a phase
# always pushes the session once, and later calls push only when the digest has changed.
_last_digest: str | None = None


def _digest(directory: Path) -> str | None:
    auth = directory / SESSION_FILES[0]
    if not auth.is_file():
        return None
    return hashlib.sha256(auth.read_bytes()).hexdigest()


def _bundle(source_dir: Path, paths: WorkPaths, runtime: Runtime, store: Store) -> None:
    tar_path = paths.root / "session.tar"
    with tarfile.open(tar_path, "w") as archive:
        for name in SESSION_FILES:
            member = source_dir / name
            if member.is_file():
                archive.add(member, arcname=name)
    encrypted = paths.root / "session.tar.age"
    try:
        crypt.encrypt(runtime.age_identity, paths.age_key, tar_path, encrypted)
        store.put(encrypted, SESSION_KEY)
    finally:
        tar_path.unlink(missing_ok=True)
        encrypted.unlink(missing_ok=True)


def restore(runtime: Runtime, paths: WorkPaths, store: Store) -> None:
    global _last_digest
    encrypted = paths.root / "session.tar.age"
    if not store.get(SESSION_KEY, encrypted):
        raise PhaseError(
            "no Proton session in R2; run `task session-seal` after a laptop login"
        )
    tar_path = paths.root / "session.tar"
    try:
        crypt.decrypt(runtime.age_identity, paths.age_key, encrypted, tar_path)
        with tarfile.open(tar_path) as archive:
            for member in archive.getmembers():
                if member.name not in SESSION_FILES:
                    raise PhaseError("session bundle holds an unexpected member")
            archive.extractall(paths.session, filter="data")
        for name in SESSION_FILES:
            member = paths.session / name
            if member.exists():
                member.chmod(0o600)
    finally:
        encrypted.unlink(missing_ok=True)
        tar_path.unlink(missing_ok=True)
    _last_digest = _digest(paths.session)


def writeback(runtime: Runtime, paths: WorkPaths, store: Store) -> bool:
    """Push the session to R2 when auth-session.json changed since the last push."""
    global _last_digest
    current = _digest(paths.session)
    if current is None or current == _last_digest:
        return False
    _bundle(paths.session, paths, runtime, store)
    _last_digest = current
    return True


def seal(runtime: Runtime, paths: WorkPaths, store: Store, source_dir: Path) -> None:
    for name in SESSION_FILES:
        if not (source_dir / name).is_file():
            raise PhaseError(f"session directory lacks {name}")
    _bundle(source_dir, paths, runtime, store)
