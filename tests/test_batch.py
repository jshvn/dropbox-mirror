from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import FakeDropbox, FakeProton, FakeStore

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


def test_fetch_downloads_to_path_lower_and_marks_vanished(state_context):
    ctx = _ctx(state_context)
    files = {"/Docs/Réport.txt": b"report", "/Docs/gone.txt": b"x"}
    batch_id = _batch(ctx, files)
    dropbox = FakeDropbox(files, missing=["/Docs/gone.txt"])
    counts = batch.fetch(ctx, dropbox, batch_id)
    assert counts == {"fetched": 1, "vanished": 1}
    assert sorted(dropbox.downloaded) == ["/docs/gone.txt", "/docs/réport.txt"]
    assert (ctx.paths.staging / "docs" / "réport.txt").read_bytes() == b"report"
    assert not (ctx.paths.staging / "docs" / "gone.txt").exists()
    statuses = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert statuses == {"/docs/réport.txt": "FETCHED", "/docs/gone.txt": "VANISHED"}


def test_fetch_refuses_to_rerun_once_items_have_advanced(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"hello"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    with pytest.raises(PhaseError, match="past fetch"):
        batch.fetch(ctx, FakeDropbox(files), batch_id)
    assert (ctx.paths.staging / "a.txt").read_bytes() == b"hello"


def test_fetch_reraises_after_the_pool_drains_on_a_download_error(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"hello", "/b.txt": b"world"}
    batch_id = _batch(ctx, files)

    class FlakyDropbox(FakeDropbox):
        def download(self, path_lower: str, target: Path) -> None:
            if path_lower == "/b.txt":
                raise RuntimeError("boom")
            super().download(path_lower, target)

    with pytest.raises(RuntimeError, match="boom"):
        batch.fetch(ctx, FlakyDropbox(files), batch_id)
    statuses = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert statuses == {"/a.txt": "FETCHED", "/b.txt": "PLANNED"}


def test_verify_records_hashes_and_skips_a_mismatch(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"hello", "/Docs/b.txt": b"world"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    (ctx.paths.staging / "docs" / "b.txt").write_bytes(b"edited mid-run")
    counts = batch.verify(ctx, batch_id)
    assert counts == {"verified": 1, "bytes": 5, "hash_mismatch": 1}
    rows = {r["path_lower"]: r for r in batch.items(ctx, batch_id)}
    assert (
        rows["/a.txt"]["status"] == "VERIFIED"
        and rows["/a.txt"]["sha1"] == hashlib.sha1(b"hello").hexdigest()
    )
    assert rows["/docs/b.txt"]["status"] == "HASH_MISMATCH"
    assert not (
        ctx.paths.staging / "docs"
    ).exists()  # wrong bytes never reach the upload


def test_verify_fails_batch_when_every_file_mismatches(state_context):
    ctx = _ctx(state_context)
    batch_id = _batch(ctx, {"/a.txt": b"hello"})
    batch.fetch(ctx, FakeDropbox({"/a.txt": b"hello"}), batch_id)
    (ctx.paths.staging / "a.txt").write_bytes(b"tampered")
    with pytest.raises(PhaseError, match="content hash"):
        batch.verify(ctx, batch_id)


def test_upload_passes_top_level_children(state_context):
    ctx = _ctx(state_context)
    batch_id = _batch(ctx, {"/Docs/a.txt": b"aa", "/b.txt": b"b"})
    batch.fetch(ctx, FakeDropbox({"/Docs/a.txt": b"aa", "/b.txt": b"b"}), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton({})
    assert batch.upload(ctx, proton, batch_id) == {
        "uploaded_files": 2,
        "uploaded_bytes": 3,
    }
    sources, destination = proton.uploads[0]
    assert sources == [
        str(ctx.paths.staging / "b.txt"),
        str(ctx.paths.staging / "docs"),
    ]
    assert destination == "/my-files/Dropbox"


def test_upload_stores_the_cli_report_as_an_artifact(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"aa"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    batch.upload(ctx, FakeProton({}), batch_id)
    report = ctx.phase_dir("40_batches") / f"upload-{batch_id}.json"
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert summary == {
        "transferredItems": 1,
        "transferredBytes": 2,
        "skippedItems": 0,
        "failedItems": 0,
        "failures": [],
    }
    row = ctx.state.connection.execute(
        "SELECT role, relative_path FROM artifacts WHERE phase_run_id=?",
        (ctx.phase_run_id,),
    ).fetchone()
    assert row["role"] == "upload_report"
    assert row["relative_path"].endswith(f"upload-{batch_id}.json")


def test_confirm_is_a_no_op_with_no_verified_rows(state_context):
    ctx = _ctx(state_context)
    batch_id = _batch(ctx, {"/a.txt": b"aa"})
    assert batch.confirm(ctx, batch_id) == {
        "confirmed": 0,
        "skipped_identical": 0,
        "confirm_failed": 0,
    }


def test_confirm_raises_when_the_upload_summary_is_missing(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"aa"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    with pytest.raises(PhaseError, match="upload summary missing"):
        batch.confirm(ctx, batch_id)


def test_confirm_matches_the_verified_count_with_a_merged_folder(state_context):
    ctx = _ctx(state_context)
    files = {"/Docs/a.txt": b"aa", "/Docs/b.txt": b"bb", "/c.txt": b"c"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    batch.upload(ctx, FakeProton({}), batch_id)
    counts = batch.confirm(ctx, batch_id)
    assert counts == {"confirmed": 3, "skipped_identical": 0, "confirm_failed": 0}
    statuses = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert statuses == {
        "/docs/a.txt": "CONFIRMED",
        "/docs/b.txt": "CONFIRMED",
        "/c.txt": "CONFIRMED",
    }


def test_confirm_fails_the_batch_when_the_summary_reports_a_failure(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"aa", "/b.txt": b"bb"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton({})
    proton.fail = {"a.txt"}
    batch.upload(ctx, proton, batch_id)
    counts = batch.confirm(ctx, batch_id)
    assert counts == {"confirmed": 0, "skipped_identical": 0, "confirm_failed": 2}
    statuses = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert statuses == {"/a.txt": "CONFIRM_FAILED", "/b.txt": "CONFIRM_FAILED"}
    reason = json.loads(
        ctx.state.connection.execute(
            "SELECT details_json FROM batch_items WHERE batch_id=? AND path_lower='/a.txt'",
            (batch_id,),
        ).fetchone()[0]
    )
    assert reason["reason"] == "upload summary mismatch" and reason["failed"] == 1


def test_confirm_counts_content_identical_skips(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"aa", "/b.txt": b"bb"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton({})
    proton.skip = {"a.txt"}
    batch.upload(ctx, proton, batch_id)
    counts = batch.confirm(ctx, batch_id)
    assert counts == {"confirmed": 2, "skipped_identical": 1, "confirm_failed": 0}


def test_checkpoint_merges_confirmed_rows_and_pushes(state_context, plain_crypt):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"same"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    batch.upload(ctx, FakeProton({}), batch_id)
    batch.confirm(ctx, batch_id)
    store = FakeStore()
    counts = batch.checkpoint(ctx, store, batch_id)
    assert counts == {"checkpointed": 1, "failed": 0}
    mirrored = ctx.state.connection.execute(
        "SELECT path_lower, proton_uid, run_id FROM mirror_objects"
    ).fetchall()
    assert [tuple(r) for r in mirrored] == [("/a.txt", None, ctx.run_id)]
    assert (
        ctx.state.connection.execute("SELECT status FROM batches").fetchone()[0]
        == "CHECKPOINTED"
    )
    assert sorted(store.objects) == [
        ".state/history/1-1.sqlite.xz.age",
        ".state/state.sqlite.xz.age",
    ]
    assert not any(ctx.paths.staging.iterdir())


def test_checkpoint_fails_the_batch_when_a_row_confirm_failed(
    state_context, plain_crypt
):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"same", "/b.txt": b"orig"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeDropbox(files), batch_id)
    batch.verify(ctx, batch_id)
    with ctx.state.connection:
        ctx.state.connection.execute(
            "UPDATE batch_items SET status='CONFIRMED' WHERE batch_id=? AND path_lower='/a.txt'",
            (batch_id,),
        )
        ctx.state.connection.execute(
            "UPDATE batch_items SET status='CONFIRM_FAILED' WHERE batch_id=? AND path_lower='/b.txt'",
            (batch_id,),
        )
    counts = batch.checkpoint(ctx, FakeStore(), batch_id)
    assert counts == {"checkpointed": 1, "failed": 1}
    mirrored = ctx.state.connection.execute(
        "SELECT path_lower FROM mirror_objects"
    ).fetchall()
    assert [r["path_lower"] for r in mirrored] == ["/a.txt"]
    assert (
        ctx.state.connection.execute("SELECT status FROM batches").fetchone()[0]
        == "FAILED"
    )
    assert not any(ctx.paths.staging.iterdir())
