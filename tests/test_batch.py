from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import FakeProton, FakeStore, proton_node

from migrator.hashing import hash_file
from migrator.phases import batch
from migrator.phases.base import PhaseContext, PhaseError


def _ctx(state_context):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(
        start_epoch=1,
        hour_utc=0,
        weekday=0,
        budget_minutes=165,
        host="t",
        reconcile=False,
    )
    phase_run_id = state.start_phase(40, "40_batches", apply=True, inputs={})
    return PhaseContext(cfg, paths, state, logger, True, phase_run_id, run_id, runtime)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """batch.now is the round-trip's budget clock; pin it inside the budget by default."""
    monkeypatch.setattr(batch, "now", lambda: 1.0)


def _batch(ctx, files: dict[str, bytes]) -> int:
    """files: display path -> content. Inserts a PLANNED batch with matching hashes."""
    with ctx.state.connection:
        cursor = ctx.state.connection.execute(
            "INSERT INTO batches(run_id, number, bytes, file_count, status) VALUES (?, 1, ?, ?, 'PLANNED')",
            (ctx.run_id, sum(len(v) for v in files.values()), len(files)),
        )
        batch_id = int(cursor.lastrowid)
        for display, content in files.items():
            tmp = ctx.paths.root / "tmp.bin"
            tmp.write_bytes(content)
            hashes = hash_file(tmp)
            ctx.state.connection.execute(
                """INSERT INTO batch_items(batch_id, path_lower, path_display, size, content_hash, status)
                   VALUES (?, ?, ?, ?, ?, 'PLANNED')""",
                (
                    batch_id,
                    display.lower(),
                    display,
                    len(content),
                    hashes.dropbox_content_hash,
                ),
            )
    return batch_id


class FakeRclone:
    """Writes the listed files at their lowercase paths, like rclone copying by path_lower."""

    def __init__(self, source: dict[str, bytes], missing=()):
        self.source = {k.lower(): v for k, v in source.items()}
        self.missing = {m.lower() for m in missing}
        self.lists = []

    def copy_files_from(self, list_file: Path, target: Path, log_path: Path) -> int:
        names = list_file.read_text(encoding="utf-8").splitlines()
        self.lists.append(names)
        code = 0
        for name in names:
            key = "/" + name
            if key in self.missing:
                code = 4
                continue
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.source[key])
        log_path.write_text("", encoding="utf-8")
        return code


def test_fetch_moves_files_to_display_paths_and_marks_vanished(state_context):
    ctx = _ctx(state_context)
    files = {"/Docs/Réport.txt": b"report", "/Docs/gone.txt": b"x"}
    batch_id = _batch(ctx, files)
    rclone = FakeRclone(files, missing=["/Docs/gone.txt"])
    counts = batch.fetch(ctx, rclone, batch_id)
    assert counts == {"fetched": 1, "vanished": 1}
    assert rclone.lists == [["docs/gone.txt", "docs/réport.txt"]]
    assert (ctx.paths.staging / "Docs" / "Réport.txt").read_bytes() == b"report"
    assert not (ctx.paths.staging / "docs").exists()
    statuses = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert statuses == {"/docs/réport.txt": "FETCHED", "/docs/gone.txt": "VANISHED"}


def test_fetch_refuses_to_rerun_once_items_have_advanced(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"hello"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    with pytest.raises(PhaseError, match="past fetch"):
        batch.fetch(ctx, FakeRclone(files), batch_id)
    assert (ctx.paths.staging / "a.txt").read_bytes() == b"hello"


def test_verify_records_hashes_and_skips_a_mismatch(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"hello", "/Docs/b.txt": b"world"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    (ctx.paths.staging / "Docs" / "b.txt").write_bytes(b"edited mid-run")
    counts = batch.verify(ctx, batch_id)
    assert counts == {"verified": 1, "bytes": 5, "hash_mismatch": 1}
    rows = {r["path_lower"]: r for r in batch.items(ctx, batch_id)}
    assert (
        rows["/a.txt"]["status"] == "VERIFIED"
        and rows["/a.txt"]["sha1"] == hashlib.sha1(b"hello").hexdigest()
    )
    assert rows["/docs/b.txt"]["status"] == "HASH_MISMATCH"
    assert not (
        ctx.paths.staging / "Docs"
    ).exists()  # wrong bytes never reach the upload


def test_verify_fails_batch_when_every_file_mismatches(state_context):
    ctx = _ctx(state_context)
    batch_id = _batch(ctx, {"/a.txt": b"hello"})
    batch.fetch(ctx, FakeRclone({"/a.txt": b"hello"}), batch_id)
    (ctx.paths.staging / "a.txt").write_bytes(b"tampered")
    with pytest.raises(PhaseError, match="content hash"):
        batch.verify(ctx, batch_id)


def test_upload_passes_top_level_children(state_context):
    ctx = _ctx(state_context)
    batch_id = _batch(ctx, {"/Docs/a.txt": b"aa", "/b.txt": b"b"})
    batch.fetch(ctx, FakeRclone({"/Docs/a.txt": b"aa", "/b.txt": b"b"}), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton({}, {})
    assert batch.upload(ctx, proton, batch_id) == {
        "uploaded_files": 2,
        "uploaded_bytes": 3,
    }
    sources, destination = proton.uploads[0]
    assert sources == [
        str(ctx.paths.staging / "Docs"),
        str(ctx.paths.staging / "b.txt"),
    ]
    assert destination == "/my-files/Dropbox"


def test_upload_stores_the_cli_report_as_an_artifact(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"aa"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    batch.upload(ctx, FakeProton({}, {}), batch_id)
    report = ctx.phase_dir("40_batches") / f"upload-{batch_id}.json"
    assert report.read_text(encoding="utf-8") == '{"ok":true}'
    row = ctx.state.connection.execute(
        "SELECT role, relative_path FROM artifacts WHERE phase_run_id=?",
        (ctx.phase_run_id,),
    ).fetchone()
    assert row["role"] == "upload_report"
    assert row["relative_path"].endswith(f"upload-{batch_id}.json")


def test_confirm_matches_name_size_sha1_and_records_uid(state_context):
    ctx = _ctx(state_context)
    files = {"/Docs/a.txt": b"aa", "/Docs/bad.txt": b"bb", "/Docs/missing.txt": b"cc"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    sha_a = hashlib.sha1(b"aa").hexdigest()
    proton = FakeProton(
        {
            "/my-files/Dropbox/Docs": [
                proton_node("u-a", "a.txt", 2, sha_a),
                proton_node("u-bad", "bad.txt", 2, "0000"),
            ]
        },
        {},
    )
    counts = batch.confirm(ctx, proton, batch_id)
    assert counts == {"confirmed": 1, "confirm_failed": 2}
    rows = {r["path_lower"]: r for r in batch.items(ctx, batch_id)}
    assert rows["/docs/a.txt"]["status"] == "CONFIRMED"
    assert rows["/docs/a.txt"]["proton_uid"] == "u-a"
    assert rows["/docs/a.txt"]["cli_path"] == "/my-files/Dropbox/Docs/a.txt"
    assert rows["/docs/bad.txt"]["status"] == "CONFIRM_FAILED"
    assert json.loads(rows["/docs/bad.txt"]["details_json"])["reason"] == "sha1"
    assert json.loads(rows["/docs/missing.txt"]["details_json"])["reason"] == "absent"


def test_confirm_uses_uid_path_for_duplicate_names_and_listing_failure(state_context):
    ctx = _ctx(state_context)
    files = {"/D/x.txt": b"x", "/E/y.txt": b"y"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    sha_x = hashlib.sha1(b"x").hexdigest()
    proton = FakeProton(
        {
            "/my-files/Dropbox/D": [
                proton_node("u1", "x.txt", 1, sha_x),
                proton_node("u2", "x.txt", 9, "zz"),
            ]
        },
        {},
        fail_list=["/my-files/Dropbox/E"],
    )
    counts = batch.confirm(ctx, proton, batch_id)
    rows = {r["path_lower"]: r for r in batch.items(ctx, batch_id)}
    assert (
        rows["/d/x.txt"]["status"] == "CONFIRMED"
        and rows["/d/x.txt"]["cli_path"] == "/my-files/Dropbox/D/u1"
    )
    assert rows["/e/y.txt"]["status"] == "CONFIRM_FAILED"
    assert counts == {"confirmed": 1, "confirm_failed": 1}


def test_roundtrip_compares_bytes(state_context, monkeypatch):
    monkeypatch.setattr(batch, "ROUNDTRIP_PROGRESS_EVERY", 2)
    ctx = _ctx(state_context)
    files = {"/a.txt": b"same", "/b.txt": b"orig"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton(
        {
            "/my-files/Dropbox": [
                proton_node("ua", "a.txt", 4, hashlib.sha1(b"same").hexdigest()),
                proton_node("ub", "b.txt", 4, hashlib.sha1(b"orig").hexdigest()),
            ]
        },
        {"/my-files/Dropbox/a.txt": b"same", "/my-files/Dropbox/b.txt": b"diff"},
    )
    batch.confirm(ctx, proton, batch_id)
    counts = batch.roundtrip(ctx, proton, batch_id)
    assert counts == {
        "roundtrip_ok": 1,
        "roundtrip_mismatch": 1,
        "roundtrip_bytes": 8,
        "deferred": 0,
    }
    rows = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert rows == {"/a.txt": "ROUNDTRIP_OK", "/b.txt": "ROUNDTRIP_MISMATCH"}
    assert not any(ctx.paths.roundtrip.iterdir())
    progress = ctx.state.connection.execute(
        "SELECT message FROM events WHERE operation='roundtrip' AND message LIKE 'round-trip progress%'"
    ).fetchall()
    assert [r["message"] for r in progress] == ["round-trip progress: 1 of 2 files"]


def test_roundtrip_defers_the_rest_when_the_run_budget_runs_out(
    state_context, monkeypatch, plain_crypt
):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"one", "/b.txt": b"two"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton(
        {
            "/my-files/Dropbox": [
                proton_node("ua", "a.txt", 3, hashlib.sha1(b"one").hexdigest()),
                proton_node("ub", "b.txt", 3, hashlib.sha1(b"two").hexdigest()),
            ]
        },
        {"/my-files/Dropbox/a.txt": b"one", "/my-files/Dropbox/b.txt": b"two"},
    )
    batch.confirm(ctx, proton, batch_id)
    # start_epoch 1 + 165 min - the 10 min margin: the second file starts past the line.
    ticks = iter([1.0, 20_000.0])
    monkeypatch.setattr(batch, "now", lambda: next(ticks))
    counts = batch.roundtrip(ctx, proton, batch_id)
    assert counts == {
        "roundtrip_ok": 1,
        "roundtrip_mismatch": 0,
        "roundtrip_bytes": 3,
        "deferred": 1,
    }
    assert proton.downloaded == ["/my-files/Dropbox/a.txt"]
    statuses = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert statuses == {"/a.txt": "ROUNDTRIP_OK", "/b.txt": "CONFIRMED"}
    assert batch.checkpoint(ctx, FakeStore(), batch_id) == {
        "checkpointed": 1,
        "failed": 0,
    }
    mirrored = ctx.state.connection.execute(
        "SELECT path_lower FROM mirror_objects"
    ).fetchall()
    assert [r["path_lower"] for r in mirrored] == ["/a.txt"]


def test_checkpoint_merges_only_verified_rows_and_pushes(state_context, plain_crypt):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"same", "/b.txt": b"orig"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton(
        {
            "/my-files/Dropbox": [
                proton_node("ua", "a.txt", 4, hashlib.sha1(b"same").hexdigest()),
                proton_node("ub", "b.txt", 4, hashlib.sha1(b"orig").hexdigest()),
            ]
        },
        {"/my-files/Dropbox/a.txt": b"same", "/my-files/Dropbox/b.txt": b"diff"},
    )
    batch.confirm(ctx, proton, batch_id)
    batch.roundtrip(ctx, proton, batch_id)
    store = FakeStore()
    counts = batch.checkpoint(ctx, store, batch_id)
    assert counts == {"checkpointed": 1, "failed": 1}
    mirrored = ctx.state.connection.execute(
        "SELECT path_lower, proton_uid, run_id FROM mirror_objects"
    ).fetchall()
    assert [tuple(r) for r in mirrored] == [("/a.txt", "ua", ctx.run_id)]
    assert (
        ctx.state.connection.execute("SELECT status FROM batches").fetchone()[0]
        == "FAILED"
    )
    assert sorted(store.objects) == [
        ".state/history/1-1.sqlite.xz.age",
        ".state/state.sqlite.xz.age",
    ]
    assert not any(ctx.paths.staging.iterdir())
