from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

DROPBOX_BLOCK_SIZE = 4 * 1024 * 1024
STREAM_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FileHashes:
    size: int
    sha256: str
    sha1: str
    dropbox_content_hash: str


def hash_stream(stream: BinaryIO) -> FileHashes:
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1(usedforsecurity=False)
    dropbox = hashlib.sha256()
    block_hash = hashlib.sha256()
    block_size = 0
    size = 0

    while chunk := stream.read(STREAM_SIZE):
        size += len(chunk)
        sha256.update(chunk)
        sha1.update(chunk)
        offset = 0
        while offset < len(chunk):
            take = min(DROPBOX_BLOCK_SIZE - block_size, len(chunk) - offset)
            part = chunk[offset : offset + take]
            block_hash.update(part)
            block_size += take
            offset += take
            if block_size == DROPBOX_BLOCK_SIZE:
                dropbox.update(block_hash.digest())
                block_hash = hashlib.sha256()
                block_size = 0

    if block_size:
        dropbox.update(block_hash.digest())

    return FileHashes(
        size=size,
        sha256=sha256.hexdigest(),
        sha1=sha1.hexdigest(),
        dropbox_content_hash=dropbox.hexdigest(),
    )


def hash_file(path: str | Path) -> FileHashes:
    target = Path(path)
    before = target.stat()
    with target.open("rb") as stream:
        result = hash_stream(stream)
    after = target.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise OSError(f"file changed while hashing: {target}")
    if result.size != after.st_size:
        raise OSError(f"short read while hashing: {target}")
    return result


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
