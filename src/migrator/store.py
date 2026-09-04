from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from .env import Runtime
from .paths import WorkPaths

MISSING_EXITS = {3, 4}  # rclone: directory / file not found


class StoreError(RuntimeError):
    pass


class Store:
    """R2 object store through rclone's S3 backend, configured by RCLONE_CONFIG_R2_* env."""

    def __init__(
        self,
        runtime: Runtime,
        paths: WorkPaths,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if not runtime.r2_bucket:
            raise StoreError("required secret is unset: MIRROR_R2_BUCKET")
        self.bucket = runtime.r2_bucket
        self.paths = paths
        self.run = run

    def _remote(self, key: str) -> str:
        return f"r2:{self.bucket}/{key}"

    def _rclone(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [
            "rclone",
            *args,
            "--config",
            str(self.paths.rclone_config),
            "--retries",
            "5",
        ]
        try:
            return self.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=900,
            )
        except subprocess.TimeoutExpired as exc:
            raise StoreError(f"rclone {args[0]} timed out") from exc

    def get(self, key: str, target: Path) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._rclone("copyto", self._remote(key), str(target))
        if result.returncode in MISSING_EXITS:
            return False
        if result.returncode:
            raise StoreError(
                f"rclone copyto from R2 failed with exit {result.returncode}"
            )
        # rclone exits 0 for an absent S3 object and writes nothing: the file on
        # disk is the evidence, never the exit status.
        return target.is_file()

    def put(self, source: Path, key: str) -> None:
        result = self._rclone("copyto", str(source), self._remote(key))
        if result.returncode:
            raise StoreError(
                f"rclone copyto to R2 failed with exit {result.returncode}"
            )

    def copy(self, source_key: str, target_key: str) -> None:
        result = self._rclone(
            "copyto", self._remote(source_key), self._remote(target_key)
        )
        if result.returncode:
            raise StoreError(
                f"rclone server-side copy failed with exit {result.returncode}"
            )

    def list(self, prefix: str) -> list[str]:
        result = self._rclone("lsjson", self._remote(prefix))
        if result.returncode in MISSING_EXITS:
            return []
        if result.returncode:
            raise StoreError(f"rclone lsjson failed with exit {result.returncode}")
        try:
            entries = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise StoreError("rclone lsjson returned invalid JSON") from exc
        return sorted(
            prefix + str(entry["Path"]) for entry in entries if not entry.get("IsDir")
        )

    def probe(self) -> None:
        """Fail unless the bucket answers a listing. rclone reports a wrong bucket name or a
        rejected credential with the same not-found exit as a missing key, and that must
        never read as an empty mirror."""
        result = self._rclone("lsjson", self._remote(""))
        if result.returncode:
            raise StoreError(
                f"R2 bucket is not reachable: rclone exit {result.returncode}"
            )
