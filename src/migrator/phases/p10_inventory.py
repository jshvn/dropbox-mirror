from __future__ import annotations

import sqlite3

from ..providers.dropbox_api import DropboxAPIProvider
from ..providers.dropbox_auth import access_token
from .base import PhaseContext, PhaseResult

PHASE = "10_inventory"
# inventory-run table -> the tables keyed by its id
_INVENTORY_TABLES = {
    "dropbox_inventory_runs": ("dropbox_objects", "dropbox_pages"),
}


def prune_inventories(connection: sqlite3.Connection, keep: int = 2) -> int:
    """Old listings are the bulk of the state, and every checkpoint ships the state to R2."""
    pruned = 0
    with connection:
        for runs_table, child_tables in _INVENTORY_TABLES.items():
            stale = [
                (int(r["id"]),)
                for r in connection.execute(
                    f"SELECT id FROM {runs_table} ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (keep,),
                )
            ]
            for table in child_tables:
                connection.executemany(
                    f"DELETE FROM {table} WHERE inventory_id=?", stale
                )
            connection.executemany(f"DELETE FROM {runs_table} WHERE id=?", stale)
            pruned += len(stale)
    return pruned


def run(ctx: PhaseContext) -> PhaseResult:
    purpose = f"run:{ctx.run_id}"
    token = access_token(ctx.cfg, ctx.runtime)
    ctx.logger.add_secret(token)
    api = DropboxAPIProvider(ctx.cfg, ctx.state, ctx.logger, token=token)
    inventory_id = api.inventory(purpose, reuse_complete=True)
    with ctx.state.connection:
        # A file Dropbox lists as downloadable but without a content hash cannot be
        # verified, so it cannot be mirrored; count it with the non-downloadable ones
        # instead of letting it hold "percent mirrored" under 100 forever.
        unhashed = ctx.state.connection.execute(
            "UPDATE dropbox_objects SET is_downloadable=0 WHERE inventory_id=? AND tag='file' "
            "AND is_downloadable=1 AND (content_hash IS NULL OR size IS NULL)",
            (inventory_id,),
        ).rowcount
    summary = ctx.state.connection.execute(
        """
        SELECT
          SUM(CASE WHEN tag='file' AND is_downloadable=1 THEN 1 ELSE 0 END) AS files,
          SUM(CASE WHEN tag='folder' THEN 1 ELSE 0 END) AS folders,
          SUM(CASE WHEN tag='file' AND is_downloadable=1 THEN size ELSE 0 END) AS bytes,
          SUM(CASE WHEN tag='file' AND is_downloadable=0 THEN 1 ELSE 0 END) AS non_downloadable
        FROM dropbox_objects WHERE inventory_id=?
        """,
        (inventory_id,),
    ).fetchone()
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    outputs = {
        "inventory_id": inventory_id,
        "files": int(summary["files"] or 0),
        "folders": int(summary["folders"] or 0),
        "bytes": int(summary["bytes"] or 0),
        "non_downloadable": int(summary["non_downloadable"] or 0),
        "unhashed": int(unhashed),
        "pruned_inventories": prune_inventories(ctx.state.connection),
    }
    ctx.logger.info(PHASE, "gate", "Dropbox inventory complete", **outputs)
    return PhaseResult(outputs=outputs)
