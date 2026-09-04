from __future__ import annotations

from .. import session, statefile
from ..filesystem import comparison_key
from ..providers.proton_cli import ProtonCLIProvider
from ..store import Store
from .base import PhaseContext, PhaseResult
from .batch import history_label

PHASE = "60_reconcile"


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    if not run["reconcile"]:
        return PhaseResult(outputs={"skipped": "not a reconcile run"})
    if run["remaining_batches"] is None or int(run["remaining_batches"]) > 0:
        return PhaseResult(outputs={"skipped": "batches remain"})
    if not ctx.apply:
        return PhaseResult(status="PLANNED", outputs={"planned": "full Proton walk"})
    store = Store(ctx.runtime, ctx.paths)
    proton = ProtonCLIProvider(
        ctx.cfg,
        ctx.state,
        ctx.logger,
        after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store),
    )
    proton.root_uid(PHASE)
    snapshot_id = proton.inventory(
        f"reconcile:{ctx.run_id}", PHASE, reuse_complete=True
    )
    connection = ctx.state.connection
    with connection:
        # One walk is evidence enough; the previous one is dead weight in every checkpoint.
        stale = [
            (int(r["id"]),)
            for r in connection.execute(
                "SELECT id FROM proton_snapshots WHERE id != ?", (snapshot_id,)
            )
        ]
        for table in ("proton_nodes", "proton_folders"):
            connection.executemany(f"DELETE FROM {table} WHERE snapshot_id=?", stale)
        connection.executemany("DELETE FROM proton_snapshots WHERE id=?", stale)
    # proton_nodes.relative_path (and its comparison_key) carry no leading slash while
    # Dropbox display paths do; comparison_key strips it on both sides before keying.
    nodes = {
        str(row["comparison_key"]): row
        for row in connection.execute(
            "SELECT * FROM proton_nodes WHERE snapshot_id=? AND LOWER(node_type)='file'",
            (snapshot_id,),
        )
    }
    dropped = refreshed = 0
    known: set[str] = set()
    with connection:
        for row in connection.execute("SELECT * FROM mirror_objects").fetchall():
            # Captured before the drop below: a row this reconcile is about to correct
            # still counts as state the mirror knows about, not an untracked stray.
            key = comparison_key(str(row["path_display"]))
            known.add(key)
            node = nodes.get(key)
            if (
                node is None
                or node["claimed_size"] is None
                or int(node["claimed_size"]) != int(row["size"])
            ):
                connection.execute(
                    "DELETE FROM mirror_objects WHERE path_lower=?",
                    (row["path_lower"],),
                )
                dropped += 1
            elif row["proton_uid"] != node["uid"]:
                connection.execute(
                    "UPDATE mirror_objects SET proton_uid=? WHERE path_lower=?",
                    (node["uid"], row["path_lower"]),
                )
                refreshed += 1
    known |= {
        comparison_key(str(row["path_display"]))
        for row in connection.execute(
            "SELECT path_display FROM dropbox_objects "
            "WHERE inventory_id=? AND tag='file' AND is_downloadable=1",
            (run["inventory_id"],),
        )
    }
    strays = sorted(
        str(node["cli_path"]) for key, node in nodes.items() if key not in known
    )
    if strays:
        proton.trash(strays, PHASE)
    statefile.push(
        ctx.state,
        ctx.runtime,
        ctx.paths,
        store,
        label=f"{history_label(ctx)}-reconcile",
    )
    outputs = {
        "snapshot_id": snapshot_id,
        "proton_files": len(nodes),
        "dropped": dropped,
        "uid_refreshed": refreshed,
        "strays_trashed": len(strays),
    }
    ctx.logger.info(PHASE, "gate", "weekly reconcile complete", **outputs)
    return PhaseResult(outputs=outputs)
