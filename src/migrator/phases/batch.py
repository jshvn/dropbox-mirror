from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

from .. import statefile
from ..hashing import hash_file
from ..logging import utc_now
from ..providers.dropbox_api import DropboxNotFound
from ..providers.proton_cli import escape_component, unwrap
from ..store import Store
from .base import PhaseContext, PhaseError

PHASE = "40_batches"


def items(
    ctx: PhaseContext, batch_id: int, status: str | None = None
) -> list[sqlite3.Row]:
    clause = " AND status=?" if status else ""
    params: tuple[Any, ...] = (batch_id, status) if status else (batch_id,)
    return ctx.state.connection.execute(
        f"SELECT * FROM batch_items WHERE batch_id=?{clause} ORDER BY path_lower",
        params,
    ).fetchall()


def _set_item(
    ctx: PhaseContext, batch_id: int, path_lower: str, status: str, **columns: Any
) -> None:
    # ponytail: one commit per row, so a killed step loses at most the row in flight.
    # The ceiling is one fsync per file per step; the upgrade path is to batch the status
    # writes per step and commit once at its end.
    assignments = ", ".join(["status=?", *(f"{name}=?" for name in columns)])
    with ctx.state.connection:
        ctx.state.connection.execute(
            f"UPDATE batch_items SET {assignments} WHERE batch_id=? AND path_lower=?",
            (status, *columns.values(), batch_id, path_lower),
        )


def _details(reason: str, **extra: Any) -> str:
    return json.dumps({"reason": reason, **extra}, sort_keys=True)


def local_path(paths: Any, path_lower: str) -> Path:
    return paths.staging / path_lower.lstrip("/")


def parent_cli_path(destination: str, path_display: str) -> str:
    parts = PurePosixPath(path_display.lstrip("/")).parent.parts
    if not parts:
        return destination.rstrip("/")
    return (
        destination.rstrip("/")
        + "/"
        + "/".join(escape_component(part) for part in parts)
    )


def _clear(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)


def history_label(ctx: PhaseContext) -> str:
    """History objects are keyed by the run's start epoch: unique across runs even after
    a rollback re-issues run ids, and what the spec's `.state/history/<epoch>-...` names."""
    return str(int(ctx.state.current_run()["start_epoch"]))


def _prune_empty_dirs(root: Path) -> None:
    for current, dirs, _files in os.walk(root, topdown=False):
        for name in dirs:
            try:
                os.rmdir(Path(current) / name)
            except OSError:
                pass


def resolve_children(
    proton: Any, parent: str, phase: str
) -> dict[str, list[dict[str, Any]]]:
    """List one Proton folder and group its visible nodes by name. More than one node
    sharing a name is a genuine Proton duplicate: `child_cli_path(parent, name, uid,
    len(by_name[name]) > 1)` is how every caller (trash) builds its path."""
    children = proton.list_folder(parent, phase)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in children:
        by_name[str(unwrap(node.get("name")))].append(node)
    return dict(by_name)


def fetch(ctx: PhaseContext, dropbox: Any, batch_id: int) -> dict[str, int]:
    """One Dropbox API call per file, run through a thread pool; each lands at its
    path_lower under staging. Fetch owns staging outright: once any item in the batch
    has moved past fetch's own PLANNED/VANISHED states, a second call would wipe staging
    out from under files verify/upload already depend on, so it refuses to run rather
    than silently losing them."""
    advanced = [
        r for r in items(ctx, batch_id) if r["status"] not in ("PLANNED", "VANISHED")
    ]
    if advanced:
        raise PhaseError(
            f"batch {batch_id} has {len(advanced)} item(s) past fetch; "
            "fetch runs once per batch and must not re-clear staging underneath them"
        )
    _clear(ctx.paths.staging)
    rows = items(ctx, batch_id, "PLANNED")

    def _download(row: sqlite3.Row) -> None:
        target = local_path(ctx.paths, str(row["path_lower"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        dropbox.download(str(row["path_lower"]), target)

    counts: Counter[str] = Counter()
    error: BaseException | None = None
    with ThreadPoolExecutor(max_workers=ctx.cfg.dropbox.download_workers) as pool:
        futures = [(row, pool.submit(_download, row)) for row in rows]
        for row, future in futures:
            try:
                future.result()
            except DropboxNotFound:
                _set_item(
                    ctx,
                    batch_id,
                    row["path_lower"],
                    "VANISHED",
                    details_json=_details("vanished"),
                )
                counts["vanished"] += 1
            except Exception as exc:  # noqa: BLE001 - pool drains fully; batch fails after
                if error is None:
                    error = exc
            else:
                _set_item(ctx, batch_id, row["path_lower"], "FETCHED")
                counts["fetched"] += 1
    if error is not None:
        raise error
    _prune_empty_dirs(ctx.paths.staging)
    ctx.logger.info(PHASE, "fetch", "batch fetched", batch=batch_id, **counts)
    return {"fetched": counts["fetched"], "vanished": counts["vanished"]}


def verify(ctx: PhaseContext, batch_id: int) -> dict[str, int]:
    """A mismatch is a file edited between listing and fetch: removed from staging so the
    upload never sees it, counted, never recorded; the next listing catches it. Every
    file mismatching is corruption, not editing, and fails the batch."""
    counts: Counter[str] = Counter()
    rows = items(ctx, batch_id, "FETCHED")
    for row in rows:
        staged = local_path(ctx.paths, str(row["path_lower"]))
        hashes = hash_file(staged)
        if hashes.size != int(row["size"]) or hashes.dropbox_content_hash != str(
            row["content_hash"]
        ):
            staged.unlink()
            _set_item(
                ctx,
                batch_id,
                row["path_lower"],
                "HASH_MISMATCH",
                details_json=_details("content_hash"),
            )
            counts["hash_mismatch"] += 1
            continue
        _set_item(
            ctx,
            batch_id,
            row["path_lower"],
            "VERIFIED",
            sha1=hashes.sha1,
            sha256=hashes.sha256,
        )
        counts["verified"] += 1
        counts["bytes"] += hashes.size
    _prune_empty_dirs(ctx.paths.staging)
    if rows and counts["hash_mismatch"] == len(rows):
        raise PhaseError(
            "content hash mismatch on every staged file; the fetch path is corrupt"
        )
    ctx.logger.info(PHASE, "verify", "batch verified", batch=batch_id, **counts)
    return {
        "verified": counts["verified"],
        "bytes": counts["bytes"],
        "hash_mismatch": counts["hash_mismatch"],
    }


def upload(ctx: PhaseContext, proton: Any, batch_id: int) -> dict[str, int]:
    rows = items(ctx, batch_id, "VERIFIED")
    if not rows:
        return {"uploaded_files": 0, "uploaded_bytes": 0}
    sources = sorted(path for path in ctx.paths.staging.iterdir())
    stdout = proton.upload_tree(sources, ctx.cfg.proton.destination, PHASE)
    # The CLI's own transfer summary is confirm's evidence, so this artifact is kept.
    report = ctx.phase_dir(PHASE) / f"upload-{batch_id}.json"
    report.write_text(stdout or "", encoding="utf-8")
    ctx.state.record_artifact(ctx.phase_run_id, "upload_report", report, ctx.paths.root)
    total = sum(int(r["size"]) for r in rows)
    ctx.logger.info(
        PHASE, "upload", "batch uploaded", batch=batch_id, files=len(rows), bytes=total
    )
    return {"uploaded_files": len(rows), "uploaded_bytes": total}


def _last_summary(report: Path) -> dict[str, Any] | None:
    """The CLI writes progress and its final summary as one JSON object per line; the
    last line carrying `transferredItems` is the summary."""
    if not report.exists():
        return None
    summary: dict[str, Any] | None = None
    for line in report.read_text(encoding="utf-8").splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "transferredItems" in candidate:
            summary = candidate
    return summary


def confirm(ctx: PhaseContext, batch_id: int) -> dict[str, int]:
    """The CLI's own summary is the evidence: no re-listing Proton. A batch confirms
    when it reports no failures and its transferred-plus-skipped count matches every
    verified file plus every directory it landed in."""
    rows = items(ctx, batch_id, "VERIFIED")
    files = len(rows)
    if not files:
        return {"confirmed": 0, "skipped_identical": 0, "confirm_failed": 0}
    summary = _last_summary(ctx.phase_dir(PHASE) / f"upload-{batch_id}.json")
    if summary is None:
        raise PhaseError("upload summary missing")
    folders = sum(1 for p in ctx.paths.staging.rglob("*") if p.is_dir())
    transferred = int(summary.get("transferredItems", 0))
    skipped = int(summary.get("skippedItems", 0))
    failed = int(summary.get("failedItems", 0))
    failures = summary.get("failures") or []
    confirmed = not failed and not failures and transferred + skipped == files + folders
    status = "CONFIRMED" if confirmed else "CONFIRM_FAILED"
    with ctx.state.connection:
        if confirmed:
            ctx.state.connection.execute(
                "UPDATE batch_items SET status=? WHERE batch_id=? AND status='VERIFIED'",
                (status, batch_id),
            )
        else:
            ctx.state.connection.execute(
                "UPDATE batch_items SET status=?, details_json=? WHERE batch_id=? AND status='VERIFIED'",
                (
                    status,
                    _details(
                        "upload summary mismatch",
                        files=files,
                        folders=folders,
                        transferred=transferred,
                        skipped=skipped,
                        failed=failed,
                    ),
                    batch_id,
                ),
            )
    ctx.logger.info(
        PHASE,
        "confirm",
        "batch confirmed by upload summary",
        batch=batch_id,
        confirmed=files if confirmed else 0,
        confirm_failed=0 if confirmed else files,
        skipped_identical=skipped if confirmed else 0,
    )
    if confirmed:
        return {"confirmed": files, "skipped_identical": skipped, "confirm_failed": 0}
    return {"confirmed": 0, "skipped_identical": 0, "confirm_failed": files}


def checkpoint(ctx: PhaseContext, store: Store, batch_id: int) -> dict[str, int]:
    """Merge every CONFIRMED row, then push the state. Always the last step."""
    connection = ctx.state.connection
    good = items(ctx, batch_id, "CONFIRMED")
    now = utc_now()
    with connection:
        connection.executemany(
            """
            INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256,
                                       proton_uid, run_id, mirrored_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path_lower) DO UPDATE SET
                path_display=excluded.path_display, size=excluded.size,
                content_hash=excluded.content_hash, sha1=excluded.sha1, sha256=excluded.sha256,
                proton_uid=excluded.proton_uid, run_id=excluded.run_id, mirrored_at=excluded.mirrored_at
            """,
            [
                (
                    r["path_lower"],
                    r["path_display"],
                    int(r["size"]),
                    r["content_hash"],
                    r["sha1"],
                    r["sha256"],
                    r["proton_uid"],
                    ctx.run_id,
                    now,
                )
                for r in good
            ],
        )
        connection.execute(
            "UPDATE batch_items SET status='CHECKPOINTED' WHERE batch_id=? AND status='CONFIRMED'",
            (batch_id,),
        )
        failed = int(
            connection.execute(
                "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND status='CONFIRM_FAILED'",
                (batch_id,),
            ).fetchone()[0]
        )
        status = "FAILED" if failed else "CHECKPOINTED"
        connection.execute(
            "UPDATE batches SET status=?, completed_at=? WHERE id=?",
            (status, now, batch_id),
        )
    number = int(
        connection.execute(
            "SELECT number FROM batches WHERE id=?", (batch_id,)
        ).fetchone()[0]
    )
    statefile.push(
        ctx.state, ctx.runtime, ctx.paths, store, label=f"{history_label(ctx)}-{number}"
    )
    _clear(ctx.paths.staging)
    ctx.logger.info(
        PHASE,
        "checkpoint",
        "batch checkpointed",
        batch=batch_id,
        checkpointed=len(good),
        failed=failed,
    )
    return {"checkpointed": len(good), "failed": failed}
