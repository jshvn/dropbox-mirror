from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import PurePosixPath

from .. import session, statefile
from ..logging import utc_now
from ..providers.proton_cli import (
    ProtonCLIError,
    ProtonCLIProvider,
    child_cli_path,
    unwrap,
)
from ..store import Store
from .base import PhaseContext, PhaseResult
from .batch import history_label, parent_cli_path, resolve_children

PHASE = "50_trash"


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    connection = ctx.state.connection
    rows = connection.execute(
        "SELECT * FROM delta_deleted WHERE run_id=? ORDER BY path_lower", (ctx.run_id,)
    ).fetchall()
    if not ctx.apply:
        return PhaseResult(status="PLANNED", outputs={"planned": len(rows)})
    if run["remaining_batches"] is None or int(run["remaining_batches"]) > 0:
        ctx.logger.info(
            PHASE,
            "gate",
            "trash deferred until every batch has landed",
            planned=len(rows),
        )
        return PhaseResult(outputs={"skipped": "batches remain", "planned": len(rows)})
    if not rows:
        return PhaseResult(
            outputs={
                "planned": 0,
                "trashed": 0,
                "not_found": 0,
                "listing_failed": 0,
                "folders": 0,
            }
        )
    store = Store(ctx.runtime, ctx.paths)
    proton = ProtonCLIProvider(
        ctx.cfg,
        ctx.state,
        ctx.logger,
        after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store),
    )
    proton.root_uid(PHASE)
    by_parent: dict[str, list] = defaultdict(list)
    for row in rows:
        by_parent[
            parent_cli_path(ctx.cfg.proton.destination, str(row["path_display"]))
        ].append(row)
    counts: Counter[str] = Counter()
    for parent, group in sorted(by_parent.items()):
        try:
            by_name = resolve_children(proton, parent, PHASE)
        except ProtonCLIError:
            # The files may well still be there: keep their state rows so tomorrow retries.
            for row in group:
                _record(ctx, row, "LISTING_FAILED", None)
            counts["listing_failed"] += len(group)
            continue
        targets = []
        found = []
        for row in group:
            name = PurePosixPath(str(row["path_display"])).name
            candidates = by_name.get(name, [])
            node = next(
                (
                    n
                    for n in candidates
                    if str(unwrap(n.get("type"))).casefold() == "file"
                ),
                None,
            )
            if node is None:
                _record(ctx, row, "NOT_FOUND", None)
                counts["not_found"] += 1
                continue
            uid = str(unwrap(node["uid"]))
            targets.append(child_cli_path(parent, name, uid, len(candidates) > 1))
            found.append((row, uid))
        if targets:
            proton.trash(targets, PHASE)
            for row, uid in found:
                _record(ctx, row, "TRASHED", uid)
                counts["trashed"] += 1
        counts["folders"] += 1
    with connection:
        connection.execute(
            """DELETE FROM mirror_objects WHERE path_lower IN
               (SELECT path_lower FROM deletions WHERE run_id=? AND status IN ('TRASHED', 'NOT_FOUND'))""",
            (ctx.run_id,),
        )
    statefile.push(
        ctx.state, ctx.runtime, ctx.paths, store, label=f"{history_label(ctx)}-trash"
    )
    outputs = {
        "planned": len(rows),
        "trashed": counts["trashed"],
        "not_found": counts["not_found"],
        "listing_failed": counts["listing_failed"],
        "folders": counts["folders"],
    }
    ctx.logger.info(PHASE, "gate", "deleted files trashed", **outputs)
    return PhaseResult(outputs=outputs)


def _record(ctx: PhaseContext, row, status: str, uid: str | None) -> None:
    with ctx.state.connection:
        ctx.state.connection.execute(
            """INSERT OR REPLACE INTO deletions(run_id, path_lower, path_display, proton_uid, status, trashed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ctx.run_id,
                row["path_lower"],
                row["path_display"],
                uid,
                status,
                utc_now() if status == "TRASHED" else None,
            ),
        )
