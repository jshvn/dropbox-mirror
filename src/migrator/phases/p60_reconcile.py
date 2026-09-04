from __future__ import annotations

import sqlite3

from .. import session, statefile
from ..filesystem import comparison_key
from ..providers.proton_cli import ProtonCLIProvider
from ..store import Store
from .base import PhaseContext, PhaseResult
from .batch import history_label

PHASE = "60_reconcile"
SNAPSHOT_PURPOSE = "reconcile"


def _prune_other_snapshots(connection: sqlite3.Connection, snapshot_id: int) -> None:
    # One walk is evidence enough; the previous one is dead weight in every checkpoint.
    # The walk just completed or resumed is the one kept, whether or not it finished.
    with connection:
        stale = [
            (int(r["id"]),)
            for r in connection.execute(
                "SELECT id FROM proton_snapshots WHERE id != ?", (snapshot_id,)
            )
        ]
        for table in ("proton_nodes", "proton_folders"):
            connection.executemany(f"DELETE FROM {table} WHERE snapshot_id=?", stale)
        connection.executemany("DELETE FROM proton_snapshots WHERE id=?", stale)


def _folder_counts(connection: sqlite3.Connection, snapshot_id: int) -> tuple[int, int]:
    listed = connection.execute(
        "SELECT COUNT(*) AS count FROM proton_folders WHERE snapshot_id=? AND status='COMPLETE'",
        (snapshot_id,),
    ).fetchone()["count"]
    pending = connection.execute(
        "SELECT COUNT(*) AS count FROM proton_folders WHERE snapshot_id=? AND status!='COMPLETE'",
        (snapshot_id,),
    ).fetchone()["count"]
    return int(listed), int(pending)


def _correct_mirror(
    connection: sqlite3.Connection, nodes: dict[str, sqlite3.Row]
) -> tuple[int, int, int, int, set[str]]:
    """Drops mirror_objects rows Proton lacks, mis-sizes or whose SHA-1 differs, and
    refreshes the recorded UID for the rest. Returns (dropped, refreshed, matched,
    sha1_mismatch, comparison keys of the rows still on record)."""
    dropped = refreshed = matched = sha1_mismatch = 0
    known: set[str] = set()
    with connection:
        for row in connection.execute("SELECT * FROM mirror_objects").fetchall():
            # Captured before the drop below: a row this reconcile is about to correct
            # still counts as state the mirror knows about, not an untracked stray.
            key = comparison_key(str(row["path_display"]))
            known.add(key)
            node = nodes.get(key)
            size_mismatch = (
                node is None
                or node["claimed_size"] is None
                or int(node["claimed_size"]) != int(row["size"])
            )
            digest_mismatch = (
                not size_mismatch
                and node["sha1"] is not None
                and str(node["sha1"]) != str(row["sha1"])
            )
            if size_mismatch or digest_mismatch:
                connection.execute(
                    "DELETE FROM mirror_objects WHERE path_lower=?",
                    (row["path_lower"],),
                )
                dropped += 1
                if digest_mismatch:
                    sha1_mismatch += 1
                continue
            matched += 1
            if row["proton_uid"] != node["uid"]:
                connection.execute(
                    "UPDATE mirror_objects SET proton_uid=? WHERE path_lower=?",
                    (node["uid"], row["path_lower"]),
                )
                refreshed += 1
    return dropped, refreshed, matched, sha1_mismatch, known


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
    deadline = int(run["start_epoch"]) + int(run["budget_minutes"]) * 60 - 600
    # ponytail: the walk is one CLI process per folder, so its cost is the folder count,
    # not the file count; a few thousand folders fit a run, tens of thousands do not. A
    # stable purpose plus reuse_complete=False resumes the RUNNING snapshot the deadline
    # or a killed run left behind instead of restarting at the root, and still refuses to
    # reuse a COMPLETE walk from a previous reconcile. The upgrade path is a recursive
    # listing.
    snapshot_id = proton.inventory(
        SNAPSHOT_PURPOSE, PHASE, reuse_complete=False, deadline=deadline
    )
    connection = ctx.state.connection
    _prune_other_snapshots(connection, snapshot_id)
    snapshot_status = str(
        connection.execute(
            "SELECT status FROM proton_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()["status"]
    )
    folders_listed, folders_pending = _folder_counts(connection, snapshot_id)
    label = f"{history_label(ctx)}-reconcile"
    if snapshot_status != "COMPLETE":
        statefile.push(ctx.state, ctx.runtime, ctx.paths, store, label=label)
        ctx.logger.info(
            PHASE,
            "figures",
            "reconcile figures",
            snapshot_id=snapshot_id,
            complete=0,
            folders_listed=folders_listed,
            folders_pending=folders_pending,
            proton_files=0,
            matched=0,
            dropped=0,
            uid_refreshed=0,
            strays_trashed=0,
        )
        return PhaseResult(outputs={"partial": folders_pending})
    # proton_nodes.relative_path (and its comparison_key) carry no leading slash while
    # Dropbox display paths do; comparison_key strips it on both sides before keying.
    nodes = {
        str(row["comparison_key"]): row
        for row in connection.execute(
            "SELECT * FROM proton_nodes WHERE snapshot_id=? AND LOWER(node_type)='file'",
            (snapshot_id,),
        )
    }
    dropped, refreshed, matched, sha1_mismatch, known = _correct_mirror(
        connection, nodes
    )
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
    statefile.push(ctx.state, ctx.runtime, ctx.paths, store, label=label)
    ctx.logger.info(
        PHASE,
        "figures",
        "reconcile figures",
        snapshot_id=snapshot_id,
        complete=1,
        folders_listed=folders_listed,
        folders_pending=folders_pending,
        proton_files=len(nodes),
        matched=matched,
        dropped=dropped,
        uid_refreshed=refreshed,
        strays_trashed=len(strays),
    )
    outputs = {
        "snapshot_id": snapshot_id,
        "proton_files": len(nodes),
        "dropped": dropped,
        "uid_refreshed": refreshed,
        "strays_trashed": len(strays),
        "matched": matched,
        "sha1_mismatch": sha1_mismatch,
    }
    return PhaseResult(outputs=outputs)
