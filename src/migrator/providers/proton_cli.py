from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import Config
from ..filesystem import comparison_key
from ..logging import RunLogger, utc_now
from ..state import State


class ProtonCLIError(RuntimeError):
    pass


def unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "ok" in value:
        return value.get("value") if value.get("ok") else None
    return value


def escape_component(name: str) -> str:
    return name.replace("\\", "\\\\").replace("/", "\\/")


def split_parent_path(path: str) -> tuple[str, str]:
    for index in range(len(path) - 1, 0, -1):
        if path[index] != "/":
            continue
        preceding_backslashes = 0
        cursor = index - 1
        while cursor >= 0 and path[cursor] == "\\":
            preceding_backslashes += 1
            cursor -= 1
        if preceding_backslashes % 2 == 0:
            parent = path[:index]
            component = path[index + 1 :]
            if parent and component:
                return parent, component
            break
    raise ProtonCLIError(
        "Proton destination must be a folder below a supported CLI root"
    )


def child_cli_path(parent_cli_path: str, name: str, uid: str, duplicate: bool) -> str:
    component = uid if duplicate else escape_component(name)
    return parent_cli_path.rstrip("/") + "/" + component


def _category(stderr: str, returncode: int) -> str:
    lowered = stderr.casefold()
    if "rate" in lowered or "429" in lowered:
        return "RATE_LIMIT"
    if "auth" in lowered or "login" in lowered:
        return "AUTH"
    if "timeout" in lowered:
        return "TIMEOUT"
    return f"EXIT_{returncode}"


class ProtonCLIProvider:
    def __init__(
        self,
        cfg: Config,
        state: State,
        logger: RunLogger,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        after_call: Callable[[], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.logger = logger
        self.run = run
        self.sleep = sleep
        self.after_call = after_call

    def _after(self) -> None:
        if self.after_call is not None:
            self.after_call()

    def version(self) -> str:
        try:
            result = self.run(
                [self.cfg.proton.executable, "version"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self.cfg.proton.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProtonCLIError("Proton version command timed out") from exc
        if result.returncode:
            raise ProtonCLIError(
                f"cannot execute official Proton Drive CLI: {result.stderr[-2000:]}"
            )
        return result.stdout.strip()

    def _json_command(
        self, operation: str, argv: list[str], *, phase: str, attempts: int
    ) -> Any:
        delay = self.cfg.proton.initial_backoff_seconds
        for attempt in range(1, attempts + 1):
            command_id = self.state.record_command_start(
                "proton", operation, argv, attempt
            )
            try:
                try:
                    result = self.run(
                        argv,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=self.cfg.proton.command_timeout_seconds,
                    )
                finally:
                    self._after()
            except subprocess.TimeoutExpired:
                self.state.record_command_end(command_id, -1, "TIMEOUT")
                self.logger.warning(
                    phase,
                    operation,
                    "Proton CLI operation timed out and will be retried",
                    retry_count=attempt,
                    provider_category="TIMEOUT",
                )
                if attempt < attempts:
                    self.sleep(delay)
                    delay = min(delay * 2, self.cfg.proton.maximum_backoff_seconds)
                    continue
                raise ProtonCLIError(f"Proton {operation} exhausted retries")
            category = (
                "SUCCESS"
                if result.returncode == 0
                else _category(result.stderr, result.returncode)
            )
            self.state.record_command_end(command_id, result.returncode, category)
            if result.returncode == 0:
                try:
                    return json.loads(result.stdout)
                except ValueError as exc:
                    raise ProtonCLIError(
                        f"Proton {operation} returned invalid JSON"
                    ) from exc
            self.logger.warning(
                phase,
                operation,
                "Proton CLI operation failed and will be retried",
                retry_count=attempt,
                provider_category=category,
                raw_error=result.stderr[-4000:],
            )
            if attempt < attempts:
                self.sleep(delay)
                delay = min(delay * 2, self.cfg.proton.maximum_backoff_seconds)
        raise ProtonCLIError(f"Proton {operation} exhausted retries")

    def root_uid(self, phase: str) -> str:
        parent_path, escaped_name = split_parent_path(self.cfg.proton.destination)
        listing = self._json_command(
            "filesystem_list_destination_parent",
            [self.cfg.proton.executable, "filesystem", "list", "-j", parent_path],
            phase=phase,
            attempts=self.cfg.proton.list_max_attempts,
        )
        if not isinstance(listing, list):
            raise ProtonCLIError(
                "Proton destination parent listing was not a JSON array"
            )
        matches = [
            node
            for node in listing
            if isinstance(node, dict)
            and isinstance(unwrap(node.get("name")), str)
            and escape_component(str(unwrap(node["name"]))) == escaped_name
        ]
        if len(matches) != 1 or unwrap(matches[0].get("type")) != "folder":
            raise ProtonCLIError(
                "configured Proton destination did not resolve to exactly one folder"
            )
        observed = str(unwrap(matches[0].get("uid")) or "")
        expected = self.cfg.proton.expected_destination_uid
        matched = observed == expected
        self.state.record_identity_observation(
            "proton",
            phase,
            expected,
            observed,
            matched=matched,
            details={"destination": self.cfg.proton.destination},
        )
        if not matched:
            raise ProtonCLIError(
                "configured Proton destination UID did not exactly match the listing"
            )
        return observed

    def list_folder(self, path: str, phase: str) -> list[dict[str, Any]]:
        result = self._json_command(
            "filesystem_list",
            [self.cfg.proton.executable, "filesystem", "list", "-j", path],
            phase=phase,
            attempts=self.cfg.proton.list_max_attempts,
        )
        if not isinstance(result, list):
            raise ProtonCLIError("Proton filesystem list was not a JSON array")
        return result

    def inventory(
        self, purpose: str, phase: str, *, reuse_complete: bool = True
    ) -> int:
        connection = self.state.connection
        version = self.version()
        row = connection.execute(
            """
            SELECT * FROM proton_snapshots
            WHERE purpose=? AND destination_root=?
              AND status IN ('RUNNING', 'COMPLETE')
            ORDER BY id DESC LIMIT 1
            """,
            (purpose, self.cfg.proton.destination),
        ).fetchone()
        if row and row["status"] == "COMPLETE" and reuse_complete:
            return int(row["id"])
        if row and row["status"] == "COMPLETE":
            row = None
        if row:
            snapshot_id = int(row["id"])
        else:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO proton_snapshots(
                        purpose, started_at, status, destination_root, cli_version
                    ) VALUES (?, ?, 'RUNNING', ?, ?)
                    """,
                    (purpose, utc_now(), self.cfg.proton.destination, version),
                )
                connection.execute(
                    """
                    INSERT INTO proton_folders(
                        snapshot_id, uid, parent_uid, visible_segments_json,
                        cli_path, status
                    ) VALUES (?, '__ROOT__', NULL, '[]', ?, 'PENDING')
                    """,
                    (cursor.lastrowid, self.cfg.proton.destination),
                )
            snapshot_id = int(cursor.lastrowid)

        while True:
            folder = connection.execute(
                """
                SELECT * FROM proton_folders
                WHERE snapshot_id=? AND status='PENDING'
                ORDER BY visible_segments_json, uid LIMIT 1
                """,
                (snapshot_id,),
            ).fetchone()
            if not folder:
                remaining = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM proton_folders
                    WHERE snapshot_id=? AND status!='COMPLETE'
                    """,
                    (snapshot_id,),
                ).fetchone()["count"]
                if remaining:
                    raise ProtonCLIError("Proton folder queue is incomplete")
                with connection:
                    connection.execute(
                        """
                        UPDATE proton_snapshots
                        SET status='COMPLETE', completed_at=? WHERE id=?
                        """,
                        (utc_now(), snapshot_id),
                    )
                break

            cli_path = str(folder["cli_path"])
            parent_uid = str(folder["uid"])
            parent_segments = json.loads(folder["visible_segments_json"])
            try:
                children = self.list_folder(cli_path, phase)
            except Exception as exc:
                with connection:
                    connection.execute(
                        """
                        UPDATE proton_folders SET
                            attempt_count=attempt_count+1,
                            last_error_category=?
                        WHERE snapshot_id=? AND uid=?
                        """,
                        (type(exc).__name__, snapshot_id, parent_uid),
                    )
                raise

            names = [str(unwrap(node.get("name"))) for node in children]
            duplicates = {name for name, count in Counter(names).items() if count > 1}
            with connection:
                for node in children:
                    uid = unwrap(node.get("uid"))
                    name_value = unwrap(node.get("name"))
                    node_type = str(unwrap(node.get("type")) or "")
                    if not uid:
                        raise ProtonCLIError(
                            f"Proton node in {cli_path!r} had no stable UID"
                        )
                    if name_value is None:
                        raise ProtonCLIError(f"Proton node {uid!r} had no visible name")
                    uid = str(uid)
                    name = str(name_value)
                    segments = [*parent_segments, name]
                    relative = "/".join(segments)
                    node_cli_path = child_cli_path(
                        cli_path, name, uid, name in duplicates
                    )
                    active = unwrap(node.get("activeRevision"))
                    claimed_size = claimed_mtime = sha1 = sha1_verified = None
                    if isinstance(active, dict):
                        claimed_size = unwrap(active.get("claimedSize"))
                        claimed_mtime = unwrap(active.get("claimedModificationTime"))
                        digests = unwrap(active.get("claimedDigests"))
                        if isinstance(digests, dict):
                            sha1 = unwrap(digests.get("sha1"))
                            sha1_verified = unwrap(digests.get("sha1Verified"))

                    connection.execute(
                        """
                        INSERT OR REPLACE INTO proton_nodes(
                            snapshot_id, uid, parent_uid, visible_segments_json,
                            relative_path, cli_path, comparison_key, name, node_type,
                            creation_time, modification_time, claimed_size,
                            claimed_modification_time, sha1, sha1_verified, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            uid,
                            parent_uid,
                            json.dumps(segments, ensure_ascii=False),
                            relative,
                            node_cli_path,
                            comparison_key(relative),
                            name,
                            node_type,
                            unwrap(node.get("creationTime")),
                            unwrap(node.get("modificationTime")),
                            claimed_size,
                            claimed_mtime,
                            sha1,
                            (
                                int(bool(sha1_verified))
                                if sha1_verified is not None
                                else None
                            ),
                            json.dumps(node, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    if node_type == "folder":
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO proton_folders(
                                snapshot_id, uid, parent_uid, visible_segments_json,
                                cli_path, status
                            ) VALUES (?, ?, ?, ?, ?, 'PENDING')
                            """,
                            (
                                snapshot_id,
                                uid,
                                parent_uid,
                                json.dumps(segments, ensure_ascii=False),
                                node_cli_path,
                            ),
                        )
                connection.execute(
                    """
                    UPDATE proton_folders SET
                        status='COMPLETE', attempt_count=attempt_count+1
                    WHERE snapshot_id=? AND uid=?
                    """,
                    (snapshot_id, parent_uid),
                )
            self.logger.info(
                phase,
                "proton_folder",
                "committed Proton folder listing",
                object_identifier=cli_path,
                entries=len(children),
            )
        return snapshot_id

    def upload_tree(self, sources: list[Path], destination: str, phase: str) -> str:
        argv = [
            self.cfg.proton.executable,
            "filesystem",
            "upload",
            "-f",
            "create-new-revision",
            "-d",
            "merge",
            "--json",
            "--skip-thumbnails",
            *(str(source) for source in sources),
            destination,
        ]
        return self._mutation("upload", argv, phase, accepted=frozenset({0, 1}))

    def trash(self, cli_paths: list[str], phase: str) -> None:
        if not cli_paths:
            return
        self._mutation(
            "trash",
            [self.cfg.proton.executable, "filesystem", "trash", *cli_paths],
            phase,
        )

    def empty_trash(self, phase: str) -> None:
        self._mutation(
            "empty_trash",
            [self.cfg.proton.executable, "filesystem", "empty-trash"],
            phase,
        )

    def download_file(self, remote_path: str, local_parent: Path, phase: str) -> None:
        local_parent.mkdir(parents=True, exist_ok=True)
        argv = [
            self.cfg.proton.executable,
            "filesystem",
            "download",
            "--file-conflict-strategy",
            "skip",
            "--folder-conflict-strategy",
            "skip",
            remote_path,
            str(local_parent),
        ]
        delay = self.cfg.proton.initial_backoff_seconds
        for attempt in range(1, self.cfg.proton.download_max_attempts + 1):
            command_id = self.state.record_command_start(
                "proton", "download", argv, attempt
            )
            try:
                try:
                    result = self.run(
                        argv,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                        timeout=self.cfg.proton.transfer_timeout_seconds,
                    )
                finally:
                    self._after()
            except subprocess.TimeoutExpired:
                self.state.record_command_end(command_id, -1, "TIMEOUT")
                self.logger.warning(
                    phase,
                    "download",
                    "Proton CLI download timed out and will be retried",
                    retry_count=attempt,
                    provider_category="TIMEOUT",
                )
                if attempt < self.cfg.proton.download_max_attempts:
                    shutil.rmtree(local_parent)
                    local_parent.mkdir(parents=True)
                    self.sleep(delay)
                    delay = min(delay * 2, self.cfg.proton.maximum_backoff_seconds)
                    continue
                raise ProtonCLIError("Proton download exhausted retries")
            category = (
                "SUCCESS"
                if result.returncode == 0
                else _category(result.stderr, result.returncode)
            )
            self.state.record_command_end(command_id, result.returncode, category)
            if result.returncode == 0:
                return
            self.logger.warning(
                phase,
                "download",
                "Proton CLI download failed and will be retried",
                retry_count=attempt,
                provider_category=category,
                raw_error=result.stderr[-4000:],
            )
            if attempt < self.cfg.proton.download_max_attempts:
                shutil.rmtree(local_parent)
                local_parent.mkdir(parents=True)
                self.sleep(delay)
                delay = min(delay * 2, self.cfg.proton.maximum_backoff_seconds)
        raise ProtonCLIError("Proton download exhausted retries")

    def _mutation(
        self,
        operation: str,
        argv: list[str],
        phase: str,
        *,
        accepted: frozenset[int] = frozenset({0}),
    ) -> str:
        command_id = self.state.record_command_start("proton", operation, argv, 1)
        timeout = (
            self.cfg.proton.transfer_timeout_seconds
            if operation == "upload"
            else self.cfg.proton.command_timeout_seconds
        )
        try:
            try:
                result = self.run(
                    argv,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=timeout,
                )
            finally:
                self._after()
        except subprocess.TimeoutExpired as exc:
            self.state.record_command_end(command_id, -1, "TIMEOUT")
            self.logger.error(
                phase,
                operation,
                "official Proton CLI mutation timed out",
                provider_category="TIMEOUT",
            )
            raise ProtonCLIError(f"Proton {operation} timed out") from exc
        category = (
            "SUCCESS"
            if result.returncode == 0
            else _category(result.stderr, result.returncode)
        )
        self.state.record_command_end(command_id, result.returncode, category)
        if result.returncode not in accepted or category == "AUTH":
            self.logger.error(
                phase,
                operation,
                "official Proton CLI mutation failed",
                provider_category=category,
                raw_error=result.stderr[-4000:],
            )
            raise ProtonCLIError(
                f"Proton {operation} failed ({category}): {result.stderr[-4000:]}"
            )
        return result.stdout
