from __future__ import annotations

import sqlite3

import pytest


def test_run_lifecycle(state_context):
    _, _, state, _, _ = state_context
    with pytest.raises(RuntimeError):
        state.current_run()
    first = state.start_run(
        start_epoch=100,
        hour_utc=2,
        weekday=0,
        budget_minutes=165,
        host="test",
        reconcile=False,
    )
    second = state.start_run(
        start_epoch=200,
        hour_utc=3,
        weekday=1,
        budget_minutes=10,
        host="test",
        reconcile=True,
    )
    rows = {r["id"]: r for r in state.connection.execute("SELECT * FROM runs")}
    assert rows[first]["status"] == "INTERRUPTED"
    assert rows[second]["status"] == "RUNNING"
    assert state.current_run()["id"] == second
    state.update_run(second, planned_batches=3, remaining_batches=1, chain=1)
    assert state.current_run()["remaining_batches"] == 1
    state.finish_run(second, "SUCCESS")
    with pytest.raises(RuntimeError):
        state.current_run()


def test_update_run_rejects_unknown_column(state_context):
    _, _, state, _, _ = state_context
    run_id = state.start_run(
        start_epoch=1,
        hour_utc=0,
        weekday=0,
        budget_minutes=1,
        host="t",
        reconcile=False,
    )
    with pytest.raises(ValueError):
        state.update_run(run_id, nonsense=1)


def test_mirror_totals_and_snapshot(state_context, tmp_path):
    _, _, state, _, _ = state_context
    assert state.mirror_totals() == (0, 0)
    with state.connection:
        state.connection.execute(
            """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1,
               sha256, proton_uid, run_id, mirrored_at) VALUES ('/a.txt','/a.txt',5,'h','s1','s2','u',1,'now')"""
        )
    assert state.mirror_totals() == (1, 5)
    target = tmp_path / "snap.sqlite"
    state.snapshot_to(target)
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT COUNT(*) FROM mirror_objects").fetchone()[0] == 1


def test_phase_attempt_marks_prior_running_attempt_interrupted(state_context):
    _, _, state, _, _ = state_context
    first = state.start_phase(
        10, "10_inventory", apply=False, inputs={}, tool_versions={}
    )
    second = state.start_phase(
        10, "10_inventory", apply=False, inputs={}, tool_versions={}
    )
    assert second != first
    old = state.connection.execute(
        "SELECT status FROM phase_runs WHERE id=?", (first,)
    ).fetchone()
    assert old["status"] == "INTERRUPTED"


def test_schema_has_no_migration_only_tables(state_context):
    _, _, state, _, _ = state_context
    names = {
        r["name"]
        for r in state.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "canonical_objects" not in names and "delta_items" not in names
    assert {
        "runs",
        "mirror_objects",
        "batches",
        "batch_items",
        "deletions",
        "delta_changed",
        "delta_deleted",
        "proton_nodes",
        "dropbox_objects",
        "events",
        "commands",
    } <= names
