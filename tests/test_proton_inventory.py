from __future__ import annotations

import json
import subprocess

import pytest

from migrator.providers.proton_cli import (
    ProtonCLIError,
    ProtonCLIProvider,
    escape_component,
    unwrap,
)


class ProtonRunner:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[-1] == "version":
            return subprocess.CompletedProcess(
                argv, 0, "Proton Drive CLI cli-drive@0.8.0+test\n", ""
            )
        path = argv[-1]
        if path == self.root:
            payload = [
                {
                    "uid": "uid-1",
                    "name": {"ok": True, "value": "Duplicate"},
                    "type": "folder",
                },
                {
                    "uid": "uid-2",
                    "name": {"ok": True, "value": "Duplicate"},
                    "type": "folder",
                },
            ]
        else:
            payload = []
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def test_proton_duplicate_names_are_traversed_by_uid(state_context):
    cfg, _, state, logger, _ = state_context
    runner = ProtonRunner(cfg.proton.destination)
    provider = ProtonCLIProvider(cfg, state, logger, run=runner, sleep=lambda _: None)
    snapshot = provider.inventory("test", "80_proton_inventory")
    status = state.connection.execute(
        "SELECT status FROM proton_snapshots WHERE id=?", (snapshot,)
    ).fetchone()["status"]
    assert status == "COMPLETE"
    paths = [call[-1] for call in runner.calls if "list" in call]
    assert cfg.proton.destination + "/uid-1" in paths
    assert cfg.proton.destination + "/uid-2" in paths
    duplicates = state.connection.execute(
        """
        SELECT COUNT(*) AS count FROM proton_nodes
        WHERE snapshot_id=? AND relative_path='Duplicate'
        """,
        (snapshot,),
    ).fetchone()["count"]
    assert duplicates == 2


def test_verification_inventory_does_not_reuse_complete_snapshot(state_context):
    cfg, _, state, logger, _ = state_context
    runner = ProtonRunner(cfg.proton.destination)
    provider = ProtonCLIProvider(cfg, state, logger, run=runner, sleep=lambda _: None)
    first = provider.inventory("verification", "110_destination_verification")
    second = provider.inventory(
        "verification",
        "110_destination_verification",
        reuse_complete=False,
    )
    assert second != first


def test_incomplete_upload_inventory_resumes_existing_folder_queue(state_context):
    cfg, _, state, logger, _ = state_context
    child_path = cfg.proton.destination + "/Folder"

    def interrupted(argv, **kwargs):
        if argv[-1] == "version":
            return subprocess.CompletedProcess(argv, 0, "cli-drive@0.8.0\n", "")
        if argv[-1] == cfg.proton.destination:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps([{"uid": "uid-folder", "name": "Folder", "type": "folder"}]),
                "",
            )
        return subprocess.CompletedProcess(argv, 1, "", "synthetic interruption")

    provider = ProtonCLIProvider(
        cfg, state, logger, run=interrupted, sleep=lambda _: None
    )
    with pytest.raises(ProtonCLIError, match="exhausted retries"):
        provider.inventory("upload_apply", "100_upload", reuse_complete=False)

    running = state.connection.execute(
        """
        SELECT id, status FROM proton_snapshots
        WHERE purpose='upload_apply' ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    assert running["status"] == "RUNNING"
    pending = state.connection.execute(
        """
        SELECT status FROM proton_folders
        WHERE snapshot_id=? AND uid='uid-folder'
        """,
        (running["id"],),
    ).fetchone()
    assert pending["status"] == "PENDING"

    calls = []

    def resumed(argv, **kwargs):
        calls.append(argv)
        if argv[-1] == "version":
            return subprocess.CompletedProcess(argv, 0, "cli-drive@0.8.0\n", "")
        assert argv[-1] == child_path
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {
                        "uid": "uid-file",
                        "name": "file.txt",
                        "type": "file",
                        "size": 1,
                    }
                ]
            ),
            "",
        )

    provider = ProtonCLIProvider(cfg, state, logger, run=resumed, sleep=lambda _: None)
    resumed_id = provider.inventory("upload_apply", "100_upload", reuse_complete=False)

    assert resumed_id == running["id"]
    assert cfg.proton.destination not in [call[-1] for call in calls if "list" in call]
    assert (
        state.connection.execute(
            "SELECT status FROM proton_snapshots WHERE id=?", (resumed_id,)
        ).fetchone()["status"]
        == "COMPLETE"
    )
    assert (
        state.connection.execute(
            "SELECT COUNT(*) AS count FROM proton_nodes WHERE snapshot_id=?",
            (resumed_id,),
        ).fetchone()["count"]
        == 2
    )


def test_wrapped_fields_and_path_escaping():
    assert unwrap({"ok": True, "value": 42}) == 42
    assert unwrap({"ok": False, "error": "x"}) is None
    assert escape_component(r"a\b/c") == r"a\\b\/c"


def test_proton_list_timeout_fails_closed(state_context):
    cfg, _, state, logger, _ = state_context

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

    provider = ProtonCLIProvider(cfg, state, logger, run=timeout, sleep=lambda _: None)
    with pytest.raises(ProtonCLIError, match="exhausted retries"):
        provider.list_folder(cfg.proton.destination, "test")
