from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import Config
from ..filesystem import comparison_key
from ..logging import RunLogger, utc_now
from ..paths import WorkPaths
from ..state import State


class RcloneError(RuntimeError):
    pass


ACCEPTED_COPY_EXITS = {0, 3, 4}  # 3/4: a listed path vanished between listing and fetch


def _category(stderr: str, returncode: int) -> str:
    lowered = stderr.casefold()
    if "429" in lowered or "rate limit" in lowered:
        return "RATE_LIMIT"
    for status in (500, 502, 503, 504):
        if str(status) in lowered:
            return f"HTTP_{status}"
    if "timeout" in lowered:
        return "TIMEOUT"
    return f"EXIT_{returncode}"


def dropbox_remote_path(remote: str, root: str, relative: str = "") -> str:
    scope = root.strip("/")
    rel = relative.strip("/")
    path = "/".join(part for part in (scope, rel) if part)
    return f"{remote}:/{path}"


class DropboxRcloneProvider:
    def __init__(
        self,
        cfg: Config,
        paths: WorkPaths,
        state: State,
        logger: RunLogger,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.paths = paths
        self.state = state
        self.logger = logger
        self.run = run
        self.sleep = sleep

    def version(self) -> str:
        try:
            result = self.run(
                [self.cfg.rclone.executable, "version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.cfg.rclone.list_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RcloneError("rclone version command timed out") from exc
        if result.returncode:
            raise RcloneError(f"cannot execute rclone: {result.stderr[-2000:]}")
        first = result.stdout.splitlines()
        return first[0].strip() if first else ""

    def _remote_path(self, relative: str = "") -> str:
        return dropbox_remote_path(
            self.cfg.rclone.remote,
            self.cfg.dropbox.root,
            relative,
        )

    def _common_flags(self) -> list[str]:
        return [
            "--config",
            str(self.paths.rclone_config),
            "--tpslimit",
            str(self.cfg.rclone.tps_limit),
            "--tpslimit-burst",
            str(self.cfg.rclone.tps_burst),
            "--retries",
            str(self.cfg.rclone.retries),
            "--low-level-retries",
            str(self.cfg.rclone.low_level_retries),
        ]

    def _list_folder(self, relative: str) -> list[dict[str, Any]]:
        argv = [
            self.cfg.rclone.executable,
            "lsjson",
            self._remote_path(relative),
            "--hash",
            "--metadata",
            *self._common_flags(),
        ]
        delay = self.cfg.dropbox.initial_backoff_seconds
        attempts = self.cfg.rclone.retries
        for attempt in range(1, attempts + 1):
            command_id = self.state.record_command_start(
                "rclone", "lsjson", argv, attempt
            )
            try:
                result = self.run(
                    argv,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.cfg.rclone.list_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                self.state.record_command_end(command_id, -1, "TIMEOUT")
                self.logger.warning(
                    "10_inventory",
                    "rclone_lsjson",
                    "rclone folder listing timed out and will be retried",
                    object_identifier=relative or "/",
                    retry_count=attempt,
                    provider_category="TIMEOUT",
                )
                if attempt < attempts:
                    self.sleep(delay)
                    delay = min(
                        delay * 2,
                        self.cfg.dropbox.maximum_backoff_seconds,
                    )
                    continue
                raise RcloneError(f"rclone could not list folder {relative!r}")
            category = (
                "SUCCESS"
                if result.returncode == 0
                else _category(result.stderr, result.returncode)
            )
            self.state.record_command_end(command_id, result.returncode, category)
            if result.returncode == 0:
                try:
                    parsed = json.loads(result.stdout)
                except ValueError as exc:
                    raise RcloneError(
                        f"rclone returned invalid JSON for {relative!r}"
                    ) from exc
                if not isinstance(parsed, list):
                    raise RcloneError(f"rclone JSON was not an array for {relative!r}")
                return parsed
            self.logger.warning(
                "10_inventory",
                "rclone_lsjson",
                "rclone folder listing failed and will be retried",
                object_identifier=relative or "/",
                retry_count=attempt,
                provider_category=category,
                raw_error=result.stderr[-4000:],
            )
            if attempt < attempts:
                self.sleep(delay)
                delay = min(delay * 2, self.cfg.dropbox.maximum_backoff_seconds)
        raise RcloneError(f"rclone could not list folder {relative!r}")

    def inventory(
        self, purpose: str = "baseline", *, reuse_complete: bool = True
    ) -> int:
        connection = self.state.connection
        version = self.version()
        row = connection.execute(
            """
            SELECT * FROM rclone_inventory_runs
            WHERE remote=? AND root=? AND purpose=?
              AND status IN ('RUNNING', 'COMPLETE')
            ORDER BY id DESC LIMIT 1
            """,
            (
                self.cfg.rclone.remote,
                self.cfg.dropbox.root,
                purpose,
            ),
        ).fetchone()
        if row and row["status"] == "COMPLETE" and reuse_complete:
            return int(row["id"])
        if row and row["status"] == "COMPLETE":
            row = None
        if row:
            inventory_id = int(row["id"])
            with connection:
                connection.execute(
                    """
                    UPDATE rclone_folders
                    SET status='PENDING'
                    WHERE inventory_id=? AND status='FAILED'
                    """,
                    (inventory_id,),
                )
        else:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO rclone_inventory_runs(
                        started_at, status, remote, root, version, purpose
                    ) VALUES (?, 'RUNNING', ?, ?, ?, ?)
                    """,
                    (
                        utc_now(),
                        self.cfg.rclone.remote,
                        self.cfg.dropbox.root,
                        version,
                        purpose,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO rclone_folders(inventory_id, path, status)
                    VALUES (?, '', 'PENDING')
                    """,
                    (cursor.lastrowid,),
                )
            inventory_id = int(cursor.lastrowid)

        while True:
            folder = connection.execute(
                """
                SELECT * FROM rclone_folders
                WHERE inventory_id=? AND status='PENDING'
                ORDER BY path LIMIT 1
                """,
                (inventory_id,),
            ).fetchone()
            if not folder:
                failed = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM rclone_folders
                    WHERE inventory_id=? AND status!='COMPLETE'
                    """,
                    (inventory_id,),
                ).fetchone()["count"]
                if failed:
                    raise RcloneError("rclone folder queue is incomplete")
                with connection:
                    connection.execute(
                        """
                        UPDATE rclone_inventory_runs
                        SET status='COMPLETE', completed_at=? WHERE id=?
                        """,
                        (utc_now(), inventory_id),
                    )
                break

            parent = str(folder["path"])
            try:
                children = self._list_folder(parent)
            except Exception as exc:
                with connection:
                    connection.execute(
                        """
                        UPDATE rclone_folders SET
                            status='FAILED', attempt_count=attempt_count+1,
                            last_error_category=?
                        WHERE inventory_id=? AND path=?
                        """,
                        (type(exc).__name__, inventory_id, parent),
                    )
                raise

            with connection:
                for child in children:
                    raw_path = str(child.get("Path") or child.get("Name") or "")
                    if not raw_path:
                        raise RcloneError(
                            f"rclone object in {parent!r} had no Path or Name"
                        )
                    path = str(PurePosixPath(parent) / raw_path) if parent else raw_path
                    path = path.lstrip("/")
                    hashes = child.get("Hashes") or child.get("hashes") or {}
                    dropbox_hash = ""
                    if isinstance(hashes, dict):
                        for key, value in hashes.items():
                            if str(key).casefold() in {"dropbox", "dropboxhash"}:
                                dropbox_hash = str(value or "")
                                break
                    object_key = "/" + path
                    connection.execute(
                        """
                        INSERT INTO rclone_objects(
                            inventory_id, object_key, path, comparison_key, name,
                            is_dir, size, object_id, dropbox_hash, modtime, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(inventory_id, object_key) DO UPDATE SET
                            comparison_key=excluded.comparison_key,
                            name=excluded.name,
                            is_dir=excluded.is_dir,
                            size=excluded.size,
                            object_id=excluded.object_id,
                            dropbox_hash=excluded.dropbox_hash,
                            modtime=excluded.modtime,
                            raw_json=excluded.raw_json
                        """,
                        (
                            inventory_id,
                            object_key,
                            path,
                            comparison_key(path),
                            str(child.get("Name") or PurePosixPath(path).name),
                            int(bool(child.get("IsDir"))),
                            child.get("Size"),
                            child.get("ID"),
                            dropbox_hash,
                            child.get("ModTime"),
                            json.dumps(child, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    if child.get("IsDir"):
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO rclone_folders(
                                inventory_id, path, status
                            ) VALUES (?, ?, 'PENDING')
                            """,
                            (inventory_id, path),
                        )
                connection.execute(
                    """
                    UPDATE rclone_folders
                    SET status='COMPLETE', attempt_count=attempt_count+1
                    WHERE inventory_id=? AND path=?
                    """,
                    (inventory_id, parent),
                )
            self.logger.info(
                "10_inventory",
                "rclone_folder",
                "committed rclone folder listing",
                object_identifier=parent or "/",
                entries=len(children),
            )
        return inventory_id

    def copy_files_from(self, list_file: Path, target: Path, log_path: Path) -> int:
        target.mkdir(parents=True, exist_ok=True)
        argv = [
            self.cfg.rclone.executable,
            "copy",
            self._remote_path(),
            str(target),
            "--files-from-raw",
            str(list_file),
            "--no-traverse",
            "--ignore-existing",
            "--transfers",
            str(self.cfg.rclone.transfers),
            "--use-json-log",
            "--log-file",
            str(log_path),
            "--stats",
            "60s",
            *self._common_flags(),
        ]
        command_id = self.state.record_command_start("rclone", "copy", argv, 1)
        try:
            result = self.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.cfg.rclone.transfer_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            self.state.record_command_end(command_id, -1, "TIMEOUT")
            raise RcloneError("rclone copy timed out") from exc
        category = (
            "SUCCESS"
            if result.returncode == 0
            else _category(result.stderr, result.returncode)
        )
        self.state.record_command_end(command_id, result.returncode, category)
        if result.returncode not in ACCEPTED_COPY_EXITS:
            raise RcloneError(f"rclone copy failed ({category})")
        return result.returncode
