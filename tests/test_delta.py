from __future__ import annotations

import unicodedata

import pytest
from conftest import seed_api_inventory

from migrator.phases import p20_delta
from migrator.phases.base import PhaseContext, PhaseError


def _ctx(state_context):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(
        start_epoch=1,
        hour_utc=0,
        weekday=0,
        budget_minutes=1,
        host="t",
        reconcile=False,
    )
    phase_run_id = state.start_phase(20, "20_delta", apply=False, inputs={})
    return PhaseContext(cfg, paths, state, logger, False, phase_run_id, run_id, runtime)


def _mirror(state, rows):
    with state.connection:
        for path_lower, display, size, content_hash in rows:
            state.connection.execute(
                """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256,
                   proton_uid, run_id, mirrored_at) VALUES (?, ?, ?, ?, 's1', 's2', 'u', 0, 'now')""",
                (path_lower, display, size, content_hash),
            )


def test_changed_and_deleted_rows(state_context):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(
        ctx.state,
        "run:1",
        [
            ("/Docs", None, None, 1, "folder"),
            ("/Docs/same.txt", 3, "h-same", 1, "file"),
            ("/Docs/edited.txt", 4, "h-new", 1, "file"),
            ("/Docs/added.txt", 5, "h-add", 1, "file"),
            ("/Docs/paper.paper", 0, None, 0, "file"),
        ],
    )
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    _mirror(
        ctx.state,
        [
            ("/docs/same.txt", "/Docs/same.txt", 3, "h-same"),
            ("/docs/edited.txt", "/Docs/edited.txt", 4, "h-old"),
            ("/docs/gone.txt", "/Docs/gone.txt", 1, "h-gone"),
        ],
    )
    result = p20_delta.run(ctx)
    changed = {
        r["path_lower"]: r
        for r in ctx.state.connection.execute(
            "SELECT * FROM delta_changed WHERE run_id=?", (ctx.run_id,)
        )
    }
    deleted = [
        r["path_lower"]
        for r in ctx.state.connection.execute(
            "SELECT path_lower FROM delta_deleted WHERE run_id=?", (ctx.run_id,)
        )
    ]
    assert set(changed) == {"/docs/edited.txt", "/docs/added.txt"}
    assert changed["/docs/added.txt"]["path_display"] == "/Docs/added.txt"
    assert deleted == ["/docs/gone.txt"]
    assert result.outputs["changed_files"] == 2 and result.outputs["changed_bytes"] == 9
    assert result.outputs["deleted_files"] == 1


def test_display_path_takes_parent_casing_from_folder_entries_and_nfc(state_context):
    ctx = _ctx(state_context)
    nfd = unicodedata.normalize("NFD", "Café")
    inventory_id = seed_api_inventory(
        ctx.state,
        "run:1",
        [
            ("/Photos", None, None, 1, "folder"),
            (f"/Photos/{nfd}", None, None, 1, "folder"),
            (f"/photos/{nfd}/IMG.jpg", 2, "h", 1, "file"),
        ],
    )
    mapping = p20_delta.display_paths(ctx.state.connection, inventory_id)
    assert mapping[f"/photos/{nfd.lower()}/img.jpg"] == "/Photos/Café/IMG.jpg"
    assert unicodedata.is_normalized("NFC", mapping[f"/photos/{nfd.lower()}/img.jpg"])


def test_listing_floor_refuses_truncated_listing(state_context):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(
        ctx.state, "run:1", [("/a.txt", 1, "h", 1, "file")]
    )
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    _mirror(ctx.state, [(f"/f{i}.txt", f"/f{i}.txt", 1, "h") for i in range(10)])
    with pytest.raises(PhaseError, match="floor"):
        p20_delta.run(ctx)


def test_first_run_has_no_floor(state_context):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(
        ctx.state, "run:1", [("/a.txt", 1, "h", 1, "file")]
    )
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    assert p20_delta.run(ctx).outputs["changed_files"] == 1
