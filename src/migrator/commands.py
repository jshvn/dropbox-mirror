from __future__ import annotations

import json
import shutil
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from . import session, statefile
from .config import Config, load_config
from .env import Runtime
from .paths import WorkPaths
from .state import State
from .store import Store


def _paths(runtime: Runtime) -> WorkPaths:
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    return paths


def clock(runtime: Runtime, args: list[str]) -> int:
    if runtime.run_epoch is None:
        raise ValueError("MIRROR_RUN_EPOCH must be set by the Taskfile")
    paths = _paths(runtime)
    shutil.rmtree(paths.staging, ignore_errors=True)
    paths.staging.mkdir()
    for stale in (paths.report, paths.chain):
        stale.unlink(missing_ok=True)
    started = datetime.fromtimestamp(runtime.run_epoch, UTC)
    stamp = {
        "start_epoch": runtime.run_epoch,
        "hour_utc": started.hour,
        "weekday": started.weekday(),
    }
    paths.clock.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
    print(f"clock: run started {started.isoformat()}")
    return 0


def read_clock(paths: WorkPaths) -> dict[str, int]:
    return json.loads(paths.clock.read_text(encoding="utf-8"))


def is_reconcile_run(
    cfg: Config, runtime: Runtime, db: State, *, start_epoch: int, weekday: int
) -> bool:
    """RECONCILE=true, or the first run that starts on the configured UTC weekday. Keyed
    on the day, not an hour: chained and queued runs start at any hour."""
    if runtime.reconcile:
        return True
    if weekday != cfg.reconcile.weekday:
        return False
    day_start = start_epoch - start_epoch % 86400
    earlier = db.connection.execute(
        "SELECT COUNT(*) FROM runs WHERE start_epoch >= ? AND start_epoch < ?",
        (day_start, start_epoch),
    ).fetchone()[0]
    return int(earlier) == 0


def session_restore(runtime: Runtime, args: list[str]) -> int:
    paths = _paths(runtime)
    session.restore(runtime, paths, Store(runtime, paths))
    print("session: restored")
    return 0


def _fetch_state(runtime: Runtime, paths: WorkPaths) -> str:
    paths.state_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(paths.state_db) + suffix).unlink(missing_ok=True)
    return statefile.fetch(runtime, paths, Store(runtime, paths))


def state(runtime: Runtime, args: list[str]) -> int:
    cfg = load_config(runtime.config_path)
    paths = _paths(runtime)
    outcome = _fetch_state(runtime, paths)
    stamp = read_clock(paths)
    db = State(paths.state_db, cfg.mirror.id)
    try:
        db.initialize_migration(cfg.source_file, cfg.source_sha256)
        # Decided before this run's row exists, so the row itself cannot count as "earlier".
        reconcile = is_reconcile_run(
            cfg, runtime, db, start_epoch=stamp["start_epoch"], weekday=stamp["weekday"]
        )
        run_id = db.start_run(
            start_epoch=stamp["start_epoch"],
            hour_utc=stamp["hour_utc"],
            weekday=stamp["weekday"],
            budget_minutes=runtime.budget_override or cfg.budget.run_budget_minutes,
            host=runtime.host,
            reconcile=reconcile,
        )
        files, size = db.mirror_totals()
    finally:
        db.close()
    print(f"state: {outcome}; run {run_id}; mirrored files={files} bytes={size}")
    return 0


def _http_get(url: str) -> None:
    with urllib.request.urlopen(
        url, timeout=10
    ) as response:  # fixed https URL from config
        response.read()


def ping(runtime: Runtime, args: list[str]) -> int:
    if not runtime.healthcheck_url:
        print("ping: MIRROR_HEALTHCHECK_URL unset; skipped")
        return 0
    # A bare word, not a flag: the command's `args` positional rejects anything starting "--".
    url = runtime.healthcheck_url.rstrip("/") + ("/fail" if "fail" in args else "")
    for _attempt in range(3):
        try:
            _http_get(url)
            return 0
        except OSError:
            continue
    print("ping: healthchecks.io unreachable after 3 attempts")
    return 0


def status(runtime: Runtime, args: list[str]) -> int:
    cfg = load_config(runtime.config_path)
    paths = _paths(runtime)
    _fetch_state(runtime, paths)  # reads R2 directly and starts no run row
    db = State(paths.state_db, cfg.mirror.id)
    try:
        files, size = db.mirror_totals()
        run = db.connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        phases = db.connection.execute(
            "SELECT phase_name, status, started_at, completed_at FROM phase_runs "
            "WHERE id IN (SELECT MAX(id) FROM phase_runs GROUP BY phase_number) ORDER BY phase_number"
        ).fetchall()
        figures = db.connection.execute(
            "SELECT fields_json FROM events WHERE operation='figures' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    print(
        json.dumps(
            {
                "mirrored_files": files,
                "mirrored_bytes": size,
                "last_run": dict(run) if run else None,
                "phases": [dict(row) for row in phases],
                "last_figures": json.loads(figures["fields_json"]) if figures else None,
            },
            indent=2,
        )
    )
    return 0


def state_push(runtime: Runtime, args: list[str]) -> int:
    cfg = load_config(runtime.config_path)
    paths = _paths(runtime)
    db = State(paths.state_db, cfg.mirror.id)
    try:
        label = args[0] if args else f"manual-{runtime.run_epoch or 0}"
        statefile.push(db, runtime, paths, Store(runtime, paths), label=label)
    finally:
        db.close()
    print(f"state-push: {label}")
    return 0


def state_rollback(runtime: Runtime, args: list[str]) -> int:
    paths = _paths(runtime)
    store = Store(runtime, paths)
    if not args:
        for key in store.list(statefile.HISTORY_PREFIX):
            print(key)
        print("state-rollback: pass one of the keys above")
        return 1
    statefile.rollback(store, args[0])
    print(f"state-rollback: {args[0]} is now the canonical state")
    return 0


def session_seal(runtime: Runtime, args: list[str]) -> int:
    if not args:
        raise ValueError("session-seal needs the laptop PROTON_DRIVE_CACHE_DIR path")
    paths = _paths(runtime)
    session.seal(runtime, paths, Store(runtime, paths), Path(args[0]))
    print("session-seal: uploaded")
    return 0
