from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from itertools import groupby
from typing import Any

from .base import PhaseContext, PhaseError


def _groups(rows: Iterator[Any]) -> Iterator[tuple[str, list[Any]]]:
    for key, values in groupby(rows, key=lambda row: str(row["comparison_key"])):
        yield key, list(values)


def _reconcile(ctx: PhaseContext, api_id: int, rclone_id: int) -> Counter[str]:
    connection = ctx.state.connection
    api_groups = _groups(
        iter(
            connection.execute(
                """
                SELECT * FROM dropbox_objects
                WHERE inventory_id=? AND tag='file' AND is_downloadable=1
                ORDER BY comparison_key, object_key
                """,
                (api_id,),
            )
        )
    )
    rclone_groups = _groups(
        iter(
            connection.execute(
                """
                SELECT * FROM rclone_objects
                WHERE inventory_id=? AND is_dir=0
                ORDER BY comparison_key, object_key
                """,
                (rclone_id,),
            )
        )
    )
    left = next(api_groups, None)
    right = next(rclone_groups, None)
    counts: Counter[str] = Counter()
    with connection:
        connection.execute(
            """
            DELETE FROM source_reconciliation
            WHERE api_inventory_id=? AND rclone_inventory_id=?
            """,
            (api_id, rclone_id),
        )
        while left is not None or right is not None:
            if right is None or (left is not None and left[0] < right[0]):
                key, api_rows, rclone_rows = left[0], left[1], []
                left = next(api_groups, None)
            elif left is None or right[0] < left[0]:
                key, api_rows, rclone_rows = right[0], [], right[1]
                right = next(rclone_groups, None)
            else:
                key, api_rows, rclone_rows = left[0], left[1], right[1]
                left = next(api_groups, None)
                right = next(rclone_groups, None)

            classification, details = _classify(ctx, api_rows, rclone_rows)
            counts[classification] += 1
            connection.execute(
                """
                INSERT INTO source_reconciliation(
                    api_inventory_id, rclone_inventory_id, comparison_key,
                    api_object_key, rclone_object_key, classification, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    api_id,
                    rclone_id,
                    key,
                    api_rows[0]["object_key"] if len(api_rows) == 1 else None,
                    rclone_rows[0]["object_key"] if len(rclone_rows) == 1 else None,
                    classification,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                ),
            )
    return counts


def _classify(
    ctx: PhaseContext, api_rows: list[Any], rclone_rows: list[Any]
) -> tuple[str, dict[str, Any]]:
    details = {
        "api_paths": [row["path_display"] for row in api_rows],
        "rclone_paths": [row["path"] for row in rclone_rows],
    }
    if len(api_rows) > 1 or len(rclone_rows) > 1:
        return "PATH_COLLISION", details
    if not api_rows:
        return "RCLONE_ONLY", details
    if not rclone_rows:
        return "MISSING_IN_RCLONE", details
    api = api_rows[0]
    rclone = rclone_rows[0]
    details.update(
        {
            "api_size": api["size"],
            "rclone_size": rclone["size"],
            "api_hash": api["content_hash"],
            "rclone_hash": rclone["dropbox_hash"],
            "api_id": api["dropbox_id"],
            "rclone_id": rclone["object_id"],
        }
    )
    problems: list[str] = []
    if int(api["size"] or 0) != int(rclone["size"] or 0):
        problems.append("SIZE")
    api_hash = str(api["content_hash"] or "").casefold()
    rclone_hash = str(rclone["dropbox_hash"] or "").casefold()
    if not api_hash or not rclone_hash:
        problems.append("HASH_UNAVAILABLE")
    elif api_hash and rclone_hash and api_hash != rclone_hash:
        problems.append("HASH")
    api_object_id = str(api["dropbox_id"] or "")
    rclone_object_id = str(rclone["object_id"] or "")
    if api_object_id and rclone_object_id and api_object_id != rclone_object_id:
        problems.append("ID")
    details["problems"] = problems
    return ("MISMATCH" if problems else "MATCH"), details


def gate(ctx: PhaseContext, api_id: int, rclone_id: int) -> Counter[str]:
    counts = _reconcile(ctx, api_id, rclone_id)
    discrepancies = sum(count for name, count in counts.items() if name != "MATCH")
    ctx.logger.info(
        "10_inventory",
        "observer",
        "independent Dropbox listings compared",
        **{name: count for name, count in counts.items()},
    )
    if discrepancies:
        raise PhaseError(
            f"Dropbox API/rclone listings have {discrepancies} discrepancies"
        )
    return counts
