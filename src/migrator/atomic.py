from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO


@contextmanager
def atomic_text_writer(
    path: str | Path, newline: str | None = None
) -> Iterator[TextIO]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    try:
        with partial.open("w", encoding="utf-8", newline=newline) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise


def write_json(path: str | Path, value: Any) -> None:
    with atomic_text_writer(path) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with atomic_text_writer(path) as stream:
        for row in rows:
            stream.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            count += 1
    return count


def write_csv(
    path: str | Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]
) -> int:
    count = 0
    with atomic_text_writer(path, newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def write_files0(path: str | Path, paths: Iterable[str]) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    count = 0
    try:
        with partial.open("wb") as stream:
            for value in paths:
                stream.write(value.encode("utf-8"))
                stream.write(b"\0")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise
    return count
