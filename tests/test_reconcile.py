from __future__ import annotations

from conftest import FakeStore, seed_api_inventory

from migrator.filesystem import comparison_key
from migrator.phases import p60_reconcile
from migrator.phases.base import PhaseContext


def _ctx(state_context, reconcile=True, remaining=0):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(
        start_epoch=1,
        hour_utc=0,
        weekday=0,
        budget_minutes=1,
        host="t",
        reconcile=reconcile,
    )
    state.update_run(run_id, planned_batches=0, remaining_batches=remaining)
    phase_run_id = state.start_phase(60, "60_reconcile", apply=True, inputs={})
    return PhaseContext(cfg, paths, state, logger, True, phase_run_id, run_id, runtime)


def _snapshot(state, nodes):
    with state.connection:
        cursor = state.connection.execute(
            """INSERT INTO proton_snapshots(purpose, started_at, completed_at, status, destination_root, cli_version)
               VALUES ('reconcile:1', 'now', 'now', 'COMPLETE', '/my-files/Dropbox', '0.8.0')"""
        )
        snapshot_id = int(cursor.lastrowid)
        for relative, uid, size in nodes:
            state.connection.execute(
                """INSERT INTO proton_nodes(snapshot_id, uid, parent_uid, visible_segments_json, relative_path, cli_path,
                   comparison_key, name, node_type, claimed_size, raw_json)
                   VALUES (?, ?, '__ROOT__', '[]', ?, ?, ?, ?, 'file', ?, '{}')""",
                (
                    snapshot_id,
                    uid,
                    relative,
                    "/my-files/Dropbox/" + relative,
                    comparison_key(relative),
                    relative.rsplit("/", 1)[-1],
                    size,
                ),
            )
    return snapshot_id


def _mirror(state, rows):
    with state.connection:
        for display, size, uid in rows:
            state.connection.execute(
                """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256, proton_uid,
                   run_id, mirrored_at) VALUES (?, ?, ?, 'h', 's', 's', ?, 0, 'now')""",
                (display.lower(), display, size, uid),
            )


def test_reconcile_drops_missing_or_missized_and_trashes_strays(
    state_context, monkeypatch, plain_crypt
):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(
        ctx.state,
        "run:1",
        [("/Keep/ok.txt", 3, "h", 1, "file"), ("/Keep/pending.txt", 7, "h", 1, "file")],
    )
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    _mirror(
        ctx.state,
        [
            ("/Keep/ok.txt", 3, None),
            ("/Keep/lost.txt", 2, "u-lost"),
            ("/Keep/bad.txt", 5, "u-bad"),
        ],
    )
    _snapshot(
        ctx.state, [("Old/x", "u-old", 1)]
    )  # last week's walk; pruned by this run
    snapshot_id = _snapshot(
        ctx.state,
        [
            ("Keep/ok.txt", "u-ok", 3),
            ("Keep/bad.txt", "u-bad", 99),
            ("Keep/pending.txt", "u-p", 7),
            ("Stray/x.bin", "u-stray", 1),
        ],
    )
    trashed = []
    fake = type(
        "P",
        (),
        {
            "root_uid": lambda self, phase: "uid-destination",
            "inventory": lambda self, purpose, phase, reuse_complete=True: snapshot_id,
            "trash": lambda self, paths, phase: trashed.extend(paths),
        },
    )()
    monkeypatch.setattr(p60_reconcile, "ProtonCLIProvider", lambda *a, **k: fake)
    monkeypatch.setattr(p60_reconcile, "Store", lambda runtime, paths: FakeStore())
    monkeypatch.setattr(p60_reconcile.session, "writeback", lambda *a: False)
    result = p60_reconcile.run(ctx)
    assert result.outputs["dropped"] == 2 and result.outputs["strays_trashed"] == 1
    assert result.outputs["uid_refreshed"] == 1
    assert trashed == ["/my-files/Dropbox/Stray/x.bin"]
    left = {
        r["path_lower"]: r["proton_uid"]
        for r in ctx.state.connection.execute("SELECT * FROM mirror_objects")
    }
    assert left == {"/keep/ok.txt": "u-ok"}
    assert (
        ctx.state.connection.execute(
            "SELECT COUNT(*) FROM proton_snapshots"
        ).fetchone()[0]
        == 1
    )


def test_reconcile_skips_when_not_scheduled(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context, reconcile=False)
    assert p60_reconcile.run(ctx).outputs == {"skipped": "not a reconcile run"}


def test_reconcile_skips_while_batches_remain(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context, remaining=1)
    assert p60_reconcile.run(ctx).outputs == {"skipped": "batches remain"}
