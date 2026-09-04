from __future__ import annotations

import pytest
from conftest import seed_api_inventory

from migrator.phases import p10_inventory
from migrator.phases.base import PhaseContext, PhaseError


def _ctx(state_context, reconcile=False):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(
        start_epoch=1,
        hour_utc=0,
        weekday=0,
        budget_minutes=1,
        host="t",
        reconcile=reconcile,
    )
    phase_run_id = state.start_phase(10, "10_inventory", apply=False, inputs={})
    return PhaseContext(cfg, paths, state, logger, False, phase_run_id, run_id, runtime)


def test_inventory_records_counts_and_run_link(state_context, monkeypatch):
    ctx = _ctx(state_context)
    rows = [
        ("/A/one.txt", 3, "h1", 1, "file"),
        ("/A", None, None, 1, "folder"),
        ("/notes.paper", 0, None, 0, "file"),
    ]
    inventory_id = seed_api_inventory(ctx.state, "run:1", rows)
    monkeypatch.setattr(p10_inventory, "access_token", lambda cfg, runtime: "tok")
    monkeypatch.setattr(
        p10_inventory.DropboxAPIProvider,
        "inventory",
        lambda self, purpose, reuse_complete=True: inventory_id,
    )
    result = p10_inventory.run(ctx)
    assert result.outputs == {
        "inventory_id": inventory_id,
        "files": 1,
        "folders": 1,
        "bytes": 3,
        "non_downloadable": 1,
        "observer": False,
        "unhashed": 0,
        "pruned_inventories": 0,
    }
    assert ctx.state.current_run()["inventory_id"] == inventory_id


def test_unhashed_files_become_non_downloadable(state_context, monkeypatch):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(
        ctx.state,
        "run:1",
        [("/cloud.gdoc", 0, None, 1, "file"), ("/real.txt", 2, "h", 1, "file")],
    )
    monkeypatch.setattr(p10_inventory, "access_token", lambda cfg, runtime: "tok")
    monkeypatch.setattr(
        p10_inventory.DropboxAPIProvider,
        "inventory",
        lambda self, purpose, reuse_complete=True: inventory_id,
    )
    outputs = p10_inventory.run(ctx).outputs
    assert (
        outputs["files"] == 1
        and outputs["non_downloadable"] == 1
        and outputs["unhashed"] == 1
    )


def test_prune_keeps_newest_inventories(state_context):
    _, _, state, _, _ = state_context
    ids = [
        seed_api_inventory(state, f"run:{n}", [("/a.txt", 1, "h", 1, "file")])
        for n in range(4)
    ]
    assert p10_inventory.prune_inventories(state.connection, keep=2) == 2
    left = {
        r["inventory_id"]
        for r in state.connection.execute("SELECT inventory_id FROM dropbox_objects")
    }
    assert left == set(ids[2:])


def test_reconcile_run_gates_on_observer(state_context, monkeypatch):
    ctx = _ctx(state_context, reconcile=True)
    inventory_id = seed_api_inventory(
        ctx.state, "run:1", [("/A/one.txt", 3, "h1", 1, "file")]
    )
    monkeypatch.setattr(p10_inventory, "access_token", lambda cfg, runtime: "tok")
    monkeypatch.setattr(
        p10_inventory.DropboxAPIProvider,
        "inventory",
        lambda self, purpose, reuse_complete=True: inventory_id,
    )
    with ctx.state.connection:
        cursor = ctx.state.connection.execute(
            """INSERT INTO rclone_inventory_runs(started_at, completed_at, status, remote, root, version, purpose)
               VALUES ('now','now','COMPLETE','dropbox','','v','run:1')"""
        )
        rclone_id = int(cursor.lastrowid)
        ctx.state.connection.execute(
            """INSERT INTO rclone_objects(inventory_id, object_key, path, comparison_key, name, is_dir, size,
               dropbox_hash, raw_json) VALUES (?, '/a/one.txt', 'A/one.txt', '/a/one.txt', 'one.txt', 0, 4, 'h1', '{}')""",
            (rclone_id,),
        )
    monkeypatch.setattr(
        p10_inventory.DropboxRcloneProvider,
        "inventory",
        lambda self, purpose, reuse_complete=True: rclone_id,
    )
    with pytest.raises(PhaseError, match="discrepanc"):
        p10_inventory.run(ctx)
