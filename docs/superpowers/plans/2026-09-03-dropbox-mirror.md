# dropbox-mirror Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A nightly, self-chaining GitHub Actions pipeline that mirrors Dropbox into Proton Drive under `/my-files/Dropbox`, round-trips every uploaded byte, checkpoints a SQLite state to R2 after every batch, and reports counts-only analytics.

**Architecture:** A Taskfile is the operator surface; every step is `python -m migrator <phase>` run inside one pinned toolbox image. Infrastructure modules (state, hashing, atomic writes, redacting logger, guards, the three providers) are imported from donphi/dropbox_proton; the mirror phases (inventory, delta, plan, batches, trash, reconcile, report) are new and written in the donor's style. R2 holds the only record of progress: `.state/state.sqlite.xz.age` plus a dated history, and `.state/session.tar.age` for the Proton CLI session.

**Tech Stack:** Python 3.13 (stdlib + `requests`), SQLite, rclone (Dropbox reads and R2 object store), official `proton-drive` CLI, `age`, go-task, Docker or Apple `container`, GitHub Actions, 1Password service account.

**Spec:** `docs/superpowers/specs/2026-09-03-dropbox-mirror-design.md` (research sidecar: `2026-09-03-dropbox-mirror-research.md`). Donor source: `https://github.com/donphi/dropbox_proton` at commit `cfd0e57` (MIT). Clone it once to `$DONOR` (any scratch path) before Task 1; every "copy from donor" step below refers to that checkout.

## Global Constraints

- Python `>=3.13`; the only runtime dependency is `requests>=2.32,<3`; tests use `pytest>=8.4,<9`; lint and format with `ruff`.
- Every check and every pipeline step runs inside the toolbox image (`task run -- ...`); the host supplies a container engine and go-task only.
- No AI attribution anywhere. No emojis outside markdown. Commit format `<type>(<scope>): <summary>` under 75 chars, imperative mood.
- Comments and docs describe the current system only: never "replaces", "imported from", or migration history in code comments. Provenance lives in `LICENSE` and this plan.
- Intentional shortcuts carry a `ponytail:` comment naming the ceiling and the upgrade path.
- Logs and the step summary carry counts, never mirrored path names (spec section 7). Console output never prints an object identifier.
- Secrets reach processes by environment variable name only, resolved from 1Password at run time; `op.env` holds `op://` references and is committed.
- `AWS_REGION`-style literals such as `RCLONE_CONFIG_R2_REGION=auto` are `ENV` lines in the Dockerfile, never secrets; they reach every process in the toolbox whether or not `task` wraps it.
- The pipeline is plan-by-default: `batches`, `trash`, `reconcile`, `empty-trash` mutate only with `--apply`.
- Every mutation is confirmed by independent observation; a command exit status is never evidence.
- Non-trivial logic leaves one runnable check behind (pytest, no fixtures beyond `conftest.py`).
- Verification commands in this plan run as `task test` (pytest in the image) once Task 2 lands; Task 1 uses `uv run --with pytest pytest` on the host because the image does not exist yet.

## Deviations from the spec, decided here

- **R2 access uses rclone, not AWS CLI v2.** rclone is already in the image and speaks S3; the AWS CLI would add a 100 MB install for four commands. `aws.config` is therefore not created. Rollback is `rclone copyto` server-side (Task 8).
- **`xz` and ICU are not installed.** Python's `lzma` and `unicodedata` cover both.
- **`normalization.py` is not imported.** In the donor it is Dropbox conflict-name normalization, not comparison keys; comparison keys come from `filesystem.py`.
- **The image holds toolchain only; the code is bind-mounted at `/work`** with `PYTHONPATH=/work/src`. Code edits never require an image rebuild.
- **`empty_trash` is not a workflow input.** A permanent deletion behind a checkbox with no confirmation is a footgun; `task empty-trash` runs it from a laptop with a prompt.
- **The `confirm` step lists each batch parent folder** and matches name, size, and SHA-1 instead of parsing the upload JSON summary, whose exact shape is unverified. This also yields the UIDs that `mirror_objects` and the round-trip need. The raw upload stdout is still stored in `commands`.
- **Non-secret rclone remote literals are Dockerfile `ENV`, not Taskfile `env:`.** Several operator tasks and the workflow's failure path run `python -m migrator` in the toolbox without an inner `task`, and a Taskfile `env:` never reaches those.
- **The nightly Proton touch is `root_uid` at the top of `batches`, unconditional.** The spec puts a cheap listing in the `session` step; that step runs before the state exists, so the CLI call could not be recorded as evidence. `root_uid` is a listing, it is the first CLI call of the run, and its `after_call` writes the session back.
- **Reconcile is the first run of the configured UTC weekday, not a run in a configured hour.** The dispatch fires at 02:10, chained runs start at any hour, and a queued run starts whenever the previous one ends; an hour key either never matches or matches several chained runs in a row.
- **`report` pushes the state under `--apply`.** The run row, its figures event and the final status only exist on the runner until something uploads them; without this push `task status` and the trend queries would never see a finished run.
- **Not computed:** files skipped as SHA-1 identical (that needs the unverified upload JSON) and batches repeated from a killed run (a new run re-plans from the state, so the repeat is invisible; the INTERRUPTED run row is the evidence).
- **Retention.** Every checkpoint ships the whole database to R2, so the listing tables keep the newest two Dropbox inventories, PLANNED batches from earlier runs are deleted at plan time, and the Proton snapshot table keeps one walk.

## Review notes, 2026-09-03

An adversarial pass over the first draft. Everything under "changed" is edited into the tasks below; everything under "not changed" is for the author and the implementor to weigh.

Changed inline:

1. A quiet night made no Proton call (`root_uid` ran only when batches were planned), so the session never refreshed and the 60-day idle expiry stayed live. Task 14 calls it unconditionally.
2. The reconcile rule keyed on `hour_utc = 3` while the dispatch is `10 2 * * *`: the weekly reconcile could never fire from a scheduled run. Tasks 3 and 9 key it on the first run of the configured weekday, read from the `runs` table.
3. The run's final status and figures never reached R2. Task 17 pushes under `--apply`; Task 18 gives `pipeline` the `--apply` and `plan-pipeline` a plain `report`.
4. `task status` started a run row in the R2 state, which showed itself as the last run and would have masked the reconcile day. Task 9 makes `status` fetch the state itself and start nothing.
5. `python -m migrator ping --fail` cannot parse: `args` is a `nargs="*"` positional and argparse rejects a `--flag`. It is `ping fail` now (Tasks 9, 18, 19).
6. `task empty-trash`, `task state-rollback`, `task session-seal` and the workflow's failure step ran `python -m migrator` directly in the container, without the Taskfile `env:` that held the rclone remote literals, so rclone had no `r2` remote. The literals moved to the Dockerfile (Task 2); the failure path goes through inner tasks (Task 19); `empty-trash` has an inner pipeline task (Task 18).
7. `PROTON_DRIVE_CACHE_DIR` was a fixed image path while `WorkPaths.session` follows `MIRROR_WORK_DIR`. Task 5 sets it from `paths.session` in `run_phase`.
8. `_mismatch` in `confirm` read `claimedSize` through `or -1`, so every empty file failed confirmation as a size mismatch (Task 13).
9. `fetch` marked every row VANISHED if rclone wrote display-cased names, and the batch then checkpointed as an empty success. Task 13 accepts either spelling and refuses a batch where rclone exited 0 yet nothing reached staging.
10. A transient failure listing one Proton folder in `trash` recorded its files NOT_FOUND and dropped them from the state while they stayed in Proton. Task 15 records LISTING_FAILED and keeps those rows for the next night.
11. Reconcile compared `comparison_key("/Keep/ok.txt")` against `comparison_key("Keep/ok.txt")`; Task 16 strips the leading slash.
12. A wrong bucket name or credential read as "fresh" (rclone's not-found exit on both probes). Task 8 adds `Store.probe()`, required before "fresh" is accepted.
13. State growth: every run appended a full Dropbox listing (`dropbox_objects` with `raw_json`) and a full re-plan (`batches`, `batch_items`) and never pruned, while every checkpoint ships the whole file. Tasks 10, 12 and 16 prune (see Retention above).
14. `checkpoint` uploaded the same blob twice; Task 8 puts the history object and server-side copies it to the canonical key. History labels use the run's start epoch (spec 5.2) instead of `runs.id`, which a rollback can re-issue.
15. `VACUUM INTO` fails inside an open transaction; `snapshot_to` commits first (Task 4).
16. The Proton CLI exits 1 when any single item fails; `upload_tree` raised on that before `confirm` could adjudicate per file. Task 7 accepts exit 1 for upload; an AUTH category still raises.
17. `main()` caught four exception classes and let the rest print a traceback, provider stderr included, to CI logs; it now catches every exception (Task 5). The batch loop marks the batch FAILED on any exception, not only `PhaseError` (Task 14).
18. `report` returned PASS for a FAIL run, so `pipeline` went on to ping success. It returns the run status now (Task 17), and the workflow's single `always()` step publishes the summary and pings `/fail` unless the job succeeded (Task 19).
19. Report throttling and error counts were all-time; they are scoped to the run's `started_at` (Task 17).
20. Files listed as downloadable but without `content_hash` were silently never mirrored and never counted; Task 10 reclassifies them as non-downloadable in the listing.
21. Batches are capped at `budget.batch_files` (default 5,000) as well as `BATCH_GB` (Tasks 3 and 12); see finding A.
22. Finding B below is adopted: `verify` skips and counts a content-hash mismatch, and fails only when every file in the batch mismatches (Task 13; spec 5.1 updated to match).
23. Finding D below is adopted: shared test helpers live in `conftest.py`, appended by the task that first needs them (Tasks 8, 10, 13).

Not changed:

- **A. Round-trip cost is per file, not per byte.** `roundtrip` spawns one `proton-drive filesystem download` per file. The Bun binary's start-up plus one authenticated request is seconds per file, so a batch of small files is bounded by count. The tree holds 200k to 300k files, mostly images and text, so the seed's round-trip time is that count times the per-file cost whatever the cap; the cap only decides whether one batch fits inside one run. At 5,000 files a batch needs the per-file cost under about 1.8 s to finish inside `RUN_BUDGET_MIN`, and the budget check runs before a batch starts, not during it: a batch that cannot finish is killed by the job timeout before it checkpoints, and the next run repeats it, forever. The spec's "every file, every run, no sampling" stands; the cap keeps a batch inside the budget only if the measured cost allows. The upgrade path is a recursive folder download when a batch covers a whole folder. `roundtrip_seconds` on the first runs decides.
- **B (adopted). `verify` failed the whole run on one content-hash mismatch**, as spec 5.1 said. A file being edited during the run window does that every night it happens. Skipping the item the way VANISHED is skipped (counted, never entering the state, caught by the next listing) is the same amount of code and meets the spec's actual goal, which is that wrong bytes are never recorded.
- **C. Donor DDL and signatures were written from memory of `cfd0e57`.** Column names in the seed helpers (`dropbox_objects`, `dropbox_inventory_runs`, `dropbox_pages`, `rclone_*`, `proton_nodes`, `proton_folders`, `events`, `commands`), `start_phase`'s keyword set (`tool_versions` in Task 4's test, `command_parameters` in Task 5's runner), `record_artifact`, the `walk_tree` item attributes, and whether `validate_dropbox_scope("")` accepts the whole-Dropbox root all need checking against the checkout; Task 4 step 5 says where.
- **D (adopted). Shared test helpers** (`FakeStore`, `plain_crypt`, `FakeProton`, `_node`, `_seed_api_inventory`) are imported across `test_*.py` files; the global constraint says fixtures live in `conftest.py`. Moving them is mechanical and belongs to the first importing task (8).
- **E. The toolbox image is rebuilt from upstream downloads on every CI run.** A proton.me or GitHub release outage fails the night, which silence-is-the-alert tolerates; publishing the image to GHCR is the fix if it bites.
- **F. Runtime preflight of binary versions** (spec section 8) happens only at image build. `provider.version()` at the top of `batches` is the addition if a mounted binary ever matters.
- **G. Case-only renames in Dropbox** (`Report.pdf` to `report.pdf`) produce no delta and leave Proton's name unchanged, because the key is `path_lower`.

---

## File structure

```
dropbox-mirror/
├── Taskfile.yml                     menu, pipeline, toolbox, op wrapper
├── op.env                           op:// references (committed)
├── pyproject.toml                   package + pytest + ruff config
├── LICENSE                          MIT with the donor's copyright notice retained
├── README.md                        runbook: bootstrap, seed, re-login, rollback
├── config/
│   ├── mirror.toml                  the one behavior input (strict schema)
│   └── toolchain.lock.toml          python image digest, rclone, proton-drive, age, task
├── docker/Dockerfile                toolbox image
├── src/migrator/
│   ├── __init__.py
│   ├── __main__.py                  argparse: pre-state commands, phases, utilities
│   ├── atomic.py                    (donor, verbatim)
│   ├── hashing.py                   (donor, verbatim)
│   ├── guards.py                    (donor, verbatim)
│   ├── filesystem.py                (donor, verbatim)
│   ├── logging.py                   (donor; console line drops object identifiers)
│   ├── config.py                    mirror schema on the donor's strict loader
│   ├── env.py                       Runtime: every environment read in one place
│   ├── paths.py                     WorkPaths under .run/
│   ├── state.py                     donor evidence tables + runs, mirror_objects, batches, deletions
│   ├── store.py                     R2 object store via rclone
│   ├── crypt.py                     age encrypt/decrypt wrappers
│   ├── session.py                   Proton CLI session restore / write-back / seal
│   ├── statefile.py                 state fetch (first-run rule) / push (history + canonical) / rollback
│   ├── runner.py                    phase registry and run_phase
│   ├── commands.py                  clock, session, state, ping, status, rollback, seal
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── dropbox_auth.py          refresh token -> access token
│   │   ├── dropbox_api.py           (donor; explicit token, root from cfg.dropbox)
│   │   ├── dropbox_rclone.py        (donor; env-configured remote, copy_files_from)
│   │   └── proton_cli.py            (donor; upload_tree, trash, empty_trash, after_call hook)
│   └── phases/
│       ├── __init__.py
│       ├── base.py                  PhaseContext (+run_id, runtime), PhaseResult, PhaseError
│       ├── observer.py              rclone second listing + reconciliation gate (from donor p10)
│       ├── p10_inventory.py
│       ├── p20_delta.py
│       ├── p30_plan.py
│       ├── batch.py                 fetch, verify, upload, confirm, roundtrip, checkpoint
│       ├── p40_batches.py           the budgeted loop and the chain decision
│       ├── p50_trash.py
│       ├── p60_reconcile.py
│       ├── p70_report.py
│       └── p80_empty_trash.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/                    synthetic JSON only; tests/fixtures/live/ is git-excluded
│   └── test_*.py
└── .github/workflows/
    ├── sync.yml
    └── check.yml
```

Work directory at run time (`MIRROR_WORK_DIR`, default `.run/`, git-excluded):
`state.sqlite`, `clock.json`, `session/` (`PROTON_DRIVE_CACHE_DIR`), `staging/`, `roundtrip/`,
`logs/`, `rclone.conf` (empty file rclone may write to), `report.md`, `chain` (marker),
`age.key` (transient, 0600).

R2 keys: `.state/state.sqlite.xz.age`, `.state/history/<epoch>-<label>.sqlite.xz.age` (label: batch number, `trash`, `reconcile`, `report`),
`.state/session.tar.age`.

## Shared interfaces (every task consumes these names exactly)

```python
# migrator.env
@dataclass(frozen=True)
class Runtime:
    work_dir: Path                 # MIRROR_WORK_DIR, default ".run"
    config_path: Path              # MIRROR_CONFIG, default "config/mirror.toml"
    run_epoch: int | None          # MIRROR_RUN_EPOCH (clock requires it)
    budget_override: int | None    # RUN_BUDGET_MIN
    reconcile: bool                # RECONCILE=true
    verbose: bool                  # MIRROR_VERBOSE=1 prints full error text
    r2_bucket: str                 # MIRROR_R2_BUCKET
    age_identity: str              # MIRROR_AGE_IDENTITY
    healthcheck_url: str           # MIRROR_HEALTHCHECK_URL
    dropbox_app_key: str           # MIRROR_DROPBOX_APP_KEY
    dropbox_app_secret: str        # MIRROR_DROPBOX_APP_SECRET
    dropbox_refresh_token: str     # MIRROR_DROPBOX_REFRESH_TOKEN
    rclone_dropbox_token: str      # RCLONE_CONFIG_DROPBOX_TOKEN (only for redaction)
    r2_secret: str                 # RCLONE_CONFIG_R2_SECRET_ACCESS_KEY (only for redaction)
    host: str                      # "github:<run id>" or the hostname
    def secrets(self) -> list[str]
    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "Runtime"

# migrator.paths
@dataclass(frozen=True)
class WorkPaths:
    root: Path
    state_db, clock, session, staging, roundtrip, logs, rclone_config, report, chain, age_key  # Path properties
    def ensure(self) -> None
    @classmethod
    def from_runtime(cls, runtime: Runtime) -> "WorkPaths"

# migrator.config
Config(mirror: Mirror, dropbox: Dropbox, rclone: Rclone, proton: Proton, budget: Budget,
       reconcile: Reconcile, source_file: Path, source_sha256: str)
load_config(path) -> Config

# migrator.state
class State:  # donor API kept: connection, start_phase, complete_phase, latest_phase, record_event,
              # record_command_start/end, record_identity_observation, record_artifact, latest_completed_id
    def start_run(self, *, start_epoch: int, hour_utc: int, weekday: int, budget_minutes: int,
                  host: str, reconcile: bool) -> int
    def current_run(self) -> sqlite3.Row            # latest RUNNING row, raises RuntimeError
    def update_run(self, run_id: int, **columns) -> None
    def finish_run(self, run_id: int, status: str) -> None
    def mirror_totals(self) -> tuple[int, int]      # (files, bytes)
    def snapshot_to(self, target: Path) -> None     # VACUUM INTO

# migrator.phases.base
class PhaseError(RuntimeError)
@dataclass class PhaseResult: status="PASS"; outputs: dict; artifacts: list
@dataclass class PhaseContext: cfg, paths, state, logger, apply, phase_run_id, run_id, runtime
    def phase_dir(self, name: str) -> Path         # paths.logs / "phases" / name

# migrator.store
class Store:
    def __init__(self, runtime: Runtime, paths: WorkPaths, *, run=subprocess.run)
    def get(self, key: str, target: Path) -> bool    # False when the key is absent
    def put(self, source: Path, key: str) -> None
    def copy(self, source_key: str, target_key: str) -> None
    def list(self, prefix: str) -> list[str]
    def probe(self) -> None                       # raises StoreError unless the bucket lists

# migrator.crypt
def recipient(identity: str, key_file: Path, *, run=subprocess.run) -> str
def encrypt(identity: str, key_file: Path, source: Path, target: Path, *, run=subprocess.run) -> None
def decrypt(identity: str, key_file: Path, source: Path, target: Path, *, run=subprocess.run) -> None

# migrator.session
SESSION_KEY = ".state/session.tar.age"
SESSION_FILES = ("auth-session.json", "clientUid.json")
def restore(runtime, paths, store) -> None
def writeback(runtime, paths, store) -> bool       # True when something was written
def seal(runtime, paths, store, source_dir: Path) -> None

# migrator.statefile
STATE_KEY = ".state/state.sqlite.xz.age"
HISTORY_PREFIX = ".state/history/"
def fetch(runtime, paths, store) -> str            # "restored" | "fresh"
def push(state, runtime, paths, store, label: str) -> None
def rollback(store, history_key: str) -> None

# migrator.providers
DropboxAPIProvider(cfg, state, logger, *, token: str, session=None, sleep=time.sleep)
    .identity(operation) ; .inventory(purpose, *, reuse_complete=True) -> int
DropboxRcloneProvider(cfg, paths, state, logger, *, run=subprocess.run, sleep=time.sleep)
    .inventory(purpose, *, reuse_complete=True) -> int
    .copy_files_from(list_file: Path, target: Path, log_path: Path) -> int   # rclone exit code
ProtonCLIProvider(cfg, state, logger, *, run=subprocess.run, sleep=time.sleep, after_call=None)
    .version() ; .root_uid(phase) -> str ; .list_folder(path, phase) -> list[dict]
    .inventory(purpose, phase, *, reuse_complete=True) -> int
    .download_file(remote_path, local_parent, phase) -> None
    .upload_tree(sources: list[Path], destination: str, phase) -> str   # raw stdout
    .trash(cli_paths: list[str], phase) -> None ; .empty_trash(phase) -> None
child_cli_path(parent_cli_path: str, name: str, uid: str, duplicate: bool) -> str
access_token(cfg, runtime, *, session=None) -> str     # migrator.providers.dropbox_auth

# migrator.runner
PHASES: dict[str, PhaseDefinition]   # command -> (number, name, run)
run_phase(command: str, *, apply: bool, runtime: Runtime) -> str
```

State tables added to the donor schema (Task 4 has the DDL):
`runs`, `mirror_objects`, `delta_changed`, `delta_deleted`, `batches`, `batch_items`, `deletions`.
Statuses: `batches.status` in PLANNED, CHECKPOINTED, FAILED; `batch_items.status` in PLANNED,
VANISHED, FETCHED, HASH_MISMATCH, VERIFIED, CONFIRMED, CONFIRM_FAILED, ROUNDTRIP_OK, ROUNDTRIP_MISMATCH,
CHECKPOINTED; `deletions.status` in TRASHED, NOT_FOUND, LISTING_FAILED.

---

### Task 1: Package scaffold and verbatim donor modules

**Files:**
- Create: `pyproject.toml`, `LICENSE`, `src/migrator/__init__.py`, `src/migrator/providers/__init__.py`, `src/migrator/phases/__init__.py`, `.git/info/exclude`
- Copy from donor: `src/migrator/atomic.py`, `src/migrator/hashing.py`, `src/migrator/guards.py`, `src/migrator/filesystem.py`, `src/migrator/logging.py`
- Copy tests from donor: `tests/test_hashing.py`, `tests/test_guards.py`, `tests/test_filesystem.py`
- Create: `tests/test_logging.py`

**Interfaces:**
- Produces: `hash_file(path) -> FileHashes(size, sha256, sha1, dropbox_content_hash)`, `sha256_file`, `atomic_text_writer`, `write_json`, `write_jsonl`, `write_csv`, `comparison_key(path) -> str`, `walk_tree(root, hash_files=)`, `RunLogger(logs_dir, *, secrets, console, sink)`, `Event`, `utc_now()`, `GuardError`, `validate_proton_cli_path`, `safe_relative_path`, `contained_path`, `dropbox_api_scope`, `validate_dropbox_scope`, `validate_executable`.

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=80"]
build-backend = "setuptools.build_meta"

[project]
name = "dropbox-mirror"
version = "0.1.0"
description = "Nightly Dropbox to Proton Drive mirror run from GitHub Actions"
requires-python = ">=3.13"
license = "MIT"
license-files = ["LICENSE"]
dependencies = ["requests>=2.32,<3"]

[project.optional-dependencies]
test = ["pytest>=8.4,<9", "ruff>=0.16,<1"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
addopts = "-ra"
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
target-version = "py313"
line-length = 88

[tool.ruff.lint]
# RunLogger uses (phase, operation, message), not stdlib formatting.
ignore = ["PLE1205"]
```

- [ ] **Step 2: Write LICENSE**

Copy `$DONOR/LICENSE` verbatim (MIT, donphi's copyright line), then append a second copyright line for Josh Vaughen 2026 above the license text. Two copyright lines, one MIT body.

- [ ] **Step 3: Copy the verbatim modules and their tests**

```bash
mkdir -p src/migrator/providers src/migrator/phases tests/fixtures
touch src/migrator/__init__.py src/migrator/providers/__init__.py src/migrator/phases/__init__.py
for f in atomic hashing guards filesystem logging; do cp "$DONOR/src/migrator/$f.py" src/migrator/; done
for f in test_hashing test_guards test_filesystem; do cp "$DONOR/tests/$f.py" tests/; done
printf '.run/\ntests/fixtures/live/\n' >> .git/info/exclude
```

`test_filesystem.py` imports only `migrator.filesystem` and `migrator.hashing`; `test_guards.py` imports `migrator.guards`; `test_hashing.py` imports `migrator.hashing`. Confirm with `grep '^from migrator' tests/test_*.py`; if any imports `migrator.config`, delete that test function.

- [ ] **Step 4: Write the failing logger test**

`tests/test_logging.py`:

```python
from __future__ import annotations

from migrator.logging import RunLogger


def test_console_line_never_carries_object_identifier(tmp_path, capsys):
    logger = RunLogger(tmp_path, secrets=["s3cret"], console=True)
    logger.info("10_inventory", "page", "committed page s3cret", object_identifier="/Taxes/2024.pdf")
    out = capsys.readouterr().out
    assert "/Taxes/2024.pdf" not in out
    assert "s3cret" not in out
    assert "[REDACTED]" in out
    human = (tmp_path / "migrate.log").read_text(encoding="utf-8")
    assert "object=/Taxes/2024.pdf" in human
```

- [ ] **Step 5: Run it to verify it fails**

Run: `uv run --with pytest --with requests pytest tests/test_logging.py -v`
Expected: FAIL, the console line contains `/Taxes/2024.pdf`.

- [ ] **Step 6: Edit logging.py so the console line omits identifiers**

In `RunLogger.event`, replace the block from `human = (` through `print(human)` with:

```python
        human = (
            f"{event.timestamp} {event.level:<7} "
            f"[{event.phase}:{event.operation}] {event.message}"
        )
        if event.retry_count is not None:
            human += f" retry={event.retry_count}"
        detailed = human
        if event.object_identifier:
            detailed += f" object={event.object_identifier}"
        self._append(self.human_path, detailed + "\n")
        self._append(
            self.jsonl_path,
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        )
        phase_name = re.sub(r"[^A-Za-z0-9_.-]", "_", phase)
        self._append(self.logs_dir / "phases" / f"{phase_name}.log", detailed + "\n")
        if self.console:
            # Console output reaches CI logs; identifiers are mirrored path names.
            print(human)
```

- [ ] **Step 7: Run all tests**

Run: `uv run --with pytest --with requests pytest -v`
Expected: PASS for every test in the four files.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml LICENSE src tests
git commit -m "feat(core): package scaffold with hashing, atomic, guards, logging"
```

---

### Task 2: Toolbox image, toolchain lock, and the Taskfile toolbox tasks

**Files:**
- Create: `docker/Dockerfile`, `config/toolchain.lock.toml`, `Taskfile.yml` (toolbox and check tasks only; pipeline tasks arrive in Task 18)

**Interfaces:**
- Produces: `task image`, `task image-clean`, `task run -- <cmd>`, `task test`, `task lint`, `task fmt`; image name `dropbox-mirror:toolbox`; inside the image: `python`, `rclone`, `proton-drive`, `age`, `age-keygen`, `task`, `requests` at the locked versions; `/work` is the repo, `PYTHONPATH=/work/src`.

- [ ] **Step 1: Write config/toolchain.lock.toml**

Start from `$DONOR/config/toolchain.lock.toml` (it already carries rclone 1.75.0 and proton-drive 0.8.0 with checksums). Replace the `[python]` table and add `age` and `task`:

```toml
[python]
version = "3.13.7"
image = "python:3.13.7-slim-bookworm"
# Resolve with: docker buildx imagetools inspect python:3.13.7-slim-bookworm --format '{{json .Manifest.Digest}}'
digest = "sha256:REPLACE_WITH_RESOLVED_DIGEST"

[python.packages]
requests = "2.34.2"
certifi = "2026.7.22"
charset-normalizer = "3.5.1"
idna = "3.19"
urllib3 = "2.7.0"

[python.test_packages]
pytest = "8.4.2"
ruff = "0.16.4"
iniconfig = "2.3.0"
packaging = "26.3"
pluggy = "1.6.0"
pygments = "2.21.0"

[age]
version = "1.2.1"
base_url = "https://github.com/FiloSottile/age/releases/download/v1.2.1"

[age.linux_amd64]
archive = "age-v1.2.1-linux-amd64.tar.gz"
sha256 = "REPLACE: curl -sL <base_url>/<archive> | sha256sum"

[age.linux_arm64]
archive = "age-v1.2.1-linux-arm64.tar.gz"
sha256 = "REPLACE: curl -sL <base_url>/<archive> | sha256sum"

[task]
version = "3.45.4"
base_url = "https://github.com/go-task/task/releases/download/v3.45.4"

[task.linux_amd64]
archive = "task_linux_amd64.tar.gz"
sha256 = "REPLACE from task_checksums.txt in the release"

[task.linux_arm64]
archive = "task_linux_arm64.tar.gz"
sha256 = "REPLACE from task_checksums.txt in the release"
```

The executor resolves each `REPLACE` value from the named source before building; the image build fails loudly on any mismatch, which is the check. If v3.45.4 of go-task does not exist, use the latest v3 release and record it.

- [ ] **Step 2: Write docker/Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1.7
# Toolbox for dropbox-mirror. The repo is bind-mounted at /work; this image holds
# the toolchain only, pinned by config/toolchain.lock.toml.
FROM python:3.13.7-slim-bookworm@sha256:REPLACE_WITH_RESOLVED_DIGEST AS fetch

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

COPY config/toolchain.lock.toml /tmp/lock.toml

# Architecture is read from the image itself: BuildKit sets TARGETARCH, Apple
# container does not, and a defaulted arg would install amd64 binaries into an
# arm64 image.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64|arm64) ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac; \
    lock() { python -c "import functools,sys,tomllib; x=tomllib.load(open('/tmp/lock.toml','rb')); print(functools.reduce(lambda d,k: d[k], sys.argv[1].split('.'), x))" "$1"; }; \
    v="$(lock rclone.version)"; \
    curl -fsSL "$(lock rclone.base_url)/$(lock rclone.linux_${arch}.archive)" -o /tmp/rclone.zip; \
    echo "$(lock rclone.linux_${arch}.sha256)  /tmp/rclone.zip" | sha256sum -c -; \
    unzip -q /tmp/rclone.zip -d /tmp/rclone; \
    install -m 0755 "/tmp/rclone/rclone-v${v}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    curl -fsSL "$(lock proton_drive_cli.linux_${arch}.url)" -o /tmp/proton-drive; \
    echo "$(lock proton_drive_cli.linux_${arch}.sha512)  /tmp/proton-drive" | sha512sum -c -; \
    install -m 0755 /tmp/proton-drive /usr/local/bin/proton-drive; \
    curl -fsSL "$(lock age.base_url)/$(lock age.linux_${arch}.archive)" -o /tmp/age.tgz; \
    echo "$(lock age.linux_${arch}.sha256)  /tmp/age.tgz" | sha256sum -c -; \
    tar -xzf /tmp/age.tgz -C /tmp; \
    install -m 0755 /tmp/age/age /tmp/age/age-keygen /usr/local/bin/; \
    curl -fsSL "$(lock task.base_url)/$(lock task.linux_${arch}.archive)" -o /tmp/task.tgz; \
    echo "$(lock task.linux_${arch}.sha256)  /tmp/task.tgz" | sha256sum -c -; \
    tar -xzf /tmp/task.tgz -C /tmp task; \
    install -m 0755 /tmp/task /usr/local/bin/task

FROM python:3.13.7-slim-bookworm@sha256:REPLACE_WITH_RESOLVED_DIGEST AS toolbox

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /usr/local/bin/rclone /usr/local/bin/proton-drive \
     /usr/local/bin/age /usr/local/bin/age-keygen /usr/local/bin/task /usr/local/bin/
COPY config/toolchain.lock.toml /etc/toolchain.lock.toml

RUN python - <<'PY'
import subprocess, tomllib
lock = tomllib.load(open("/etc/toolchain.lock.toml", "rb"))
pins = [f"{k}=={v}" for k, v in lock["python"]["packages"].items()]
pins += [f"{k}=={v}" for k, v in lock["python"]["test_packages"].items()]
subprocess.check_call(["pip", "install", "--no-cache-dir", "--no-deps", *pins])
PY

RUN python - <<'PY'
import importlib.metadata, platform, subprocess, tomllib
lock = tomllib.load(open("/etc/toolchain.lock.toml", "rb"))
assert platform.python_version() == lock["python"]["version"]
for table in ("packages", "test_packages"):
    for name, version in lock["python"][table].items():
        assert importlib.metadata.version(name) == version, name
first = lambda argv: subprocess.check_output(argv, text=True).splitlines()[0]
assert first(["rclone", "version"]) == "rclone v" + lock["rclone"]["version"]
assert "@" + lock["proton_drive_cli"]["version"] in first(["proton-drive", "version"])
assert lock["age"]["version"] in first(["age", "--version"])
assert lock["task"]["version"] in subprocess.check_output(["task", "--version"], text=True)
PY

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/work/src \
    MIGRATOR_TOOLCHAIN_LOCK=/etc/toolchain.lock.toml \
    PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
    PROTON_DRIVE_CACHE_DIR=/work/.run/session \
    PROTON_DRIVE_LOG_LEVEL=INFO
# Non-secret rclone remote literals. They live here, not in the Taskfile, so that every
# command in the toolbox sees them whether or not an inner `task` wraps it. Credentials
# for both remotes arrive by name from 1Password (op.env). Never secrets: `op run`
# masks every value it resolves, and masking "auto" would corrupt ordinary output.
ENV RCLONE_CONFIG_DROPBOX_TYPE=dropbox \
    RCLONE_CONFIG_R2_TYPE=s3 \
    RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
    RCLONE_CONFIG_R2_REGION=auto \
    RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true
WORKDIR /work
```

- [ ] **Step 3: Write Taskfile.yml (toolbox and checks only)**

```yaml
# dropbox-mirror: nightly Dropbox -> Proton Drive mirror run from GitHub Actions.
#
# Invariant: every step runs inside the toolbox image, on a laptop and in CI
# alike, and R2 holds the only record of how far the mirror has got. Secrets
# reach processes by name from 1Password (op.env); nothing is written to disk.
# Design: docs/superpowers/specs/2026-09-03-dropbox-mirror-design.md
version: '3'

vars:
  IMAGE: dropbox-mirror:toolbox
  ENGINE:
    sh: |
      if [ -n "$ENGINE" ]; then echo "$ENGINE"
      elif command -v container >/dev/null 2>&1 && container system status >/dev/null 2>&1; then echo container
      elif command -v docker >/dev/null 2>&1; then echo docker
      elif command -v container >/dev/null 2>&1; then echo container
      else echo docker
      fi
  # Every name in op.env crosses into the container by name, never by value.
  PASS_ENV:
    sh: grep -v '^#' op.env | sed -n 's/=.*//p' | sed 's/^/-e /' | tr '\n' ' '; printf -- '-e RUN_BUDGET_MIN -e RECONCILE -e ENGINE -e MIRROR_VERBOSE -e GITHUB_RUN_ID'

tasks:
  default:
    desc: Print the menu
    silent: true
    # ponytail: hand-maintained listing -- go-task can neither group nor annotate,
    # so a new task means a line below. `task --list` stays the generated view.
    cmds:
      - |
        b=$(printf '\033[1m'); c=$(printf '\033[1;36m'); d=$(printf '\033[2m'); r=$(printf '\033[0m')
        printf '%s\n' \
          "" \
          "${b}dropbox-mirror${r}   ${d}nightly Dropbox -> Proton Drive mirror; R2 holds the state${r}" \
          "" \
          "${c}checks -- offline, no credentials${r}" \
          "  task test                 pytest inside the toolbox" \
          "  task lint                 ruff check + format check" \
          "  task fmt                  ruff format" \
          "" \
          "${c}toolbox${r}" \
          "  task image                Build the toolbox image (no-op while it exists)" \
          "  task image-clean          Remove it so the next task image rebuilds" \
          "  task run -- <cmd>         Run any command in the toolbox with the repo at /work" \
          "  task op -- <cmd>          Same, with secrets from 1Password via op.env" \
          ""

  image:
    desc: Build the toolbox image
    run: once
    # Image presence is the freshness check; a Dockerfile edit needs `task image-clean`.
    status:
      - '{{.ENGINE}} image inspect {{.IMAGE}} >/dev/null 2>&1'
    cmds:
      - '{{.ENGINE}} build -t {{.IMAGE}} -f docker/Dockerfile .'

  image-clean:
    desc: Remove the toolbox image
    cmds:
      - '{{.ENGINE}} image rm {{.IMAGE}}'

  run:
    desc: 'Run any command in the toolbox image, e.g. task run -- task test'
    deps: [image]
    cmds:
      - >-
        {{.ENGINE}} run --rm -i $(test -t 0 && test -t 1 && echo -t || true)
        --user $(id -u):$(id -g)
        -v "{{.ROOT_DIR}}":/work -w /work
        -e HOME=/tmp
        {{.PASS_ENV}}
        {{.IMAGE}} {{.CLI_ARGS}}

  op:
    desc: 'Run a command in the toolbox with secrets from 1Password'
    cmds:
      - op run --env-file=op.env -- task run -- {{.CLI_ARGS}}

  test:
    desc: Run the test suite inside the toolbox
    cmds:
      - task: run
        vars: { CLI_ARGS: 'python -m pytest {{.CLI_ARGS}}' }

  lint:
    desc: ruff check and format check
    cmds:
      - task: run
        vars: { CLI_ARGS: 'sh -c "ruff check src tests && ruff format --check src tests"' }

  fmt:
    desc: ruff format
    cmds:
      - task: run
        vars: { CLI_ARGS: 'ruff format src tests' }
```

Create an empty `op.env` placeholder line so `PASS_ENV` resolves: `printf '# op:// references, filled in Task 18\n' > op.env`.

- [ ] **Step 4: Build and run the checks**

Run: `task image && task test && task lint`
Expected: image builds with every checksum verified; pytest passes the Task 1 tests; ruff is clean (run `task fmt` first if the donor files need formatting).

- [ ] **Step 5: Commit**

```bash
git add docker config/toolchain.lock.toml Taskfile.yml op.env
git commit -m "chore(toolbox): pinned toolchain image and task wrappers"
```

---

### Task 3: Configuration schema

**Files:**
- Create: `src/migrator/config.py`, `config/mirror.toml`, `tests/test_config.py`

**Interfaces:**
- Consumes: `guards.validate_dropbox_scope`, `validate_proton_cli_path`, `validate_executable`, `hashing.sha256_file`.
- Produces: `Config`, `Mirror`, `Dropbox`, `Rclone`, `Proton`, `Budget`, `Reconcile`, `ConfigError`, `load_config(path) -> Config`, `Budget.batch_bytes`, `Budget.ceiling_bytes`, `Budget.headroom_bytes`.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
from __future__ import annotations

import pytest

from migrator.config import ConfigError, load_config

GOOD = """
[mirror]
id = "test"
[dropbox]
expected_account_id = "dbid:abc"
[proton]
expected_destination_uid = "uid-12345678"
"""


def _write(tmp_path, text):
    path = tmp_path / "mirror.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_and_derived_bytes(tmp_path):
    cfg = load_config(_write(tmp_path, GOOD))
    assert cfg.rclone.tps_limit == 10
    assert cfg.budget.batch_gb == 4
    assert cfg.budget.batch_files == 5000
    assert cfg.budget.batch_bytes == 4 * 1024**3
    assert cfg.budget.run_budget_minutes == 165
    assert cfg.budget.ceiling_gb == 4000
    assert cfg.proton.destination == "/my-files/Dropbox"
    assert cfg.reconcile.weekday == 0


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(_write(tmp_path, GOOD + "\n[budget]\nmax_batches = 4\n"))


def test_unknown_table_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown top-level"):
        load_config(_write(tmp_path, GOOD + "\n[safety]\nx = 1\n"))


def test_required_identity_guards(tmp_path):
    with pytest.raises(ConfigError, match="expected_account_id"):
        load_config(_write(tmp_path, GOOD.replace('"dbid:abc"', '""')))
    with pytest.raises(ConfigError, match="expected_destination_uid"):
        load_config(_write(tmp_path, GOOD.replace('"uid-12345678"', '"short"')))


def test_numeric_floors(tmp_path):
    with pytest.raises(ConfigError, match="batch_gb"):
        load_config(_write(tmp_path, GOOD + "\n[budget]\nbatch_gb = 0\n"))
    with pytest.raises(ConfigError, match="listing_floor_ratio"):
        load_config(_write(tmp_path, GOOD + "\n[budget]\nlisting_floor_ratio = 1.5\n"))
    with pytest.raises(ConfigError, match="weekday"):
        load_config(_write(tmp_path, GOOD + "\n[reconcile]\nweekday = 7\n"))
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_config.py`
Expected: FAIL with `ModuleNotFoundError: migrator.config`.

- [ ] **Step 3: Write config.py**

Copy `_convert`, `_section`, `_enum`, `_positive`, `_nonnegative`, `_positive_int`, `ConfigError`, and the `T = TypeVar("T")` line from `$DONOR/src/migrator/config.py` verbatim. Replace everything else with:

```python
@dataclass(frozen=True)
class Mirror:
    id: str


@dataclass(frozen=True)
class Dropbox:
    expected_account_id: str
    root: str = ""
    api_base_url: str = "https://api.dropboxapi.com/2"
    token_url: str = "https://api.dropboxapi.com/oauth2/token"
    timeout_seconds: float = 180
    page_limit: int = 2000
    minimum_call_interval_seconds: float = 0.1
    max_attempts: int = 12
    initial_backoff_seconds: float = 2
    maximum_backoff_seconds: float = 300


@dataclass(frozen=True)
class Rclone:
    remote: str = "dropbox"
    executable: str = "rclone"
    tps_limit: float = 10
    tps_burst: int = 1
    transfers: int = 4
    retries: int = 8
    low_level_retries: int = 20
    list_timeout_seconds: float = 300
    transfer_timeout_seconds: float = 14400


@dataclass(frozen=True)
class Proton:
    expected_destination_uid: str
    executable: str = "proton-drive"
    destination: str = "/my-files/Dropbox"
    list_max_attempts: int = 8
    download_max_attempts: int = 8
    initial_backoff_seconds: float = 3
    maximum_backoff_seconds: float = 120
    command_timeout_seconds: float = 300
    transfer_timeout_seconds: float = 14400


@dataclass(frozen=True)
class Budget:
    batch_gb: float = 4
    batch_files: int = 5000
    run_budget_minutes: int = 165
    ceiling_gb: float = 4000
    disk_headroom_gb: float = 1
    listing_floor_ratio: float = 0.5

    @property
    def batch_bytes(self) -> int:
        return round(self.batch_gb * 1024**3)

    @property
    def ceiling_bytes(self) -> int:
        return round(self.ceiling_gb * 1024**3)

    @property
    def headroom_bytes(self) -> int:
        return round(self.disk_headroom_gb * 1024**3)


@dataclass(frozen=True)
class Reconcile:
    weekday: int = 0  # the first run that starts on this UTC weekday walks Proton


@dataclass(frozen=True)
class Config:
    mirror: Mirror
    dropbox: Dropbox
    rclone: Rclone
    proton: Proton
    budget: Budget
    reconcile: Reconcile
    source_file: Path
    source_sha256: str


_MIRROR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECTIONS = {"mirror", "dropbox", "rclone", "proton", "budget", "reconcile"}


def load_config(path: str | Path) -> Config:
    from .hashing import sha256_file

    source = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(source.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {source}: {exc}") from exc
    unknown = sorted(set(data) - SECTIONS)
    if unknown:
        raise ConfigError(f"unknown top-level tables: {', '.join(unknown)}")
    base = source.parent
    cfg = Config(
        mirror=_section(Mirror, data.get("mirror", {}), base, "mirror"),
        dropbox=_section(Dropbox, data.get("dropbox", {}), base, "dropbox"),
        rclone=_section(Rclone, data.get("rclone", {}), base, "rclone"),
        proton=_section(Proton, data.get("proton", {}), base, "proton"),
        budget=_section(Budget, data.get("budget", {}), base, "budget"),
        reconcile=_section(Reconcile, data.get("reconcile", {}), base, "reconcile"),
        source_file=source,
        source_sha256=sha256_file(source),
    )
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    if not _MIRROR_ID.fullmatch(cfg.mirror.id):
        raise ConfigError("mirror.id must be alphanumeric with dot, underscore, hyphen")
    if not re.fullmatch(r"dbid:[A-Za-z0-9_-]+", cfg.dropbox.expected_account_id):
        raise ConfigError("dropbox.expected_account_id must be a full dbid: identifier")
    if len(cfg.proton.expected_destination_uid.strip()) < 8:
        raise ConfigError("proton.expected_destination_uid must be at least 8 characters")
    if not cfg.rclone.remote or ":" in cfg.rclone.remote:
        raise ConfigError("rclone.remote must be a bare remote name")
    try:
        validate_executable(cfg.rclone.executable, label="rclone.executable")
        validate_executable(cfg.proton.executable, label="proton.executable")
        validate_dropbox_scope(cfg.dropbox.root)
        validate_proton_cli_path(cfg.proton.destination, label="proton.destination")
    except GuardError as exc:
        raise ConfigError(str(exc)) from exc
    if type(cfg.dropbox.page_limit) is not int or not 1 <= cfg.dropbox.page_limit <= 2000:
        raise ConfigError("dropbox.page_limit must be in 1..2000")
    _positive(cfg.dropbox.timeout_seconds, "dropbox.timeout_seconds")
    _nonnegative(cfg.dropbox.minimum_call_interval_seconds, "dropbox.minimum_call_interval_seconds")
    _positive_int(cfg.dropbox.max_attempts, "dropbox.max_attempts")
    _nonnegative(cfg.dropbox.initial_backoff_seconds, "dropbox.initial_backoff_seconds")
    _positive(cfg.dropbox.maximum_backoff_seconds, "dropbox.maximum_backoff_seconds")
    _positive(cfg.rclone.tps_limit, "rclone.tps_limit")
    for name in ("tps_burst", "transfers", "retries", "low_level_retries"):
        _positive_int(getattr(cfg.rclone, name), f"rclone.{name}")
    _positive(cfg.rclone.list_timeout_seconds, "rclone.list_timeout_seconds")
    _positive(cfg.rclone.transfer_timeout_seconds, "rclone.transfer_timeout_seconds")
    _positive_int(cfg.proton.list_max_attempts, "proton.list_max_attempts")
    _positive_int(cfg.proton.download_max_attempts, "proton.download_max_attempts")
    _nonnegative(cfg.proton.initial_backoff_seconds, "proton.initial_backoff_seconds")
    _positive(cfg.proton.maximum_backoff_seconds, "proton.maximum_backoff_seconds")
    _positive(cfg.proton.command_timeout_seconds, "proton.command_timeout_seconds")
    _positive(cfg.proton.transfer_timeout_seconds, "proton.transfer_timeout_seconds")
    _positive(cfg.budget.batch_gb, "budget.batch_gb")
    _positive_int(cfg.budget.batch_files, "budget.batch_files")
    _positive_int(cfg.budget.run_budget_minutes, "budget.run_budget_minutes")
    _positive(cfg.budget.ceiling_gb, "budget.ceiling_gb")
    _nonnegative(cfg.budget.disk_headroom_gb, "budget.disk_headroom_gb")
    if not 0 < cfg.budget.listing_floor_ratio <= 1:
        raise ConfigError("budget.listing_floor_ratio must be in (0, 1]")
    if type(cfg.reconcile.weekday) is not int or not 0 <= cfg.reconcile.weekday <= 6:
        raise ConfigError("reconcile.weekday must be 0 (Monday) to 6 (Sunday)")
```

Imports at the top: `re`, `tomllib`, `dataclass`, `fields`, `isfinite`, `Path`, `Any`, `TypeVar`, `get_type_hints`, and from `.guards`: `GuardError`, `validate_dropbox_scope`, `validate_executable`, `validate_proton_cli_path`.

- [ ] **Step 4: Write config/mirror.toml**

```toml
# The one behavior input. Runtime paths and secrets come from the environment.
[mirror]
id = "dropbox-mirror"

[dropbox]
expected_account_id = "dbid:REPLACE_AT_BOOTSTRAP"
root = ""
page_limit = 2000
minimum_call_interval_seconds = 0.1

[rclone]
remote = "dropbox"
tps_limit = 10
tps_burst = 1
transfers = 4

[proton]
destination = "/my-files/Dropbox"
expected_destination_uid = "REPLACE_AT_BOOTSTRAP"

[budget]
batch_gb = 4
# ponytail: the round-trip downloads one file per CLI process, seconds each, so a batch
# is bounded by its file count as much as by its bytes. 5,000 fits the budget only when
# the measured cost is under about 1.8 s per file (README, first-run checks); a batch
# that cannot finish never checkpoints and repeats every run. The upgrade path is a
# recursive folder download when a batch covers a whole folder.
batch_files = 5000
run_budget_minutes = 165
ceiling_gb = 4000
disk_headroom_gb = 1
listing_floor_ratio = 0.5

[reconcile]
weekday = 0
```

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_config.py`
Expected: PASS (5 tests). Note the placeholder `expected_destination_uid` is 20 characters, so the committed file also loads.

- [ ] **Step 6: Commit**

```bash
git add src/migrator/config.py config/mirror.toml tests/test_config.py
git commit -m "feat(config): strict mirror configuration schema"
```

---

### Task 4: Runtime environment, work paths, and the state database

**Files:**
- Create: `src/migrator/env.py`, `src/migrator/paths.py`, `src/migrator/state.py` (from donor, pruned and extended), `tests/conftest.py`, `tests/test_env.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: `Config` (Task 3), `atomic.write_json`, `hashing.sha256_file`, `logging.Event`, `utc_now`.
- Produces: `Runtime`, `WorkPaths`, `State` with `start_run`, `current_run`, `update_run`, `finish_run`, `mirror_totals`, `snapshot_to`, plus the donor methods listed in the shared interfaces; conftest fixtures `runtime_factory`, `config_factory`, `state_context` yielding `(cfg, paths, state, logger, runtime)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_env.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from migrator.env import Runtime

BASE = {
    "MIRROR_R2_BUCKET": "b",
    "MIRROR_AGE_IDENTITY": "AGE-SECRET-KEY-1TEST",
    "MIRROR_HEALTHCHECK_URL": "https://hc/ping/x",
    "MIRROR_DROPBOX_APP_KEY": "k",
    "MIRROR_DROPBOX_APP_SECRET": "s",
    "MIRROR_DROPBOX_REFRESH_TOKEN": "r",
    "RCLONE_CONFIG_DROPBOX_TOKEN": '{"access_token":"t"}',
    "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": "r2s",
}


def test_defaults_and_secrets():
    runtime = Runtime.from_environ(BASE)
    assert runtime.work_dir == Path(".run")
    assert runtime.config_path == Path("config/mirror.toml")
    assert runtime.run_epoch is None
    assert runtime.budget_override is None
    assert runtime.reconcile is False
    assert set(runtime.secrets()) == {
        "AGE-SECRET-KEY-1TEST", "https://hc/ping/x", "s", "r", '{"access_token":"t"}', "r2s"
    }


def test_overrides_and_host():
    env = {**BASE, "MIRROR_RUN_EPOCH": "1700000000", "RUN_BUDGET_MIN": "30",
           "RECONCILE": "true", "GITHUB_RUN_ID": "42", "MIRROR_WORK_DIR": "/tmp/w"}
    runtime = Runtime.from_environ(env)
    assert runtime.run_epoch == 1700000000
    assert runtime.budget_override == 30
    assert runtime.reconcile is True
    assert runtime.host == "github:42"
    assert runtime.work_dir == Path("/tmp/w")


def test_missing_secret_is_empty_not_error():
    runtime = Runtime.from_environ({})
    assert runtime.r2_bucket == ""
    assert runtime.secrets() == []


def test_bad_epoch_rejected():
    with pytest.raises(ValueError, match="MIRROR_RUN_EPOCH"):
        Runtime.from_environ({**BASE, "MIRROR_RUN_EPOCH": "noon"})
```

`tests/test_state.py`:

```python
from __future__ import annotations

import sqlite3

import pytest

from migrator.state import State


def test_run_lifecycle(state_context):
    _, _, state, _, _ = state_context
    with pytest.raises(RuntimeError):
        state.current_run()
    first = state.start_run(start_epoch=100, hour_utc=2, weekday=0, budget_minutes=165,
                            host="test", reconcile=False)
    second = state.start_run(start_epoch=200, hour_utc=3, weekday=1, budget_minutes=10,
                             host="test", reconcile=True)
    rows = {r["id"]: r for r in state.connection.execute("SELECT * FROM runs")}
    assert rows[first]["status"] == "INTERRUPTED"
    assert rows[second]["status"] == "RUNNING"
    assert state.current_run()["id"] == second
    state.update_run(second, planned_batches=3, remaining_batches=1, chain=1)
    assert state.current_run()["remaining_batches"] == 1
    state.finish_run(second, "SUCCESS")
    with pytest.raises(RuntimeError):
        state.current_run()


def test_update_run_rejects_unknown_column(state_context):
    _, _, state, _, _ = state_context
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1,
                             host="t", reconcile=False)
    with pytest.raises(ValueError):
        state.update_run(run_id, nonsense=1)


def test_mirror_totals_and_snapshot(state_context, tmp_path):
    _, _, state, _, _ = state_context
    assert state.mirror_totals() == (0, 0)
    with state.connection:
        state.connection.execute(
            """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1,
               sha256, proton_uid, run_id, mirrored_at) VALUES ('/a.txt','/a.txt',5,'h','s1','s2','u',1,'now')"""
        )
    assert state.mirror_totals() == (1, 5)
    target = tmp_path / "snap.sqlite"
    state.snapshot_to(target)
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT COUNT(*) FROM mirror_objects").fetchone()[0] == 1


def test_phase_attempt_marks_prior_running_attempt_interrupted(state_context):
    _, _, state, _, _ = state_context
    first = state.start_phase(10, "10_inventory", apply=False, inputs={}, tool_versions={})
    second = state.start_phase(10, "10_inventory", apply=False, inputs={}, tool_versions={})
    assert second != first
    old = state.connection.execute("SELECT status FROM phase_runs WHERE id=?", (first,)).fetchone()
    assert old["status"] == "INTERRUPTED"


def test_schema_has_no_migration_only_tables(state_context):
    _, _, state, _, _ = state_context
    names = {r["name"] for r in state.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "canonical_objects" not in names and "delta_items" not in names
    assert {"runs", "mirror_objects", "batches", "batch_items", "deletions", "delta_changed",
            "delta_deleted", "proton_nodes", "dropbox_objects", "events", "commands"} <= names
```

`tests/conftest.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from migrator.config import Budget, Config, Dropbox, Mirror, Proton, Rclone, Reconcile
from migrator.env import Runtime
from migrator.logging import RunLogger
from migrator.paths import WorkPaths
from migrator.state import State

TEST_ENV = {
    "MIRROR_R2_BUCKET": "test-bucket",
    "MIRROR_AGE_IDENTITY": "AGE-SECRET-KEY-1TEST",
    "MIRROR_HEALTHCHECK_URL": "https://hc.example/ping/x",
    "MIRROR_DROPBOX_APP_KEY": "app-key",
    "MIRROR_DROPBOX_APP_SECRET": "app-secret",
    "MIRROR_DROPBOX_REFRESH_TOKEN": "refresh-token",
    "RCLONE_CONFIG_DROPBOX_TOKEN": '{"access_token":"rclone-token"}',
    "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": "r2-secret",
}


@pytest.fixture
def runtime_factory():
    def make(tmp_path: Path, **overrides) -> Runtime:
        env = {**TEST_ENV, "MIRROR_WORK_DIR": str(tmp_path / "run"),
               "MIRROR_CONFIG": str(tmp_path / "mirror.toml"), "MIRROR_RUN_EPOCH": "1700000000"}
        env.update({k: str(v) for k, v in overrides.items()})
        return Runtime.from_environ(env)
    return make


@pytest.fixture
def config_factory():
    def make(tmp_path: Path, **overrides) -> Config:
        source = tmp_path / "mirror.toml"
        source.write_text("# test\n", encoding="utf-8")
        return Config(
            mirror=Mirror("test"),
            dropbox=overrides.get("dropbox", Dropbox(expected_account_id="dbid:test-account",
                                                     minimum_call_interval_seconds=0,
                                                     max_attempts=3, initial_backoff_seconds=0)),
            rclone=overrides.get("rclone", Rclone(retries=2)),
            proton=overrides.get("proton", Proton(expected_destination_uid="uid-destination",
                                                  initial_backoff_seconds=0)),
            budget=overrides.get("budget", Budget()),
            reconcile=overrides.get("reconcile", Reconcile()),
            source_file=source,
            source_sha256="test-config-hash",
        )
    return make


@pytest.fixture
def state_context(tmp_path, config_factory, runtime_factory):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    state = State(paths.state_db, cfg.mirror.id)
    state.initialize_migration(cfg.source_file, cfg.source_sha256)
    logger = RunLogger(paths.logs, secrets=runtime.secrets(), console=False, sink=state.record_event)
    try:
        yield cfg, paths, state, logger, runtime
    finally:
        state.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_env.py tests/test_state.py`
Expected: FAIL with `ModuleNotFoundError` for `migrator.env`.

- [ ] **Step 3: Write env.py**

```python
from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _int(environ: Mapping[str, str], name: str) -> int | None:
    raw = environ.get(name, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Runtime:
    work_dir: Path
    config_path: Path
    run_epoch: int | None
    budget_override: int | None
    reconcile: bool
    verbose: bool
    r2_bucket: str
    age_identity: str
    healthcheck_url: str
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    rclone_dropbox_token: str
    r2_secret: str
    host: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Runtime:
        env = os.environ if environ is None else environ
        run_id = env.get("GITHUB_RUN_ID", "")
        return cls(
            work_dir=Path(env.get("MIRROR_WORK_DIR", ".run")),
            config_path=Path(env.get("MIRROR_CONFIG", "config/mirror.toml")),
            run_epoch=_int(env, "MIRROR_RUN_EPOCH"),
            budget_override=_int(env, "RUN_BUDGET_MIN"),
            reconcile=env.get("RECONCILE", "").lower() == "true",
            verbose=env.get("MIRROR_VERBOSE", "") == "1",
            r2_bucket=env.get("MIRROR_R2_BUCKET", ""),
            age_identity=env.get("MIRROR_AGE_IDENTITY", ""),
            healthcheck_url=env.get("MIRROR_HEALTHCHECK_URL", ""),
            dropbox_app_key=env.get("MIRROR_DROPBOX_APP_KEY", ""),
            dropbox_app_secret=env.get("MIRROR_DROPBOX_APP_SECRET", ""),
            dropbox_refresh_token=env.get("MIRROR_DROPBOX_REFRESH_TOKEN", ""),
            rclone_dropbox_token=env.get("RCLONE_CONFIG_DROPBOX_TOKEN", ""),
            r2_secret=env.get("RCLONE_CONFIG_R2_SECRET_ACCESS_KEY", ""),
            host=f"github:{run_id}" if run_id else socket.gethostname(),
        )

    def secrets(self) -> list[str]:
        values = (
            self.age_identity, self.healthcheck_url, self.dropbox_app_secret,
            self.dropbox_refresh_token, self.rclone_dropbox_token, self.r2_secret,
        )
        return [value for value in values if value]
```

- [ ] **Step 4: Write paths.py**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .env import Runtime


@dataclass(frozen=True)
class WorkPaths:
    root: Path

    @classmethod
    def from_runtime(cls, runtime: Runtime) -> WorkPaths:
        return cls(root=runtime.work_dir.resolve())

    @property
    def state_db(self) -> Path:
        return self.root / "state.sqlite"

    @property
    def clock(self) -> Path:
        return self.root / "clock.json"

    @property
    def session(self) -> Path:
        return self.root / "session"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def roundtrip(self) -> Path:
        return self.root / "roundtrip"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def rclone_config(self) -> Path:
        return self.root / "rclone.conf"

    @property
    def report(self) -> Path:
        return self.root / "report.md"

    @property
    def chain(self) -> Path:
        return self.root / "chain"

    @property
    def age_key(self) -> Path:
        return self.root / "age.key"

    def ensure(self) -> None:
        for directory in (self.root, self.session, self.staging, self.roundtrip,
                          self.logs, self.logs / "phases"):
            directory.mkdir(parents=True, exist_ok=True)
        self.session.chmod(0o700)
        self.rclone_config.touch(exist_ok=True)
```

- [ ] **Step 5: Write state.py**

Copy `$DONOR/src/migrator/state.py`, then:

1. Set `SCHEMA_VERSION = 1`. Delete `_migrate_schema` and its call.
2. In `SCHEMA`, delete these tables and their indexes: `local_snapshots`, `local_objects`, `local_verification`, `recovery_items`, `normalization_items`, `canonical_snapshots`, `canonical_objects`, `canonical_provenance`, `delta_items`, `upload_attempts`, `source_stability_checks`, `source_stability_diffs`, `proton_download_items`. Keep `schema_info`, `migrations`, `phase_runs`, `artifacts`, `commands`, `events`, `identity_observations`, `dropbox_inventory_runs`, `dropbox_pages`, `dropbox_objects`, `rclone_inventory_runs`, `rclone_folders`, `rclone_objects`, `source_reconciliation`, `proton_snapshots`, `proton_folders`, `proton_nodes`.
3. Append to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    start_epoch INTEGER NOT NULL,
    hour_utc INTEGER NOT NULL,
    weekday INTEGER NOT NULL,
    budget_minutes INTEGER NOT NULL,
    host TEXT NOT NULL,
    reconcile INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    chain INTEGER NOT NULL DEFAULT 0,
    inventory_id INTEGER,
    planned_batches INTEGER,
    remaining_batches INTEGER,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS mirror_objects (
    path_lower TEXT PRIMARY KEY,
    path_display TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    sha1 TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    proton_uid TEXT,
    run_id INTEGER NOT NULL,
    mirrored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS delta_changed (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    path_lower TEXT NOT NULL,
    path_display TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(run_id, path_lower)
);

CREATE TABLE IF NOT EXISTS delta_deleted (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    path_lower TEXT NOT NULL,
    path_display TEXT NOT NULL,
    proton_uid TEXT,
    PRIMARY KEY(run_id, path_lower)
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    number INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    file_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, number)
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    path_lower TEXT NOT NULL,
    path_display TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    sha1 TEXT,
    sha256 TEXT,
    proton_uid TEXT,
    cli_path TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(batch_id, path_lower)
);

CREATE TABLE IF NOT EXISTS deletions (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    path_lower TEXT NOT NULL,
    path_display TEXT NOT NULL,
    proton_uid TEXT,
    status TEXT NOT NULL,
    trashed_at TEXT,
    PRIMARY KEY(run_id, path_lower)
);
```

4. In `initialize_migration`, replace the `if existing and existing["config_sha256"] != config_sha256: raise ...` block with an update, because tuning `mirror.toml` between runs is normal:

```python
            if existing and existing["config_sha256"] != config_sha256:
                self.connection.execute(
                    "UPDATE migrations SET config_sha256=?, config_path=? WHERE migration_id=?",
                    (config_sha256, str(config_path), self.migration_id),
                )
```

5. In `latest_completed_id`, set `allowed = {"dropbox_inventory_runs", "rclone_inventory_runs", "proton_snapshots"}` and the purpose set to the same three.
6. Delete `phase_artifacts_valid`, `mark_phase_stale`, `mark_stale_from`, `run_projection`, `write_run_json` (nothing calls them; `status` gets its own projection in Task 9).
7. Append these methods to `State`:

```python
    RUN_COLUMNS = frozenset({
        "status", "chain", "inventory_id", "planned_batches", "remaining_batches",
        "completed_at", "reconcile",
    })

    def start_run(self, *, start_epoch: int, hour_utc: int, weekday: int,
                  budget_minutes: int, host: str, reconcile: bool) -> int:
        now = utc_now()
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET status='INTERRUPTED', completed_at=? WHERE status='RUNNING'",
                (now,),
            )
            cursor = self.connection.execute(
                """
                INSERT INTO runs(started_at, start_epoch, hour_utc, weekday, budget_minutes,
                                 host, reconcile, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING')
                """,
                (now, start_epoch, hour_utc, weekday, budget_minutes, host, int(reconcile)),
            )
        return int(cursor.lastrowid)

    def current_run(self) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM runs WHERE status='RUNNING' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise RuntimeError("no run is in progress; the state step starts one")
        return row

    def update_run(self, run_id: int, **columns: Any) -> None:
        unknown = set(columns) - self.RUN_COLUMNS
        if unknown:
            raise ValueError(f"unknown runs columns: {sorted(unknown)}")
        assignments = ", ".join(f"{name}=?" for name in columns)
        with self.connection:
            self.connection.execute(
                f"UPDATE runs SET {assignments} WHERE id=?", (*columns.values(), run_id)
            )

    def finish_run(self, run_id: int, status: str) -> None:
        self.update_run(run_id, status=status, completed_at=utc_now())

    def mirror_totals(self) -> tuple[int, int]:
        row = self.connection.execute(
            "SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM mirror_objects"
        ).fetchone()
        return int(row["files"]), int(row["bytes"])

    def snapshot_to(self, target: Path) -> None:
        if target.exists():
            target.unlink()
        self.connection.commit()  # VACUUM refuses to run inside an open transaction
        self.connection.execute("VACUUM INTO ?", (str(target),))
```

8. Dump the donor DDL the later tasks lean on and reconcile the plan against it now, not when a test fails with `OperationalError`: `python -c "from migrator.state import SCHEMA; print(SCHEMA)"` and read the columns of `dropbox_inventory_runs`, `dropbox_pages`, `dropbox_objects`, `rclone_inventory_runs`, `rclone_folders`, `rclone_objects`, `proton_snapshots`, `proton_folders`, `proton_nodes`, `events`, `commands`, `phase_runs`. Then check `start_phase`'s keyword arguments (this plan uses both `tool_versions` and `command_parameters`), `record_artifact`'s signature, and the attributes of the items `filesystem.walk_tree` yields. Every seed helper and query in Tasks 10 through 17 was written from memory of the donor; fix the plan text where it differs before those tasks run.

- [ ] **Step 6: Run tests**

Run: `task test -- tests/test_env.py tests/test_state.py`
Expected: PASS (9 tests).

- [ ] **Step 7: Commit**

```bash
git add src/migrator/env.py src/migrator/paths.py src/migrator/state.py tests/conftest.py tests/test_env.py tests/test_state.py
git commit -m "feat(state): runtime env, work paths, and mirror state schema"
```

---

### Task 5: Phase framework, runner, and command-line entry point

**Files:**
- Create: `src/migrator/phases/base.py`, `src/migrator/runner.py`, `src/migrator/__main__.py`, `tests/test_runner.py`

**Interfaces:**
- Consumes: `Runtime`, `WorkPaths`, `Config`, `State`, `RunLogger`.
- Produces: `PhaseContext`, `PhaseResult`, `PhaseError`, `PhaseDefinition(number, command, name, run)`, `PHASES`, `run_phase(command, *, apply, runtime) -> str`, `main(argv) -> int`. Phases are registered in later tasks by adding a line to `PHASES`; until then the registry holds one test-only entry that Task 10 replaces.

- [ ] **Step 1: Write the failing test**

`tests/test_runner.py`:

```python
from __future__ import annotations

import json

import pytest

from migrator import runner
from migrator.phases.base import PhaseContext, PhaseError, PhaseResult


def _register(monkeypatch, fn):
    monkeypatch.setitem(
        runner.PHASES, "probe", runner.PhaseDefinition(99, "probe", "99_probe", fn)
    )


def _prepare(state_context):
    cfg, paths, state, _, runtime = state_context
    state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=False)
    state.close()
    (paths.root / "mirror.toml").write_text("", encoding="utf-8")
    return runtime


def test_run_phase_records_pass_with_run_id(state_context, monkeypatch):
    runtime = _prepare(state_context)
    seen = {}

    def probe(ctx: PhaseContext) -> PhaseResult:
        seen["run_id"] = ctx.run_id
        seen["apply"] = ctx.apply
        return PhaseResult(outputs={"files": 3})

    _register(monkeypatch, probe)
    monkeypatch.setattr(runner, "load_config", lambda _: state_context[0])
    assert runner.run_phase("probe", apply=True, runtime=runtime) == "PASS"
    assert seen == {"run_id": 1, "apply": True}
    from migrator.state import State
    state = State(state_context[1].state_db, "test")
    row = state.latest_phase(99)
    assert row["status"] == "PASS"
    assert json.loads(row["outputs_json"]) == {"files": 3}
    assert json.loads(row["inputs_json"])["run_id"] == 1
    state.close()


def test_run_phase_records_fail_and_reraises(state_context, monkeypatch):
    runtime = _prepare(state_context)

    def probe(ctx: PhaseContext) -> PhaseResult:
        raise PhaseError("listing refused: /Taxes/secret.pdf")

    _register(monkeypatch, probe)
    monkeypatch.setattr(runner, "load_config", lambda _: state_context[0])
    with pytest.raises(PhaseError):
        runner.run_phase("probe", apply=False, runtime=runtime)
    from migrator.state import State
    state = State(state_context[1].state_db, "test")
    assert state.latest_phase(99)["status"] == "FAIL"
    state.close()


def test_main_hides_error_text_unless_verbose(state_context, monkeypatch, capsys):
    from migrator.__main__ import main
    runtime = _prepare(state_context)

    def probe(ctx: PhaseContext) -> PhaseResult:
        raise PhaseError("contains /Taxes/secret.pdf")

    _register(monkeypatch, probe)
    monkeypatch.setattr(runner, "load_config", lambda _: state_context[0])
    monkeypatch.setattr("migrator.__main__.Runtime.from_environ", lambda: runtime)
    assert main(["probe"]) == 1
    err = capsys.readouterr().err
    assert "PhaseError" in err and "secret.pdf" not in err
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_runner.py`
Expected: FAIL with `ModuleNotFoundError: migrator.runner`.

- [ ] **Step 3: Write phases/base.py**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..env import Runtime
from ..logging import RunLogger
from ..paths import WorkPaths
from ..state import State


class PhaseError(RuntimeError):
    """A required gate failed."""


@dataclass
class PhaseResult:
    status: str = "PASS"
    outputs: dict[str, Any] = field(default_factory=dict)
    artifacts: list[tuple[str, Path, int | None]] = field(default_factory=list)

    def artifact(self, role: str, path: Path, row_count: int | None = None) -> PhaseResult:
        self.artifacts.append((role, path, row_count))
        return self


@dataclass
class PhaseContext:
    cfg: Config
    paths: WorkPaths
    state: State
    logger: RunLogger
    apply: bool
    phase_run_id: int
    run_id: int
    runtime: Runtime

    def phase_dir(self, name: str) -> Path:
        directory = self.paths.logs / "phases" / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory
```

- [ ] **Step 4: Write runner.py**

```python
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from .config import load_config
from .env import Runtime
from .logging import RunLogger
from .paths import WorkPaths
from .phases.base import PhaseContext, PhaseError, PhaseResult
from .state import State


@dataclass(frozen=True)
class PhaseDefinition:
    number: int
    command: str
    name: str
    run: Callable[[PhaseContext], PhaseResult]


# Filled in by the phase modules' tasks; Task 10 onward adds one line each.
PHASES: dict[str, PhaseDefinition] = {}


def run_phase(command: str, *, apply: bool, runtime: Runtime) -> str:
    phase = PHASES[command]
    cfg = load_config(runtime.config_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    # The CLI's cache dir must follow MIRROR_WORK_DIR, not the image default.
    os.environ["PROTON_DRIVE_CACHE_DIR"] = str(paths.session)
    os.environ["PROTON_DRIVE_CREDENTIALS_STORE"] = "unsafe_file"
    state = State(paths.state_db, cfg.mirror.id)
    try:
        state.initialize_migration(cfg.source_file, cfg.source_sha256)
        run_id = int(state.current_run()["id"])
        logger = RunLogger(paths.logs, secrets=runtime.secrets(), console=True,
                           sink=state.record_event)
        phase_run_id = state.start_phase(
            phase.number, phase.name, apply=apply,
            inputs={"run_id": run_id, "config_sha256": cfg.source_sha256},
            command_parameters={"apply": apply},
        )
        ctx = PhaseContext(cfg, paths, state, logger, apply, phase_run_id, run_id, runtime)
        logger.info(phase.name, "start", f"starting phase {phase.command}")
        try:
            result = phase.run(ctx)
        except BaseException as exc:
            logger.error(phase.name, "failure", f"phase {phase.command} failed",
                         provider_category=type(exc).__name__, raw_error=str(exc))
            state.complete_phase(phase_run_id, "FAIL", outputs={"error_type": type(exc).__name__},
                                 error_summary=logger.redact(str(exc)))
            if isinstance(exc, PhaseError):
                raise
            raise PhaseError(f"{phase.command} failed: {type(exc).__name__}") from exc
        for role, path, row_count in result.artifacts:
            state.record_artifact(phase_run_id, role, path, paths.root, row_count=row_count)
        state.complete_phase(phase_run_id, result.status, outputs=result.outputs)
        logger.info(phase.name, "complete", f"phase {phase.command} completed with {result.status}")
        return result.status
    finally:
        state.close()
```

- [ ] **Step 5: Write __main__.py (phase dispatch only; utility commands arrive in Task 9)**

```python
from __future__ import annotations

import argparse
import sys

from .env import Runtime
from .runner import PHASES, run_phase

# Pre-state and utility commands register here in later tasks: {name: callable(runtime, args)}
COMMANDS: dict[str, object] = {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="migrator", description="Dropbox to Proton Drive mirror")
    parser.add_argument("--apply", action="store_true", help="permit mutation phases to act")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in PHASES:
        sub.add_parser(command, help=f"run the {command} phase")
    for command in COMMANDS:
        p = sub.add_parser(command)
        p.add_argument("args", nargs="*")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime = Runtime.from_environ()
    try:
        if args.command in PHASES:
            status = run_phase(args.command, apply=args.apply, runtime=runtime)
            print(f"{args.command}: {status}")
            return 0 if status in {"PASS", "PLANNED"} else 2
        return int(COMMANDS[args.command](runtime, args.args))  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001 - every class: a traceback would print provider stderr
        # Error text may carry provider stderr with path names; CI sees the class only.
        detail = f": {exc}" if runtime.verbose else ""
        print(f"ERROR: {type(exc).__name__}{detail}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("INTERRUPTED", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
```

The `argparse` parser is built from `PHASES` at call time, so the test's monkeypatched entry appears.

- [ ] **Step 6: Run tests**

Run: `task test -- tests/test_runner.py`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add src/migrator/phases/base.py src/migrator/runner.py src/migrator/__main__.py tests/test_runner.py
git commit -m "feat(runner): phase context, evidence-recording runner, CLI entry"
```

---

### Task 6: Dropbox providers (OAuth refresh, API inventory, rclone)

**Files:**
- Create: `src/migrator/providers/dropbox_auth.py`, `tests/test_dropbox_auth.py`
- Copy and adapt: `src/migrator/providers/dropbox_api.py`, `src/migrator/providers/dropbox_rclone.py` from the donor
- Copy and adapt tests: `tests/test_dropbox_api.py`, `tests/test_rclone_inventory.py`; copy `tests/fixtures/dropbox/*.synthetic.json`
- Create: `tests/test_rclone_copy.py`

**Interfaces:**
- Consumes: `Config` (`cfg.dropbox.*`, `cfg.rclone.*`), `WorkPaths.rclone_config`, `State`, `RunLogger`, `Runtime` (app key, secret, refresh token).
- Produces: `access_token(cfg, runtime, *, session=None) -> str`; `DropboxAPIProvider(cfg, state, logger, *, token, session=None, sleep=time.sleep)` with `.identity(operation)` and `.inventory(purpose, *, reuse_complete=True) -> int`; `DropboxRcloneProvider(cfg, paths, state, logger, *, run=subprocess.run, sleep=time.sleep)` with `.version()`, `.inventory(purpose, *, reuse_complete=True) -> int`, `.copy_files_from(list_file, target, log_path) -> int`, and `ACCEPTED_COPY_EXITS = {0, 3, 4}`.

- [ ] **Step 1: Write the failing auth test**

`tests/test_dropbox_auth.py`:

```python
from __future__ import annotations

import pytest

from migrator.providers.dropbox_auth import DropboxAuthError, access_token


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, data=None, auth=None, timeout=None):
        self.calls.append((url, data, auth))
        return self.response


def test_exchanges_refresh_token(config_factory, runtime_factory, tmp_path):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path)
    session = _Session(_Response(200, {"access_token": "sl.new", "expires_in": 14400}))
    assert access_token(cfg, runtime, session=session) == "sl.new"
    url, data, auth = session.calls[0]
    assert url == cfg.dropbox.token_url
    assert data == {"grant_type": "refresh_token", "refresh_token": "refresh-token"}
    assert auth == ("app-key", "app-secret")


def test_rejects_failure_without_leaking_body(config_factory, runtime_factory, tmp_path):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path)
    session = _Session(_Response(400, {"error": "invalid_grant"}))
    with pytest.raises(DropboxAuthError, match="HTTP 400"):
        access_token(cfg, runtime, session=session)


def test_requires_all_three_secrets(config_factory, runtime_factory, tmp_path):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path, MIRROR_DROPBOX_REFRESH_TOKEN="")
    with pytest.raises(DropboxAuthError, match="MIRROR_DROPBOX_REFRESH_TOKEN"):
        access_token(cfg, runtime, session=_Session(None))
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_dropbox_auth.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write dropbox_auth.py**

```python
from __future__ import annotations

import requests

from ..config import Config
from ..env import Runtime


class DropboxAuthError(RuntimeError):
    pass


def access_token(cfg: Config, runtime: Runtime, *, session: requests.Session | None = None) -> str:
    missing = [
        name for name, value in (
            ("MIRROR_DROPBOX_APP_KEY", runtime.dropbox_app_key),
            ("MIRROR_DROPBOX_APP_SECRET", runtime.dropbox_app_secret),
            ("MIRROR_DROPBOX_REFRESH_TOKEN", runtime.dropbox_refresh_token),
        ) if not value
    ]
    if missing:
        raise DropboxAuthError(f"required secret is unset: {', '.join(missing)}")
    http = session or requests.Session()
    try:
        response = http.post(
            cfg.dropbox.token_url,
            data={"grant_type": "refresh_token", "refresh_token": runtime.dropbox_refresh_token},
            auth=(runtime.dropbox_app_key, runtime.dropbox_app_secret),
            timeout=cfg.dropbox.timeout_seconds,
        )
    except requests.RequestException as exc:
        raise DropboxAuthError("Dropbox token refresh failed on the network") from exc
    if response.status_code != 200:
        raise DropboxAuthError(f"Dropbox token refresh failed with HTTP {response.status_code}")
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise DropboxAuthError("Dropbox token refresh returned no access token")
    return token
```

- [ ] **Step 4: Copy and adapt dropbox_api.py**

Copy `$DONOR/src/migrator/providers/dropbox_api.py`, then:

1. Change the constructor signature to `(self, cfg, state, logger, *, token: str, session=None, sleep=time.sleep)` and set `self.token = token`.
2. Replace every `self.cfg.dropbox_api` with `self.cfg.dropbox`, every `self.cfg.dropbox_rclone.root` with `self.cfg.dropbox.root`.
3. Delete `root_namespace_id` and `expected_account_id` handling? No: keep `expected_account_id` (from `cfg.dropbox`); for the namespace, replace `self.cfg.dropbox_api.root_namespace_id or observed_root_namespace` with `observed_root_namespace`.
4. Change the log phase string `"10_source"` to `"10_inventory"` (four places).
5. In the 429 branch, add `wait_seconds=wait` to the `self.logger.warning(...)` call's keyword arguments so the report can sum waits from `events.fields_json`.
6. Remove the `page_limit` reference `self.cfg.dropbox_api.page_limit` -> `self.cfg.dropbox.page_limit`.

- [ ] **Step 5: Copy and adapt dropbox_rclone.py**

Copy `$DONOR/src/migrator/providers/dropbox_rclone.py`, then:

1. Constructor becomes `(self, cfg, paths, state, logger, *, run=subprocess.run, sleep=time.sleep)`; store `self.paths = paths`.
2. Replace `self.cfg.dropbox_rclone.executable` with `self.cfg.rclone.executable`, `self.cfg.dropbox_rclone.remote` with `self.cfg.rclone.remote`, `self.cfg.dropbox_rclone.root` with `self.cfg.dropbox.root`, `self.cfg.dropbox_rclone.list_timeout_seconds` with `self.cfg.rclone.list_timeout_seconds`, `self.cfg.dropbox_rclone.transfer_timeout_seconds` with `self.cfg.rclone.transfer_timeout_seconds`, `self.cfg.transfer.X` with `self.cfg.rclone.X`, `self.cfg.dropbox_api.initial_backoff_seconds` and `maximum_backoff_seconds` with `self.cfg.dropbox.*`.
3. In `_common_flags`, the config path becomes `str(self.paths.rclone_config)`; drop `--checkers` where it appears (not in `Rclone`).
4. Phase strings `"10_source"` -> `"10_inventory"`.
5. Delete `copy_archive` and `recover_file`; add:

```python
ACCEPTED_COPY_EXITS = {0, 3, 4}  # 3/4: a listed path vanished between listing and fetch


    def copy_files_from(self, list_file: Path, target: Path, log_path: Path) -> int:
        target.mkdir(parents=True, exist_ok=True)
        argv = [
            self.cfg.rclone.executable, "copy", self._remote_path(), str(target),
            "--files-from-raw", str(list_file), "--no-traverse", "--ignore-existing",
            "--transfers", str(self.cfg.rclone.transfers),
            "--use-json-log", "--log-file", str(log_path), "--stats", "60s",
            *self._common_flags(),
        ]
        command_id = self.state.record_command_start("rclone", "copy", argv, 1)
        try:
            result = self.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False, timeout=self.cfg.rclone.transfer_timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self.state.record_command_end(command_id, -1, "TIMEOUT")
            raise RcloneError("rclone copy timed out") from exc
        category = "SUCCESS" if result.returncode == 0 else _category(result.stderr, result.returncode)
        self.state.record_command_end(command_id, result.returncode, category)
        if result.returncode not in ACCEPTED_COPY_EXITS:
            raise RcloneError(f"rclone copy failed ({category})")
        return result.returncode
```

`--files-from-raw` reads the list without rclone's filter-syntax processing, so names containing `*`, `[`, or `{` survive.

- [ ] **Step 6: Adapt the donor tests and add the copy test**

Copy `tests/test_dropbox_api.py` and `tests/test_rclone_inventory.py` from the donor, plus `tests/fixtures/dropbox/`. Update every `DropboxAPIProvider(cfg, state, logger, session=...)` call to pass `token="test-token"`; every `DropboxRcloneProvider(cfg, state, logger, run=...)` to `DropboxRcloneProvider(cfg, paths, state, logger, run=...)`; unpack `state_context` as `cfg, paths, state, logger, runtime`; replace `cfg.dropbox_api` / `cfg.dropbox_rclone` / `cfg.transfer` references with `cfg.dropbox` / `cfg.rclone`. Delete any test about `root_namespace_id` configuration override.

`tests/test_rclone_copy.py`:

```python
from __future__ import annotations

import subprocess

import pytest

from migrator.providers.dropbox_rclone import DropboxRcloneProvider, RcloneError


def _run_returning(code, stderr=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)

    return run, calls


def test_copy_accepts_vanished_exit_codes(state_context, tmp_path):
    cfg, paths, state, logger, _ = state_context
    run, calls = _run_returning(3)
    provider = DropboxRcloneProvider(cfg, paths, state, logger, run=run)
    listing = tmp_path / "files.txt"
    listing.write_text("A/b.txt\n", encoding="utf-8")
    code = provider.copy_files_from(listing, tmp_path / "staging", tmp_path / "copy.jsonl")
    assert code == 3
    argv = calls[0]
    assert "--files-from-raw" in argv and "--no-traverse" in argv
    assert argv[argv.index("--tpslimit") + 1] == "10"
    assert argv[argv.index("--config") + 1] == str(paths.rclone_config)
    row = state.connection.execute("SELECT response_category FROM commands").fetchone()
    assert row["response_category"] == "EXIT_3"


def test_copy_rejects_other_failures(state_context, tmp_path):
    cfg, paths, state, logger, _ = state_context
    run, _ = _run_returning(7, "Fatal error")
    provider = DropboxRcloneProvider(cfg, paths, state, logger, run=run)
    listing = tmp_path / "files.txt"
    listing.write_text("A/b.txt\n", encoding="utf-8")
    with pytest.raises(RcloneError):
        provider.copy_files_from(listing, tmp_path / "staging", tmp_path / "copy.jsonl")
```

- [ ] **Step 7: Run tests**

Run: `task test -- tests/test_dropbox_auth.py tests/test_dropbox_api.py tests/test_rclone_inventory.py tests/test_rclone_copy.py`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/migrator/providers tests/test_dropbox_auth.py tests/test_dropbox_api.py tests/test_rclone_inventory.py tests/test_rclone_copy.py tests/fixtures/dropbox
git commit -m "feat(dropbox): token refresh, API inventory, rclone batch fetch"
```

---

### Task 7: Proton CLI provider

**Files:**
- Copy and adapt: `src/migrator/providers/proton_cli.py`
- Copy and adapt: `tests/test_proton_inventory.py`, `tests/fixtures/proton/filesystem_info_0.8.0.synthetic.json`
- Create: `tests/test_proton_mutations.py`

**Interfaces:**
- Consumes: `cfg.proton.*`, `State`, `RunLogger`.
- Produces: `ProtonCLIProvider(cfg, state, logger, *, run=subprocess.run, sleep=time.sleep, after_call=None)` with `.version()`, `.root_uid(phase) -> str`, `.list_folder(path, phase) -> list[dict]`, `.inventory(purpose, phase, *, reuse_complete=True) -> int`, `.download_file(remote_path, local_parent, phase)`, `.upload_tree(sources, destination, phase) -> str`, `.trash(cli_paths, phase)`, `.empty_trash(phase)`; module functions `unwrap`, `escape_component`, `split_parent_path`, `child_cli_path(parent_cli_path, name, uid, duplicate) -> str`, `ProtonCLIError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_proton_mutations.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from migrator.providers.proton_cli import (
    ProtonCLIError, ProtonCLIProvider, child_cli_path,
)


def _fake_run(responses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code, out, err = responses.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    return run, calls


def test_upload_tree_argv_and_hook(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([(0, '{"uploaded":1}\n', "")])
    hooks = []
    provider = ProtonCLIProvider(cfg, state, logger, run=run, after_call=lambda: hooks.append(1))
    out = provider.upload_tree([tmp_path / "A", tmp_path / "B"], "/my-files/Dropbox", "40_batches")
    assert out.startswith('{"uploaded"')
    argv = calls[0]
    assert argv[:3] == ["proton-drive", "filesystem", "upload"]
    assert argv[argv.index("-f") + 1] == "create-new-revision"
    assert argv[argv.index("-d") + 1] == "merge"
    assert "--json" in argv and "--skip-thumbnails" in argv
    assert argv[-3:] == [str(tmp_path / "A"), str(tmp_path / "B"), "/my-files/Dropbox"]
    assert hooks == [1]


def test_upload_tree_failure_raises_and_still_hooks(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    run, _ = _fake_run([(1, "", "You need to login first")])
    hooks = []
    provider = ProtonCLIProvider(cfg, state, logger, run=run, after_call=lambda: hooks.append(1))
    with pytest.raises(ProtonCLIError, match="AUTH"):
        provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert hooks == [1]


def test_trash_passes_every_path_in_one_call(state_context):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([(0, "", "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    provider.trash(["/my-files/Dropbox/a", "/my-files/Dropbox/b"], "50_trash")
    assert calls[0] == ["proton-drive", "filesystem", "trash", "/my-files/Dropbox/a", "/my-files/Dropbox/b"]


def test_root_uid_matches_expected(state_context):
    cfg, _, state, logger, _ = state_context
    listing = json.dumps([{"uid": "uid-destination", "name": {"ok": True, "value": "Dropbox"}, "type": "folder"}])
    run, calls = _fake_run([(0, listing, "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    assert provider.root_uid("00_probe") == "uid-destination"
    assert calls[0][-1] == "/my-files"


def test_root_uid_mismatch_raises(state_context):
    cfg, _, state, logger, _ = state_context
    listing = json.dumps([{"uid": "other", "name": {"ok": True, "value": "Dropbox"}, "type": "folder"}])
    run, _ = _fake_run([(0, listing, "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    with pytest.raises(ProtonCLIError, match="did not exactly match"):
        provider.root_uid("00_probe")


def test_child_cli_path_escapes_or_uses_uid():
    assert child_cli_path("/my-files/Dropbox", "a/b.txt", "u1", duplicate=False) == "/my-files/Dropbox/a\\/b.txt"
    assert child_cli_path("/my-files/Dropbox/", "x", "u1", duplicate=True) == "/my-files/Dropbox/u1"
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_proton_mutations.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Copy and adapt proton_cli.py**

Copy `$DONOR/src/migrator/providers/proton_cli.py`, then:

1. Constructor gains `after_call: Callable[[], None] | None = None`; store as `self.after_call`. Add a private helper and call it after every `self.run(...)` returns or raises inside `_json_command`, `download_file`, and `_mutation` (place the call in a `finally:` around each `self.run` invocation):

```python
    def _after(self) -> None:
        if self.after_call is not None:
            self.after_call()
```

2. Replace `info` and `identity` (and `ProtonIdentity`) with:

```python
    def root_uid(self, phase: str) -> str:
        parent_path, escaped_name = split_parent_path(self.cfg.proton.destination)
        listing = self._json_command(
            "filesystem_list_destination_parent",
            [self.cfg.proton.executable, "filesystem", "list", "-j", parent_path],
            phase=phase, attempts=self.cfg.proton.list_max_attempts,
        )
        if not isinstance(listing, list):
            raise ProtonCLIError("Proton destination parent listing was not a JSON array")
        matches = [
            node for node in listing
            if isinstance(node, dict) and isinstance(unwrap(node.get("name")), str)
            and escape_component(str(unwrap(node["name"]))) == escaped_name
        ]
        if len(matches) != 1 or unwrap(matches[0].get("type")) != "folder":
            raise ProtonCLIError("configured Proton destination did not resolve to exactly one folder")
        observed = str(unwrap(matches[0].get("uid")) or "")
        expected = self.cfg.proton.expected_destination_uid
        matched = observed == expected
        self.state.record_identity_observation(
            "proton", phase, expected, observed, matched=matched,
            details={"destination": self.cfg.proton.destination},
        )
        if not matched:
            raise ProtonCLIError("configured Proton destination UID did not exactly match the listing")
        return observed
```

3. Delete `create_folder` and `upload_file`. Add:

```python
    def upload_tree(self, sources: list[Path], destination: str, phase: str) -> str:
        argv = [
            self.cfg.proton.executable, "filesystem", "upload",
            "-f", "create-new-revision", "-d", "merge", "--json", "--skip-thumbnails",
            *(str(source) for source in sources), destination,
        ]
        return self._mutation("upload", argv, phase, accepted=frozenset({0, 1}))

    def trash(self, cli_paths: list[str], phase: str) -> None:
        if not cli_paths:
            return
        self._mutation("trash", [self.cfg.proton.executable, "filesystem", "trash", *cli_paths], phase)

    def empty_trash(self, phase: str) -> None:
        self._mutation("empty_trash", [self.cfg.proton.executable, "filesystem", "empty-trash"], phase)
```

Spec section 5.1 lists a `-t` flag for upload; the CLI's `--help` decides whether `-t` is a separate flag or the short form of `--skip-thumbnails`. Bootstrap step 3 in the README (Task 20) records the verified invocation; until then the argv above is the one under test. Also unverified: whether one invocation accepts several local sources. `ponytail:` if it does not, `upload_tree` loops one `_mutation` per source; the confirm step is unaffected.

4. `_mutation` returns `result.stdout` (change the signature to `-> str` and `return result.stdout` after the success branch). Its `timeout` uses `transfer_timeout_seconds` for `upload` and `command_timeout_seconds` otherwise (already so). Add a keyword `accepted: frozenset[int] = frozenset({0})` and treat any exit code in it as success; `upload_tree` passes `frozenset({0, 1})` because the CLI exits 1 when any single item failed and `confirm` adjudicates per file from the listing. An AUTH category (`login first`) raises whatever the exit code.

5. In `inventory`, the child path expression `component = uid if name in duplicates else escape_component(name)` / `node_cli_path = ...` becomes `node_cli_path = child_cli_path(cli_path, name, uid, name in duplicates)`, with the module-level function:

```python
def child_cli_path(parent_cli_path: str, name: str, uid: str, duplicate: bool) -> str:
    component = uid if duplicate else escape_component(name)
    return parent_cli_path.rstrip("/") + "/" + component
```

6. `download_file` keeps `--file-conflict-strategy skip --folder-conflict-strategy skip`. Delete the `from ..filesystem import comparison_key` only if unused (it is used in `inventory`; keep it).

- [ ] **Step 4: Adapt tests/test_proton_inventory.py**

Copy from the donor with its fixture. Change the `state_context` unpacking to five values, `ProtonCLIProvider(cfg, state, logger, run=...)` calls stay valid. Delete tests that exercise `info`, `identity`, `create_folder`, or `upload_file`. Keep the tests that walk the folder queue and resume it.

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_proton_mutations.py tests/test_proton_inventory.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/migrator/providers/proton_cli.py tests/test_proton_mutations.py tests/test_proton_inventory.py tests/fixtures/proton
git commit -m "feat(proton): CLI provider with tree upload, trash, and session hook"
```

---

### Task 8: R2 store, age wrappers, session and state files

**Files:**
- Create: `src/migrator/store.py`, `src/migrator/crypt.py`, `src/migrator/session.py`, `src/migrator/statefile.py`, `tests/test_store.py`, `tests/test_session.py`, `tests/test_statefile.py`

**Interfaces:**
- Consumes: `Runtime` (bucket, age identity), `WorkPaths` (session, age_key, state_db, rclone_config), `State.snapshot_to`.
- Produces: everything under `migrator.store`, `migrator.crypt`, `migrator.session`, `migrator.statefile` in the shared interfaces. `Store` raises `StoreError`; `statefile.fetch` raises `PhaseError` for a missing state beside existing history.

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
from __future__ import annotations

import subprocess

import pytest

from migrator.paths import WorkPaths
from migrator.store import Store, StoreError


def _fake(responses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code, out, err = responses.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    return run, calls


def test_get_put_copy_list_argv(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, calls = _fake([(0, "", ""), (0, "", ""), (0, "", ""), (0, '[{"Path":"a.age","Name":"a.age"}]', "")])
    store = Store(runtime, paths, run=run)
    assert store.get(".state/x", tmp_path / "x") is True
    store.put(tmp_path / "x", ".state/y")
    store.copy(".state/history/h", ".state/state.sqlite.xz.age")
    assert store.list(".state/history/") == [".state/history/a.age"]
    assert calls[0][:3] == ["rclone", "copyto", "r2:test-bucket/.state/x"]
    assert calls[1][1:4] == ["copyto", str(tmp_path / "x"), "r2:test-bucket/.state/y"]
    assert calls[2][1:4] == ["copyto", "r2:test-bucket/.state/history/h", "r2:test-bucket/.state/state.sqlite.xz.age"]
    assert calls[3][1:3] == ["lsjson", "r2:test-bucket/.state/history/"]
    assert all("--config" in argv for argv in calls)


def test_get_missing_returns_false_and_other_errors_raise(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, _ = _fake([(3, "", "directory not found"), (7, "", "boom")])
    store = Store(runtime, paths, run=run)
    assert store.get(".state/missing", tmp_path / "m") is False
    with pytest.raises(StoreError):
        store.get(".state/other", tmp_path / "o")


def test_list_of_absent_prefix_is_empty(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, _ = _fake([(3, "", "not found")])
    assert Store(runtime, paths, run=run).list(".state/history/") == []


def test_probe_raises_when_bucket_unreachable(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, _ = _fake([(3, "", "NoSuchBucket")])
    with pytest.raises(StoreError, match="reachable"):
        Store(runtime, paths, run=run).probe()


def test_bucket_required(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path, MIRROR_R2_BUCKET="")
    with pytest.raises(StoreError, match="MIRROR_R2_BUCKET"):
        Store(runtime, WorkPaths.from_runtime(runtime))
```

Append to `tests/conftest.py` (shared by every later test that touches R2 or age):

```python
from migrator import crypt, session


class FakeStore:
    """In-memory stand-in for migrator.store.Store."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get(self, key, target: Path) -> bool:
        if key not in self.objects:
            return False
        target.write_bytes(self.objects[key])
        return True

    def put(self, source: Path, key) -> None:
        self.objects[key] = source.read_bytes()

    def copy(self, s, t):
        self.objects[t] = self.objects[s]

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def probe(self):
        pass


@pytest.fixture
def plain_crypt(monkeypatch):
    # age is exercised in the image build; here encryption is identity so tar bytes are inspectable.
    monkeypatch.setattr(crypt, "encrypt", lambda identity, key_file, source, target, run=None: target.write_bytes(source.read_bytes()))
    monkeypatch.setattr(crypt, "decrypt", lambda identity, key_file, source, target, run=None: target.write_bytes(source.read_bytes()))
    monkeypatch.setattr(session, "_last_digest", None)
```

Test modules reach the helper classes with `from conftest import FakeStore`; fixtures such as `plain_crypt` are visible by name without an import.

`tests/test_session.py`:

```python
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from conftest import FakeStore

from migrator import session
from migrator.paths import WorkPaths
from migrator.phases.base import PhaseError


def _ready(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    return runtime, paths


def test_restore_requires_object(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    with pytest.raises(PhaseError, match="session"):
        session.restore(runtime, paths, FakeStore())


def test_seal_then_restore_round_trips_files(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    laptop = tmp_path / "pd"
    laptop.mkdir()
    (laptop / "auth-session.json").write_text('{"session":1}', encoding="utf-8")
    (laptop / "clientUid.json").write_text('{"uid":"c"}', encoding="utf-8")
    (laptop / "proton-drive.log").write_text("noise", encoding="utf-8")
    store = FakeStore()
    session.seal(runtime, paths, store, laptop)
    names = tarfile.open(fileobj=io.BytesIO(store.objects[session.SESSION_KEY])).getnames()
    assert sorted(names) == ["auth-session.json", "clientUid.json"]
    session.restore(runtime, paths, store)
    assert (paths.session / "auth-session.json").read_text(encoding="utf-8") == '{"session":1}'
    assert (paths.session / "auth-session.json").stat().st_mode & 0o777 == 0o600


def test_writeback_only_when_session_changed(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    (paths.session / "auth-session.json").write_text("v1", encoding="utf-8")
    (paths.session / "clientUid.json").write_text("c", encoding="utf-8")
    store = FakeStore()
    assert session.writeback(runtime, paths, store) is True
    assert session.writeback(runtime, paths, store) is False
    (paths.session / "auth-session.json").write_text("v2-rotated", encoding="utf-8")
    assert session.writeback(runtime, paths, store) is True


def test_writeback_with_missing_session_file_is_noop(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    assert session.writeback(runtime, paths, FakeStore()) is False
```

`tests/test_statefile.py`:

```python
from __future__ import annotations

import lzma
import sqlite3

import pytest

from migrator import crypt, statefile
from migrator.phases.base import PhaseError
from migrator.state import State
from conftest import FakeStore


def test_fresh_when_bucket_has_no_state_and_no_history(state_context, plain_crypt):
    _, paths, state, _, runtime = state_context
    state.close()
    paths.state_db.unlink()
    assert statefile.fetch(runtime, paths, FakeStore()) == "fresh"
    assert not paths.state_db.exists()


def test_missing_state_beside_history_fails(state_context, plain_crypt):
    _, paths, state, _, runtime = state_context
    state.close()
    store = FakeStore()
    store.objects[statefile.HISTORY_PREFIX + "1-1.sqlite.xz.age"] = b"x"
    with pytest.raises(PhaseError, match="history"):
        statefile.fetch(runtime, paths, store)


def test_push_writes_history_then_canonical_and_fetch_restores(state_context, plain_crypt):
    cfg, paths, state, _, runtime = state_context
    with state.connection:
        state.connection.execute(
            """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256,
               run_id, mirrored_at) VALUES ('/a','/a',1,'h','s1','s2',1,'now')"""
        )
    store = FakeStore()
    statefile.push(state, runtime, paths, store, label="7-2")
    assert set(store.objects) == {statefile.STATE_KEY, statefile.HISTORY_PREFIX + "7-2.sqlite.xz.age"}
    raw = lzma.decompress(store.objects[statefile.STATE_KEY])
    state.close()
    paths.state_db.unlink()
    assert statefile.fetch(runtime, paths, store) == "restored"
    assert paths.state_db.read_bytes() == raw
    restored = State(paths.state_db, cfg.mirror.id)
    assert restored.mirror_totals() == (1, 1)
    restored.close()


def test_rollback_copies_history_over_canonical():
    store = FakeStore()
    store.objects[statefile.HISTORY_PREFIX + "1-1.sqlite.xz.age"] = b"old"
    store.objects[statefile.STATE_KEY] = b"bad"
    statefile.rollback(store, statefile.HISTORY_PREFIX + "1-1.sqlite.xz.age")
    assert store.objects[statefile.STATE_KEY] == b"old"
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_store.py tests/test_session.py tests/test_statefile.py`
Expected: FAIL, modules not found.

- [ ] **Step 3: Write store.py**

```python
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from .env import Runtime
from .paths import WorkPaths

MISSING_EXITS = {3, 4}  # rclone: directory / file not found


class StoreError(RuntimeError):
    pass


class Store:
    """R2 object store through rclone's S3 backend, configured by RCLONE_CONFIG_R2_* env."""

    def __init__(self, runtime: Runtime, paths: WorkPaths, *,
                 run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        if not runtime.r2_bucket:
            raise StoreError("required secret is unset: MIRROR_R2_BUCKET")
        self.bucket = runtime.r2_bucket
        self.paths = paths
        self.run = run

    def _remote(self, key: str) -> str:
        return f"r2:{self.bucket}/{key}"

    def _rclone(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = ["rclone", *args, "--config", str(self.paths.rclone_config), "--retries", "5"]
        try:
            return self.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=900)
        except subprocess.TimeoutExpired as exc:
            raise StoreError(f"rclone {args[0]} timed out") from exc

    def get(self, key: str, target: Path) -> bool:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = self._rclone("copyto", self._remote(key), str(target))
        if result.returncode in MISSING_EXITS:
            return False
        if result.returncode:
            raise StoreError(f"rclone copyto from R2 failed with exit {result.returncode}")
        return True

    def put(self, source: Path, key: str) -> None:
        result = self._rclone("copyto", str(source), self._remote(key))
        if result.returncode:
            raise StoreError(f"rclone copyto to R2 failed with exit {result.returncode}")

    def copy(self, source_key: str, target_key: str) -> None:
        result = self._rclone("copyto", self._remote(source_key), self._remote(target_key))
        if result.returncode:
            raise StoreError(f"rclone server-side copy failed with exit {result.returncode}")

    def list(self, prefix: str) -> list[str]:
        result = self._rclone("lsjson", self._remote(prefix))
        if result.returncode in MISSING_EXITS:
            return []
        if result.returncode:
            raise StoreError(f"rclone lsjson failed with exit {result.returncode}")
        try:
            entries = json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise StoreError("rclone lsjson returned invalid JSON") from exc
        return sorted(prefix + str(entry["Path"]) for entry in entries if not entry.get("IsDir"))

    def probe(self) -> None:
        """Fail unless the bucket answers a listing. rclone reports a wrong bucket name or a
        rejected credential with the same not-found exit as a missing key, and that must
        never read as an empty mirror."""
        result = self._rclone("lsjson", self._remote(""))
        if result.returncode:
            raise StoreError(f"R2 bucket is not reachable: rclone exit {result.returncode}")
```

- [ ] **Step 4: Write crypt.py**

```python
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path


class CryptError(RuntimeError):
    pass


@contextmanager
def _identity_file(identity: str, key_file: Path):
    # ponytail: age reads identities from a file; this one lives on the ephemeral
    # runner disk for the duration of one call, mode 0600, and is removed after.
    if not identity:
        raise CryptError("required secret is unset: MIRROR_AGE_IDENTITY")
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(identity.strip() + "\n")
    try:
        yield key_file
    finally:
        key_file.unlink(missing_ok=True)


def _check(result: subprocess.CompletedProcess[str], what: str) -> None:
    if result.returncode:
        raise CryptError(f"age {what} failed with exit {result.returncode}")


def recipient(identity: str, key_file: Path, *,
              run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str:
    with _identity_file(identity, key_file) as path:
        result = run(["age-keygen", "-y", str(path)], text=True, stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, check=False, timeout=60)
    _check(result, "keygen")
    return result.stdout.strip()


def encrypt(identity: str, key_file: Path, source: Path, target: Path, *,
            run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
    public = recipient(identity, key_file, run=run)
    result = run(["age", "-r", public, "-o", str(target), str(source)], text=True,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600)
    _check(result, "encrypt")


def decrypt(identity: str, key_file: Path, source: Path, target: Path, *,
            run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
    with _identity_file(identity, key_file) as path:
        result = run(["age", "-d", "-i", str(path), "-o", str(target), str(source)], text=True,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600)
    _check(result, "decrypt")
```

- [ ] **Step 5: Write session.py**

```python
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

from . import crypt
from .env import Runtime
from .paths import WorkPaths
from .phases.base import PhaseError
from .store import Store

SESSION_KEY = ".state/session.tar.age"
SESSION_FILES = ("auth-session.json", "clientUid.json")
_last_digest: str | None = None


def _digest(directory: Path) -> str | None:
    auth = directory / SESSION_FILES[0]
    if not auth.is_file():
        return None
    return hashlib.sha256(auth.read_bytes()).hexdigest()


def _bundle(source_dir: Path, paths: WorkPaths, runtime: Runtime, store: Store) -> None:
    tar_path = paths.root / "session.tar"
    with tarfile.open(tar_path, "w") as archive:
        for name in SESSION_FILES:
            member = source_dir / name
            if member.is_file():
                archive.add(member, arcname=name)
    encrypted = paths.root / "session.tar.age"
    try:
        crypt.encrypt(runtime.age_identity, paths.age_key, tar_path, encrypted)
        store.put(encrypted, SESSION_KEY)
    finally:
        tar_path.unlink(missing_ok=True)
        encrypted.unlink(missing_ok=True)


def restore(runtime: Runtime, paths: WorkPaths, store: Store) -> None:
    global _last_digest
    encrypted = paths.root / "session.tar.age"
    if not store.get(SESSION_KEY, encrypted):
        raise PhaseError("no Proton session in R2; run `task session-seal` after a laptop login")
    tar_path = paths.root / "session.tar"
    try:
        crypt.decrypt(runtime.age_identity, paths.age_key, encrypted, tar_path)
        with tarfile.open(tar_path) as archive:
            for member in archive.getmembers():
                if member.name not in SESSION_FILES:
                    raise PhaseError("session bundle holds an unexpected member")
            archive.extractall(paths.session, filter="data")
        for name in SESSION_FILES:
            member = paths.session / name
            if member.exists():
                member.chmod(0o600)
    finally:
        encrypted.unlink(missing_ok=True)
        tar_path.unlink(missing_ok=True)
    _last_digest = _digest(paths.session)


def writeback(runtime: Runtime, paths: WorkPaths, store: Store) -> bool:
    """Push the session to R2 when auth-session.json changed since the last push."""
    global _last_digest
    current = _digest(paths.session)
    if current is None or current == _last_digest:
        return False
    _bundle(paths.session, paths, runtime, store)
    _last_digest = current
    return True


def seal(runtime: Runtime, paths: WorkPaths, store: Store, source_dir: Path) -> None:
    for name in SESSION_FILES:
        if not (source_dir / name).is_file():
            raise PhaseError(f"session directory lacks {name}")
    _bundle(source_dir, paths, runtime, store)
```

`_last_digest` is per process, which is exactly the lifetime of one phase invocation; the first `writeback` in a phase that did not `restore` compares against `None` and pushes once, which is harmless.

- [ ] **Step 6: Write statefile.py**

```python
from __future__ import annotations

import lzma
from pathlib import Path

from . import crypt
from .env import Runtime
from .paths import WorkPaths
from .phases.base import PhaseError
from .state import State
from .store import Store

STATE_KEY = ".state/state.sqlite.xz.age"
HISTORY_PREFIX = ".state/history/"


def fetch(runtime: Runtime, paths: WorkPaths, store: Store) -> str:
    """Restore state.sqlite from R2. A missing object is an empty mirror only on the
    first run ever, which is when the history prefix is empty as well."""
    encrypted = paths.root / "state.sqlite.xz.age"
    try:
        if not store.get(STATE_KEY, encrypted):
            if store.list(HISTORY_PREFIX):
                raise PhaseError(
                    "state object is missing but history exists; a lost state must never "
                    "be mistaken for an empty mirror. Roll back with `task state-rollback`."
                )
            store.probe()  # "fresh" is only believable from a bucket that answers
            return "fresh"
        compressed = paths.root / "state.sqlite.xz"
        try:
            crypt.decrypt(runtime.age_identity, paths.age_key, encrypted, compressed)
            with lzma.open(compressed, "rb") as source:
                paths.state_db.write_bytes(source.read())
        finally:
            compressed.unlink(missing_ok=True)
        return "restored"
    finally:
        encrypted.unlink(missing_ok=True)


def push(state: State, runtime: Runtime, paths: WorkPaths, store: Store, label: str) -> None:
    snapshot = paths.root / "state.snapshot.sqlite"
    compressed = paths.root / "state.sqlite.xz"
    encrypted = paths.root / "state.sqlite.xz.age"
    try:
        state.snapshot_to(snapshot)
        with open(snapshot, "rb") as source, lzma.open(compressed, "wb", preset=6) as target:
            target.write(source.read())
        crypt.encrypt(runtime.age_identity, paths.age_key, compressed, encrypted)
        history_key = f"{HISTORY_PREFIX}{label}.sqlite.xz.age"
        store.put(encrypted, history_key)
        store.copy(history_key, STATE_KEY)  # server-side; the blob crosses the wire once
    finally:
        for path in (snapshot, compressed, encrypted):
            path.unlink(missing_ok=True)


def rollback(store: Store, history_key: str) -> None:
    store.copy(history_key, STATE_KEY)
```

`ponytail:` the snapshot is read into memory for compression; the state is a few hundred MB at most for 100k files, and the runner has 16 GB. Stream it if that ever changes.

- [ ] **Step 7: Run tests**

Run: `task test -- tests/test_store.py tests/test_session.py tests/test_statefile.py`
Expected: PASS (13 tests). Also run `task run -- sh -c 'cd /tmp && age-keygen -o k 2>/dev/null && echo hi > p && age -r $(age-keygen -y k) -o p.age p && age -d -i k p.age'` once; expected output `hi`, proving the real binaries behave as the wrappers assume.

- [ ] **Step 8: Commit**

```bash
git add src/migrator/store.py src/migrator/crypt.py src/migrator/session.py src/migrator/statefile.py tests/test_store.py tests/test_session.py tests/test_statefile.py
git commit -m "feat(r2): encrypted session and state objects with dated history"
```

---

### Task 9: Pre-state commands and utilities (clock, session, state, ping, status, rollback, seal)

**Files:**
- Create: `src/migrator/commands.py`, `tests/test_commands.py`
- Modify: `src/migrator/__main__.py` (register `COMMANDS`)

**Interfaces:**
- Consumes: `Runtime`, `WorkPaths`, `Store`, `session`, `statefile`, `State`, `Config`.
- Produces: `COMMANDS` entries `clock`, `session`, `state`, `ping`, `status`, `state-push`, `state-rollback`, `session-seal`; each is `fn(runtime, args: list[str]) -> int`. `clock` writes `paths.clock` as `{"start_epoch": int, "hour_utc": int, "weekday": int}` and clears `staging/`, `roundtrip/`, `report`, `chain`. `state` fetches the state and starts the run row; `status` fetches the state and starts nothing. `is_reconcile_run(cfg, runtime, db, *, start_epoch, weekday) -> bool`.

- [ ] **Step 1: Write the failing tests**

`tests/test_commands.py`:

```python
from __future__ import annotations

import json

import pytest

from migrator import commands
from migrator.paths import WorkPaths
from migrator.state import State
from conftest import FakeStore


def test_clock_writes_stamp_and_clears_outputs(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path, MIRROR_RUN_EPOCH="1700000000")  # 2023-11-14 22:13 UTC, Tuesday
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    (paths.staging / "junk").write_text("x", encoding="utf-8")
    paths.chain.write_text("", encoding="utf-8")
    assert commands.clock(runtime, []) == 0
    stamp = json.loads(paths.clock.read_text(encoding="utf-8"))
    assert stamp == {"start_epoch": 1700000000, "hour_utc": 22, "weekday": 1}
    assert not (paths.staging / "junk").exists() and not paths.chain.exists()


def test_clock_requires_epoch(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path, MIRROR_RUN_EPOCH="")
    with pytest.raises(ValueError, match="MIRROR_RUN_EPOCH"):
        commands.clock(runtime, [])


def test_state_fresh_starts_run_with_budget_override(state_context, plain_crypt, monkeypatch):
    cfg, paths, state, _, runtime = state_context
    state.close()
    paths.state_db.unlink()
    commands.clock(runtime, [])
    monkeypatch.setattr(commands, "Store", lambda runtime, paths: FakeStore())
    monkeypatch.setattr(commands, "load_config", lambda _: cfg)
    runtime = runtime_factory_override(runtime, budget_override=30)
    assert commands.state(runtime, []) == 0
    fresh = State(paths.state_db, cfg.mirror.id)
    run = fresh.current_run()
    assert run["budget_minutes"] == 30 and run["start_epoch"] == 1700000000
    assert run["reconcile"] == 0
    fresh.close()


def runtime_factory_override(runtime, **changes):
    from dataclasses import replace
    return replace(runtime, **changes)


def test_is_reconcile_run_is_first_run_of_the_weekday(state_context):
    cfg, _, state, _, runtime = state_context  # reconcile.weekday 0
    monday = 1700438400  # 2023-11-20 00:00:00 UTC, a Monday
    assert commands.is_reconcile_run(cfg, runtime, state, start_epoch=monday + 3600, weekday=0) is True
    state.start_run(start_epoch=monday + 3600, hour_utc=1, weekday=0, budget_minutes=1, host="t", reconcile=True)
    # a chained or queued run later the same day does not walk Proton again
    assert commands.is_reconcile_run(cfg, runtime, state, start_epoch=monday + 7200, weekday=0) is False
    assert commands.is_reconcile_run(cfg, runtime, state, start_epoch=monday + 90000, weekday=1) is False
    forced = runtime_factory_override(runtime, reconcile=True)
    assert commands.is_reconcile_run(cfg, forced, state, start_epoch=monday + 7200, weekday=0) is True


def test_ping_hits_fail_suffix(runtime_factory, tmp_path, monkeypatch):
    runtime = runtime_factory(tmp_path)
    seen = []
    monkeypatch.setattr(commands, "_http_get", lambda url: seen.append(url))
    assert commands.ping(runtime, []) == 0
    assert commands.ping(runtime, ["fail"]) == 0
    assert seen == ["https://hc.example/ping/x", "https://hc.example/ping/x/fail"]


def test_status_prints_counts_only(state_context, capsys, monkeypatch):
    cfg, paths, state, _, runtime = state_context
    monkeypatch.setattr(commands, "load_config", lambda _: cfg)
    monkeypatch.setattr(commands, "_fetch_state", lambda runtime, paths: "restored")
    state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=False)
    with state.connection:
        state.connection.execute(
            """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256,
               run_id, mirrored_at) VALUES ('/taxes/x.pdf','/Taxes/x.pdf',9,'h','s','s',1,'now')"""
        )
    state.close()
    assert commands.status(runtime, []) == 0
    out = capsys.readouterr().out
    assert '"mirrored_files": 1' in out and "Taxes" not in out
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_commands.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write commands.py**

```python
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
    for directory in (paths.staging, paths.roundtrip):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir()
    for stale in (paths.report, paths.chain):
        stale.unlink(missing_ok=True)
    started = datetime.fromtimestamp(runtime.run_epoch, UTC)
    stamp = {"start_epoch": runtime.run_epoch, "hour_utc": started.hour, "weekday": started.weekday()}
    paths.clock.write_text(json.dumps(stamp) + "\n", encoding="utf-8")
    print(f"clock: run started {started.isoformat()}")
    return 0


def read_clock(paths: WorkPaths) -> dict[str, int]:
    return json.loads(paths.clock.read_text(encoding="utf-8"))


def is_reconcile_run(cfg: Config, runtime: Runtime, db: State, *, start_epoch: int, weekday: int) -> bool:
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
        reconcile = is_reconcile_run(cfg, runtime, db, start_epoch=stamp["start_epoch"], weekday=stamp["weekday"])
        run_id = db.start_run(
            start_epoch=stamp["start_epoch"], hour_utc=stamp["hour_utc"], weekday=stamp["weekday"],
            budget_minutes=runtime.budget_override or cfg.budget.run_budget_minutes,
            host=runtime.host, reconcile=reconcile,
        )
        files, size = db.mirror_totals()
    finally:
        db.close()
    print(f"state: {outcome}; run {run_id}; mirrored files={files} bytes={size}")
    return 0


def _http_get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - fixed https URL from config
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
        run = db.connection.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        phases = db.connection.execute(
            "SELECT phase_name, status, started_at, completed_at FROM phase_runs "
            "WHERE id IN (SELECT MAX(id) FROM phase_runs GROUP BY phase_number) ORDER BY phase_number"
        ).fetchall()
        figures = db.connection.execute(
            "SELECT fields_json FROM events WHERE operation='figures' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        db.close()
    print(json.dumps({
        "mirrored_files": files, "mirrored_bytes": size,
        "last_run": dict(run) if run else None,
        "phases": [dict(row) for row in phases],
        "last_figures": json.loads(figures["fields_json"]) if figures else None,
    }, indent=2))
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
```

- [ ] **Step 4: Register the commands in __main__.py**

Replace `COMMANDS: dict[str, object] = {}` with:

```python
from . import commands

COMMANDS = {
    "clock": commands.clock,
    "session": commands.session_restore,
    "state": commands.state,
    "ping": commands.ping,
    "status": commands.status,
    "state-push": commands.state_push,
    "state-rollback": commands.state_rollback,
    "session-seal": commands.session_seal,
}
```

- [ ] **Step 5: Run tests**

Run: `task test`
Expected: PASS for every test so far, including the six new ones.

- [ ] **Step 6: Commit**

```bash
git add src/migrator/commands.py src/migrator/__main__.py tests/test_commands.py
git commit -m "feat(commands): clock, session, state, ping, status, rollback, seal"
```

---

### Task 10: Inventory phase with the reconcile-run observer gate

**Files:**
- Create: `src/migrator/phases/observer.py` (from donor `p10_source_inventory._reconcile` and `_classify`), `src/migrator/phases/p10_inventory.py`, `tests/test_inventory.py`
- Modify: `src/migrator/runner.py` (register `inventory`)

**Interfaces:**
- Consumes: `DropboxAPIProvider`, `DropboxRcloneProvider`, `access_token`, `State`, `PhaseContext`.
- Produces: `p10_inventory.run(ctx) -> PhaseResult` with outputs `{inventory_id, files, folders, bytes, non_downloadable, observer}`; sets `runs.inventory_id`. `observer.gate(ctx, api_id, rclone_id) -> Counter` raising `PhaseError` on any classification other than `MATCH`. Later phases read `ctx.state.current_run()["inventory_id"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/conftest.py`:

```python
def seed_api_inventory(state, purpose, rows):
    """rows: (path_display, size, content_hash, is_downloadable, tag). Returns the inventory id."""
    with state.connection:
        cursor = state.connection.execute(
            """INSERT INTO dropbox_inventory_runs(started_at, completed_at, status, account_id,
               root_namespace_id, purpose) VALUES ('now','now','COMPLETE','dbid:test-account','ns',?)""",
            (purpose,),
        )
        inventory_id = int(cursor.lastrowid)
        for path, size, content_hash, downloadable, tag in rows:
            state.connection.execute(
                """INSERT INTO dropbox_objects(inventory_id, object_key, tag, name, path_display, path_lower,
                   comparison_key, size, content_hash, is_downloadable, raw_json, first_page, last_page)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 1, 1)""",
                (inventory_id, path.lower(), tag, path.rsplit("/", 1)[-1], path, path.lower(),
                 path.lower(), size, content_hash, downloadable),
            )
    return inventory_id
```

`tests/test_inventory.py`:

```python
from __future__ import annotations

import pytest
from conftest import seed_api_inventory

from migrator.phases import p10_inventory
from migrator.phases.base import PhaseContext, PhaseError


def _ctx(state_context, reconcile=False):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t",
                             reconcile=reconcile)
    phase_run_id = state.start_phase(10, "10_inventory", apply=False, inputs={})
    return PhaseContext(cfg, paths, state, logger, False, phase_run_id, run_id, runtime)


def test_inventory_records_counts_and_run_link(state_context, monkeypatch):
    ctx = _ctx(state_context)
    rows = [("/A/one.txt", 3, "h1", 1, "file"), ("/A", None, None, 1, "folder"),
            ("/notes.paper", 0, None, 0, "file")]
    inventory_id = seed_api_inventory(ctx.state, "run:1", rows)
    monkeypatch.setattr(p10_inventory, "access_token", lambda cfg, runtime: "tok")
    monkeypatch.setattr(p10_inventory.DropboxAPIProvider, "inventory",
                        lambda self, purpose, reuse_complete=True: inventory_id)
    result = p10_inventory.run(ctx)
    assert result.outputs == {"inventory_id": inventory_id, "files": 1, "folders": 1, "bytes": 3,
                              "non_downloadable": 1, "observer": False, "unhashed": 0,
                              "pruned_inventories": 0}
    assert ctx.state.current_run()["inventory_id"] == inventory_id


def test_unhashed_files_become_non_downloadable(state_context, monkeypatch):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(ctx.state, "run:1", [("/cloud.gdoc", 0, None, 1, "file"),
                                                            ("/real.txt", 2, "h", 1, "file")])
    monkeypatch.setattr(p10_inventory, "access_token", lambda cfg, runtime: "tok")
    monkeypatch.setattr(p10_inventory.DropboxAPIProvider, "inventory",
                        lambda self, purpose, reuse_complete=True: inventory_id)
    outputs = p10_inventory.run(ctx).outputs
    assert outputs["files"] == 1 and outputs["non_downloadable"] == 1 and outputs["unhashed"] == 1


def test_prune_keeps_newest_inventories(state_context):
    _, _, state, _, _ = state_context
    ids = [seed_api_inventory(state, f"run:{n}", [("/a.txt", 1, "h", 1, "file")]) for n in range(4)]
    assert p10_inventory.prune_inventories(state.connection, keep=2) == 2
    left = {r["inventory_id"] for r in state.connection.execute("SELECT inventory_id FROM dropbox_objects")}
    assert left == set(ids[2:])


def test_reconcile_run_gates_on_observer(state_context, monkeypatch):
    ctx = _ctx(state_context, reconcile=True)
    inventory_id = seed_api_inventory(ctx.state, "run:1", [("/A/one.txt", 3, "h1", 1, "file")])
    monkeypatch.setattr(p10_inventory, "access_token", lambda cfg, runtime: "tok")
    monkeypatch.setattr(p10_inventory.DropboxAPIProvider, "inventory",
                        lambda self, purpose, reuse_complete=True: inventory_id)
    with ctx.state.connection:
        cursor = ctx.state.connection.execute(
            """INSERT INTO rclone_inventory_runs(started_at, completed_at, status, remote, root, version, purpose)
               VALUES ('now','now','COMPLETE','dropbox','','v','run:1')"""
        )
        rclone_id = int(cursor.lastrowid)
        ctx.state.connection.execute(
            """INSERT INTO rclone_objects(inventory_id, object_key, path, comparison_key, name, is_dir, size,
               dropbox_hash, raw_json) VALUES (?, '/a/one.txt', 'A/one.txt', '/a/one.txt', 'one.txt', 0, 4, 'h1', '{}')""",
            (rclone_id,),
        )
    monkeypatch.setattr(p10_inventory.DropboxRcloneProvider, "inventory",
                        lambda self, purpose, reuse_complete=True: rclone_id)
    with pytest.raises(PhaseError, match="discrepanc"):
        p10_inventory.run(ctx)
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_inventory.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write observer.py**

Copy `_groups`, `_reconcile`, and `_classify` from `$DONOR/src/migrator/phases/p10_source_inventory.py` into `observer.py`. In `_classify`, replace `ctx.cfg.verification.require_dropbox_rclone_hash` and `ctx.cfg.verification.require_dropbox_id_match_when_available` with `True` (both gates are always on). Append:

```python
def gate(ctx: PhaseContext, api_id: int, rclone_id: int) -> Counter[str]:
    counts = _reconcile(ctx, api_id, rclone_id)
    discrepancies = sum(count for name, count in counts.items() if name != "MATCH")
    ctx.logger.info("10_inventory", "observer", "independent Dropbox listings compared",
                    **{name: count for name, count in counts.items()})
    if discrepancies:
        raise PhaseError(f"Dropbox API/rclone listings have {discrepancies} discrepancies")
    return counts
```

- [ ] **Step 4: Write p10_inventory.py**

```python
from __future__ import annotations

import sqlite3

from ..providers.dropbox_api import DropboxAPIProvider
from ..providers.dropbox_auth import access_token
from ..providers.dropbox_rclone import DropboxRcloneProvider
from . import observer
from .base import PhaseContext, PhaseResult

PHASE = "10_inventory"
# inventory-run table -> the tables keyed by its id (column names per the donor DDL, Task 4 step 5)
_INVENTORY_TABLES = {
    "dropbox_inventory_runs": ("dropbox_objects", "dropbox_pages"),
    "rclone_inventory_runs": ("rclone_objects", "rclone_folders"),
}


def prune_inventories(connection: sqlite3.Connection, keep: int = 2) -> int:
    """Old listings are the bulk of the state, and every checkpoint ships the state to R2."""
    pruned = 0
    with connection:
        for runs_table, child_tables in _INVENTORY_TABLES.items():
            stale = [(int(r["id"]),) for r in connection.execute(
                f"SELECT id FROM {runs_table} ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,))]
            for table in child_tables:
                connection.executemany(f"DELETE FROM {table} WHERE inventory_id=?", stale)
            connection.executemany(f"DELETE FROM {runs_table} WHERE id=?", stale)
            pruned += len(stale)
    return pruned


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    purpose = f"run:{ctx.run_id}"
    token = access_token(ctx.cfg, ctx.runtime)
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
    observed = bool(run["reconcile"])
    if observed:
        rclone = DropboxRcloneProvider(ctx.cfg, ctx.paths, ctx.state, ctx.logger)
        rclone_id = rclone.inventory(purpose, reuse_complete=True)
        observer.gate(ctx, inventory_id, rclone_id)
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
        "observer": observed,
        "unhashed": int(unhashed),
        "pruned_inventories": prune_inventories(ctx.state.connection),
    }
    ctx.logger.info(PHASE, "gate", "Dropbox inventory complete", **outputs)
    return PhaseResult(outputs=outputs)
```

- [ ] **Step 5: Register the phase**

In `runner.py`, import `from .phases import p10_inventory` and set:

```python
PHASES: dict[str, PhaseDefinition] = {
    "inventory": PhaseDefinition(10, "inventory", "10_inventory", p10_inventory.run),
}
```

- [ ] **Step 6: Run tests**

Run: `task test -- tests/test_inventory.py tests/test_runner.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/migrator/phases/observer.py src/migrator/phases/p10_inventory.py src/migrator/runner.py tests/test_inventory.py
git commit -m "feat(inventory): Dropbox listing phase with weekly observer gate"
```

---

### Task 11: Delta phase

**Files:**
- Create: `src/migrator/phases/p20_delta.py`, `tests/test_delta.py`
- Modify: `src/migrator/runner.py` (register `delta`)

**Interfaces:**
- Consumes: `runs.inventory_id`, `dropbox_objects`, `mirror_objects`, `cfg.budget.listing_floor_ratio`, `filesystem.comparison_key`.
- Produces: `p20_delta.run(ctx)` filling `delta_changed` and `delta_deleted` for `ctx.run_id`; outputs `{changed_files, changed_bytes, deleted_files, listed_files, mirrored_files}`; `display_paths(connection, inventory_id) -> dict[str, str]` mapping each downloadable file's `path_lower` to its NFC display path with parent casing taken from folder entries.

- [ ] **Step 1: Write the failing tests**

`tests/test_delta.py`:

```python
from __future__ import annotations

import unicodedata

import pytest

from migrator.phases import p20_delta
from migrator.phases.base import PhaseContext, PhaseError
from conftest import seed_api_inventory


def _ctx(state_context):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=False)
    phase_run_id = state.start_phase(20, "20_delta", apply=False, inputs={})
    return PhaseContext(cfg, paths, state, logger, False, phase_run_id, run_id, runtime)


def _mirror(state, rows):
    with state.connection:
        for path_lower, display, size, content_hash in rows:
            state.connection.execute(
                """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256,
                   proton_uid, run_id, mirrored_at) VALUES (?, ?, ?, ?, 's1', 's2', 'u', 0, 'now')""",
                (path_lower, display, size, content_hash),
            )


def test_changed_and_deleted_rows(state_context):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(ctx.state, "run:1", [
        ("/Docs", None, None, 1, "folder"),
        ("/Docs/same.txt", 3, "h-same", 1, "file"),
        ("/Docs/edited.txt", 4, "h-new", 1, "file"),
        ("/Docs/added.txt", 5, "h-add", 1, "file"),
        ("/Docs/paper.paper", 0, None, 0, "file"),
    ])
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    _mirror(ctx.state, [("/docs/same.txt", "/Docs/same.txt", 3, "h-same"),
                        ("/docs/edited.txt", "/Docs/edited.txt", 4, "h-old"),
                        ("/docs/gone.txt", "/Docs/gone.txt", 1, "h-gone")])
    result = p20_delta.run(ctx)
    changed = {r["path_lower"]: r for r in ctx.state.connection.execute(
        "SELECT * FROM delta_changed WHERE run_id=?", (ctx.run_id,))}
    deleted = [r["path_lower"] for r in ctx.state.connection.execute(
        "SELECT path_lower FROM delta_deleted WHERE run_id=?", (ctx.run_id,))]
    assert set(changed) == {"/docs/edited.txt", "/docs/added.txt"}
    assert changed["/docs/added.txt"]["path_display"] == "/Docs/added.txt"
    assert deleted == ["/docs/gone.txt"]
    assert result.outputs["changed_files"] == 2 and result.outputs["changed_bytes"] == 9
    assert result.outputs["deleted_files"] == 1


def test_display_path_takes_parent_casing_from_folder_entries_and_nfc(state_context):
    ctx = _ctx(state_context)
    nfd = unicodedata.normalize("NFD", "Café")
    inventory_id = seed_api_inventory(ctx.state, "run:1", [
        ("/Photos", None, None, 1, "folder"),
        (f"/Photos/{nfd}", None, None, 1, "folder"),
        (f"/photos/{nfd}/IMG.jpg", 2, "h", 1, "file"),
    ])
    mapping = p20_delta.display_paths(ctx.state.connection, inventory_id)
    assert mapping[f"/photos/{nfd.lower()}/img.jpg"] == "/Photos/Café/IMG.jpg"
    assert unicodedata.is_normalized("NFC", mapping[f"/photos/{nfd.lower()}/img.jpg"])


def test_listing_floor_refuses_truncated_listing(state_context):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(ctx.state, "run:1", [("/a.txt", 1, "h", 1, "file")])
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    _mirror(ctx.state, [(f"/f{i}.txt", f"/f{i}.txt", 1, "h") for i in range(10)])
    with pytest.raises(PhaseError, match="floor"):
        p20_delta.run(ctx)


def test_first_run_has_no_floor(state_context):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(ctx.state, "run:1", [("/a.txt", 1, "h", 1, "file")])
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    assert p20_delta.run(ctx).outputs["changed_files"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_delta.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write p20_delta.py**

```python
from __future__ import annotations

import sqlite3
import unicodedata

from ..logging import utc_now
from .base import PhaseContext, PhaseError, PhaseResult

PHASE = "20_delta"


def display_paths(connection: sqlite3.Connection, inventory_id: int) -> dict[str, str]:
    """path_lower -> NFC display path. A file's own path_display can carry stale
    parent casing in recursive listings, so parents come from the folder entries."""
    folders = {
        str(row["path_lower"]): str(row["path_display"])
        for row in connection.execute(
            "SELECT path_lower, path_display FROM dropbox_objects WHERE inventory_id=? AND tag='folder'",
            (inventory_id,),
        )
    }
    memo: dict[str, str] = {"": ""}

    def resolve(lower: str, display: str) -> str:
        if lower in memo:
            return memo[lower]
        parent_lower, _, _ = lower.rpartition("/")
        name = display.rpartition("/")[2]
        if parent_lower in folders:
            parent_display = resolve(parent_lower, folders[parent_lower])
        else:
            parent_display = display.rpartition("/")[0]
        memo[lower] = parent_display + "/" + name
        return memo[lower]

    files = connection.execute(
        "SELECT path_lower, path_display FROM dropbox_objects "
        "WHERE inventory_id=? AND tag='file' AND is_downloadable=1",
        (inventory_id,),
    ).fetchall()
    return {
        str(row["path_lower"]): unicodedata.normalize(
            "NFC", resolve(str(row["path_lower"]), str(row["path_display"]))
        )
        for row in files
    }


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    inventory_id = run["inventory_id"]
    if inventory_id is None:
        raise PhaseError("inventory has not run")
    connection = ctx.state.connection
    listed = int(connection.execute(
        "SELECT COUNT(*) FROM dropbox_objects WHERE inventory_id=? AND tag='file' AND is_downloadable=1",
        (inventory_id,),
    ).fetchone()[0])
    mirrored, _ = ctx.state.mirror_totals()
    floor = int(mirrored * ctx.cfg.budget.listing_floor_ratio)
    if mirrored and listed < floor:
        raise PhaseError(
            f"listing has {listed} files, under the floor of {floor}; a truncated listing "
            "must never become a trash list"
        )
    display = display_paths(connection, inventory_id)
    with connection:
        connection.execute("DELETE FROM delta_changed WHERE run_id=?", (ctx.run_id,))
        connection.execute("DELETE FROM delta_deleted WHERE run_id=?", (ctx.run_id,))
        changed = connection.execute(
            """
            SELECT d.path_lower, d.size, d.content_hash
            FROM dropbox_objects d LEFT JOIN mirror_objects m ON m.path_lower = d.path_lower
            WHERE d.inventory_id=? AND d.tag='file' AND d.is_downloadable=1
              AND d.content_hash IS NOT NULL AND d.size IS NOT NULL
              AND (m.path_lower IS NULL OR m.size != d.size OR m.content_hash != d.content_hash)
            ORDER BY d.path_lower
            """,
            (inventory_id,),
        ).fetchall()
        connection.executemany(
            "INSERT INTO delta_changed(run_id, path_lower, path_display, size, content_hash) VALUES (?, ?, ?, ?, ?)",
            [(ctx.run_id, r["path_lower"], display[str(r["path_lower"])], int(r["size"]), r["content_hash"])
             for r in changed],
        )
        connection.execute(
            """
            INSERT INTO delta_deleted(run_id, path_lower, path_display, proton_uid)
            SELECT ?, m.path_lower, m.path_display, m.proton_uid FROM mirror_objects m
            WHERE NOT EXISTS (
                SELECT 1 FROM dropbox_objects d
                WHERE d.inventory_id=? AND d.path_lower=m.path_lower AND d.tag='file' AND d.is_downloadable=1
            )
            """,
            (ctx.run_id, inventory_id),
        )
    deleted = int(connection.execute(
        "SELECT COUNT(*) FROM delta_deleted WHERE run_id=?", (ctx.run_id,)).fetchone()[0])
    outputs = {
        "changed_files": len(changed),
        "changed_bytes": sum(int(r["size"]) for r in changed),
        "deleted_files": deleted,
        "listed_files": listed,
        "mirrored_files": mirrored,
        "computed_at": utc_now(),
    }
    ctx.logger.info(PHASE, "gate", "delta computed", **outputs)
    return PhaseResult(outputs=outputs)
```

- [ ] **Step 4: Register the phase**

Add to `PHASES`: `"delta": PhaseDefinition(20, "delta", "20_delta", p20_delta.run),` with the import.

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_delta.py`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/migrator/phases/p20_delta.py src/migrator/runner.py tests/test_delta.py
git commit -m "feat(delta): compare listing against mirror state with a sanity floor"
```

---

### Task 12: Plan phase (ceiling, disk, batching)

**Files:**
- Create: `src/migrator/phases/p30_plan.py`, `tests/test_plan.py`
- Modify: `src/migrator/runner.py` (register `plan`)

**Interfaces:**
- Consumes: `delta_changed`, `cfg.budget.*`, `shutil.disk_usage`.
- Produces: `p30_plan.run(ctx)` writing `batches` (status PLANNED) and `batch_items` (status PLANNED) for `ctx.run_id`; sets `runs.planned_batches`; outputs `{batches, files, bytes, largest_file, tree_bytes, free_bytes}`. `pack(rows, batch_bytes) -> list[list[row]]` is the pure packer.

- [ ] **Step 1: Write the failing tests**

`tests/test_plan.py`:

```python
from __future__ import annotations

import pytest

from migrator.config import Budget
from migrator.phases import p30_plan
from migrator.phases.base import PhaseContext, PhaseError


def _ctx(state_context, budget=None):
    cfg, paths, state, logger, runtime = state_context
    if budget is not None:
        from dataclasses import replace
        cfg = replace(cfg, budget=budget)
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=False)
    phase_run_id = state.start_phase(30, "30_plan", apply=False, inputs={})
    return PhaseContext(cfg, paths, state, logger, False, phase_run_id, run_id, runtime)


def _changed(ctx, rows):
    with ctx.state.connection:
        ctx.state.connection.executemany(
            "INSERT INTO delta_changed(run_id, path_lower, path_display, size, content_hash) VALUES (?, ?, ?, ?, ?)",
            [(ctx.run_id, p.lower(), p, s, "h") for p, s in rows],
        )
    with ctx.state.connection:
        cursor = ctx.state.connection.execute(
            """INSERT INTO dropbox_inventory_runs(started_at, completed_at, status, account_id, root_namespace_id, purpose)
               VALUES ('now','now','COMPLETE','dbid:test-account','ns','run:1')""")
        inventory_id = int(cursor.lastrowid)
        ctx.state.connection.executemany(
            """INSERT INTO dropbox_objects(inventory_id, object_key, tag, name, path_display, path_lower, comparison_key,
               size, content_hash, is_downloadable, raw_json, first_page, last_page)
               VALUES (?, ?, 'file', ?, ?, ?, ?, ?, 'h', 1, '{}', 1, 1)""",
            [(inventory_id, p.lower(), p.rsplit("/", 1)[-1], p, p.lower(), p.lower(), s) for p, s in rows],
        )
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)


def test_pack_is_greedy_in_path_order_with_oversized_alone():
    rows = [{"path_lower": "/a", "size": 6}, {"path_lower": "/b", "size": 5},
            {"path_lower": "/c", "size": 20}, {"path_lower": "/d", "size": 1}]
    batches = p30_plan.pack(rows, batch_bytes=10, batch_files=5000)
    assert [[r["path_lower"] for r in b] for b in batches] == [["/a"], ["/b"], ["/c"], ["/d"]]


def test_pack_caps_files_per_batch():
    rows = [{"path_lower": f"/{i}", "size": 1} for i in range(5)]
    assert [len(b) for b in p30_plan.pack(rows, batch_bytes=100, batch_files=2)] == [2, 2, 1]


def test_plan_writes_batches_and_items(state_context, monkeypatch):
    ctx = _ctx(state_context, Budget(batch_gb=10 / 1024**3, ceiling_gb=1, disk_headroom_gb=0))
    _changed(ctx, [("/A/one", 6), ("/A/two", 5), ("/big", 20)])
    monkeypatch.setattr(p30_plan.shutil, "disk_usage", lambda _: type("U", (), {"free": 10**9})())
    result = p30_plan.run(ctx)
    assert result.outputs["batches"] == 3 and result.outputs["files"] == 3 and result.outputs["largest_file"] == 20
    rows = ctx.state.connection.execute(
        "SELECT number, bytes, file_count, status FROM batches WHERE run_id=? ORDER BY number", (ctx.run_id,)).fetchall()
    assert [tuple(r) for r in rows] == [(1, 6, 1, "PLANNED"), (2, 5, 1, "PLANNED"), (3, 20, 1, "PLANNED")]
    assert ctx.state.current_run()["planned_batches"] == 3
    items = ctx.state.connection.execute("SELECT status FROM batch_items").fetchall()
    assert {r["status"] for r in items} == {"PLANNED"}


def test_plan_refuses_tree_over_ceiling(state_context, monkeypatch):
    ctx = _ctx(state_context, Budget(ceiling_gb=1 / 1024**3))
    _changed(ctx, [("/a", 2)])
    monkeypatch.setattr(p30_plan.shutil, "disk_usage", lambda _: type("U", (), {"free": 10**9})())
    with pytest.raises(PhaseError, match="CEILING"):
        p30_plan.run(ctx)


def test_plan_refuses_file_disk_cannot_hold_twice(state_context, monkeypatch):
    ctx = _ctx(state_context, Budget(batch_gb=1, ceiling_gb=1, disk_headroom_gb=0))
    _changed(ctx, [("/a", 600)])
    monkeypatch.setattr(p30_plan.shutil, "disk_usage", lambda _: type("U", (), {"free": 1000})())
    with pytest.raises(PhaseError, match="disk"):
        p30_plan.run(ctx)


def test_plan_is_idempotent_within_a_run(state_context, monkeypatch):
    ctx = _ctx(state_context, Budget(ceiling_gb=1, disk_headroom_gb=0))
    _changed(ctx, [("/a", 2)])
    monkeypatch.setattr(p30_plan.shutil, "disk_usage", lambda _: type("U", (), {"free": 10**9})())
    p30_plan.run(ctx)
    p30_plan.run(ctx)
    assert ctx.state.connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_plan.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write p30_plan.py**

```python
from __future__ import annotations

import shutil
from typing import Any

from .base import PhaseContext, PhaseError, PhaseResult

PHASE = "30_plan"


def pack(rows: list[Any], batch_bytes: int, batch_files: int) -> list[list[Any]]:
    """Greedy first-fit in path order by bytes and by file count (budget.batch_files
    explains why); a file over batch_bytes is a batch by itself."""
    batches: list[list[Any]] = []
    current: list[Any] = []
    current_bytes = 0
    for row in rows:
        size = int(row["size"])
        if size > batch_bytes:
            if current:
                batches.append(current)
                current, current_bytes = [], 0
            batches.append([row])
            continue
        if current and (current_bytes + size > batch_bytes or len(current) >= batch_files):
            batches.append(current)
            current, current_bytes = [], 0
        current.append(row)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    connection = ctx.state.connection
    tree_bytes = int(connection.execute(
        "SELECT COALESCE(SUM(size), 0) FROM dropbox_objects WHERE inventory_id=? AND tag='file' AND is_downloadable=1",
        (run["inventory_id"],),
    ).fetchone()[0])
    budget = ctx.cfg.budget
    if tree_bytes > budget.ceiling_bytes:
        raise PhaseError(f"tree is {tree_bytes} bytes, over CEILING_GB ({budget.ceiling_bytes} bytes)")
    rows = connection.execute(
        "SELECT * FROM delta_changed WHERE run_id=? ORDER BY path_lower", (ctx.run_id,)
    ).fetchall()
    largest = max((int(r["size"]) for r in rows), default=0)
    free = shutil.disk_usage(ctx.paths.root).free
    needed = min(budget.batch_bytes, sum(int(r["size"]) for r in rows)) + largest + budget.headroom_bytes
    if largest and free < needed:
        raise PhaseError(f"disk cannot hold a batch plus its round-trip copy: {free} free, {needed} needed")
    batches = pack(rows, budget.batch_bytes, budget.batch_files)
    with connection:
        # A PLANNED batch from any run was never executed; each run re-plans from the
        # state, so those rows are dead weight in every checkpoint that follows.
        connection.execute(
            "DELETE FROM batch_items WHERE batch_id IN (SELECT id FROM batches WHERE status='PLANNED')")
        connection.execute("DELETE FROM batches WHERE status='PLANNED'")
        for number, batch in enumerate(batches, start=1):
            cursor = connection.execute(
                "INSERT INTO batches(run_id, number, bytes, file_count, status) VALUES (?, ?, ?, ?, 'PLANNED')",
                (ctx.run_id, number, sum(int(r["size"]) for r in batch), len(batch)),
            )
            connection.executemany(
                """INSERT INTO batch_items(batch_id, path_lower, path_display, size, content_hash, status)
                   VALUES (?, ?, ?, ?, ?, 'PLANNED')""",
                [(cursor.lastrowid, r["path_lower"], r["path_display"], int(r["size"]), r["content_hash"])
                 for r in batch],
            )
    ctx.state.update_run(ctx.run_id, planned_batches=len(batches))
    outputs = {
        "batches": len(batches), "files": len(rows), "bytes": sum(int(r["size"]) for r in rows),
        "largest_file": largest, "tree_bytes": tree_bytes, "free_bytes": free,
    }
    ctx.logger.info(PHASE, "gate", "batches planned", **outputs)
    return PhaseResult(outputs=outputs)
```

- [ ] **Step 4: Register the phase**

Add `"plan": PhaseDefinition(30, "plan", "30_plan", p30_plan.run),` with the import.

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_plan.py`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add src/migrator/phases/p30_plan.py src/migrator/runner.py tests/test_plan.py
git commit -m "feat(plan): ceiling and disk guards, greedy batch packing"
```

---

### Task 13: Batch steps: fetch, verify, upload, confirm, roundtrip, checkpoint

**Files:**
- Create: `src/migrator/phases/batch.py`, `tests/test_batch.py`

**Interfaces:**
- Consumes: `DropboxRcloneProvider.copy_files_from`, `ProtonCLIProvider.upload_tree/list_folder/download_file`, `child_cli_path`, `escape_component`, `unwrap`, `hash_file`, `walk_tree`, `statefile.push`, `Store`.
- Produces (all take `ctx` and a `batch_id`):
  `fetch(ctx, rclone, batch_id) -> dict`, `verify(ctx, batch_id) -> dict`, `upload(ctx, proton, batch_id) -> dict`, `confirm(ctx, proton, batch_id) -> dict`, `roundtrip(ctx, proton, batch_id) -> dict`, `checkpoint(ctx, store, batch_id) -> dict`; helpers `local_path(paths, path_display) -> Path`, `parent_cli_path(destination, path_display) -> str`, `history_label(ctx) -> str` (the run's start epoch, the history key prefix every push uses), `items(ctx, batch_id, status=None) -> list[Row]`. Each returns counts that Task 14 stores in `batches.details_json`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/conftest.py`:

```python
from migrator.providers.proton_cli import ProtonCLIError


class FakeProton:
    """Stand-in for ProtonCLIProvider: canned listings, canned download bytes."""

    def __init__(self, listings: dict[str, list[dict]], downloads: dict[str, bytes], fail_list=()):
        self.listings = listings
        self.downloads = downloads
        self.fail_list = set(fail_list)
        self.uploads = []
        self.downloaded = []

    def root_uid(self, phase):
        return "uid-destination"

    def upload_tree(self, sources, destination, phase):
        self.uploads.append(([str(s) for s in sources], destination))
        return '{"ok":true}'

    def list_folder(self, path, phase):
        if path in self.fail_list:
            raise ProtonCLIError("EXIT_1")
        return self.listings[path]

    def download_file(self, remote_path, local_parent: Path, phase):
        self.downloaded.append(remote_path)
        local_parent.mkdir(parents=True, exist_ok=True)
        (local_parent / "file").write_bytes(self.downloads[remote_path])


def proton_node(uid, name, size, sha1, kind="file"):
    """One entry as `proton-drive filesystem list -j` returns it."""
    return {"uid": uid, "name": {"ok": True, "value": name}, "type": kind,
            "activeRevision": {"claimedSize": size, "claimedDigests": {"sha1": sha1, "sha1Verified": True}}}
```

`tests/test_batch.py`:

```python
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
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=False)
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
                (batch_id, display.lower(), display, len(content), hashes.dropbox_content_hash),
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


def test_verify_records_hashes_and_skips_a_mismatch(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"hello", "/Docs/b.txt": b"world"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    (ctx.paths.staging / "Docs" / "b.txt").write_bytes(b"edited mid-run")
    counts = batch.verify(ctx, batch_id)
    assert counts == {"verified": 1, "bytes": 5, "hash_mismatch": 1}
    rows = {r["path_lower"]: r for r in batch.items(ctx, batch_id)}
    assert rows["/a.txt"]["status"] == "VERIFIED" and rows["/a.txt"]["sha1"] == hashlib.sha1(b"hello").hexdigest()
    assert rows["/docs/b.txt"]["status"] == "HASH_MISMATCH"
    assert not (ctx.paths.staging / "Docs").exists()  # wrong bytes never reach the upload


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
    assert batch.upload(ctx, proton, batch_id) == {"uploaded_files": 2, "uploaded_bytes": 3}
    sources, destination = proton.uploads[0]
    assert sources == [str(ctx.paths.staging / "Docs"), str(ctx.paths.staging / "b.txt")]
    assert destination == "/my-files/Dropbox"


def test_confirm_matches_name_size_sha1_and_records_uid(state_context):
    ctx = _ctx(state_context)
    files = {"/Docs/a.txt": b"aa", "/Docs/bad.txt": b"bb", "/Docs/missing.txt": b"cc"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    sha_a = hashlib.sha1(b"aa").hexdigest()
    proton = FakeProton({"/my-files/Dropbox/Docs": [
        proton_node("u-a", "a.txt", 2, sha_a), proton_node("u-bad", "bad.txt", 2, "0000"),
    ]}, {})
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
    proton = FakeProton({"/my-files/Dropbox/D": [proton_node("u1", "x.txt", 1, sha_x), proton_node("u2", "x.txt", 9, "zz")]},
                        {}, fail_list=["/my-files/Dropbox/E"])
    counts = batch.confirm(ctx, proton, batch_id)
    rows = {r["path_lower"]: r for r in batch.items(ctx, batch_id)}
    assert rows["/d/x.txt"]["status"] == "CONFIRMED" and rows["/d/x.txt"]["cli_path"] == "/my-files/Dropbox/D/u1"
    assert rows["/e/y.txt"]["status"] == "CONFIRM_FAILED"
    assert counts == {"confirmed": 1, "confirm_failed": 1}


def test_roundtrip_compares_bytes(state_context):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"same", "/b.txt": b"orig"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton({"/my-files/Dropbox": [
        proton_node("ua", "a.txt", 4, hashlib.sha1(b"same").hexdigest()),
        proton_node("ub", "b.txt", 4, hashlib.sha1(b"orig").hexdigest()),
    ]}, {"/my-files/Dropbox/a.txt": b"same", "/my-files/Dropbox/b.txt": b"diff"})
    batch.confirm(ctx, proton, batch_id)
    counts = batch.roundtrip(ctx, proton, batch_id)
    assert counts == {"roundtrip_ok": 1, "roundtrip_mismatch": 1, "roundtrip_bytes": 8}
    rows = {r["path_lower"]: r["status"] for r in batch.items(ctx, batch_id)}
    assert rows == {"/a.txt": "ROUNDTRIP_OK", "/b.txt": "ROUNDTRIP_MISMATCH"}
    assert not any(ctx.paths.roundtrip.iterdir())


def test_checkpoint_merges_only_verified_rows_and_pushes(state_context, plain_crypt):
    ctx = _ctx(state_context)
    files = {"/a.txt": b"same", "/b.txt": b"orig"}
    batch_id = _batch(ctx, files)
    batch.fetch(ctx, FakeRclone(files), batch_id)
    batch.verify(ctx, batch_id)
    proton = FakeProton({"/my-files/Dropbox": [
        proton_node("ua", "a.txt", 4, hashlib.sha1(b"same").hexdigest()),
        proton_node("ub", "b.txt", 4, hashlib.sha1(b"orig").hexdigest()),
    ]}, {"/my-files/Dropbox/a.txt": b"same", "/my-files/Dropbox/b.txt": b"diff"})
    batch.confirm(ctx, proton, batch_id)
    batch.roundtrip(ctx, proton, batch_id)
    store = FakeStore()
    counts = batch.checkpoint(ctx, store, batch_id)
    assert counts == {"checkpointed": 1, "failed": 1}
    mirrored = ctx.state.connection.execute("SELECT path_lower, proton_uid, run_id FROM mirror_objects").fetchall()
    assert [tuple(r) for r in mirrored] == [("/a.txt", "ua", ctx.run_id)]
    assert ctx.state.connection.execute("SELECT status FROM batches").fetchone()[0] == "FAILED"
    assert sorted(store.objects) == [".state/history/1-1.sqlite.xz.age", ".state/state.sqlite.xz.age"]
    assert not any(ctx.paths.staging.iterdir())
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_batch.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write batch.py**

```python
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from .. import statefile
from ..filesystem import walk_tree
from ..hashing import hash_file
from ..logging import utc_now
from ..providers.proton_cli import ProtonCLIError, child_cli_path, escape_component, unwrap
from ..store import Store
from .base import PhaseContext, PhaseError

PHASE = "40_batches"


def items(ctx: PhaseContext, batch_id: int, status: str | None = None) -> list[sqlite3.Row]:
    clause = " AND status=?" if status else ""
    params: tuple[Any, ...] = (batch_id, status) if status else (batch_id,)
    return ctx.state.connection.execute(
        f"SELECT * FROM batch_items WHERE batch_id=?{clause} ORDER BY path_lower", params
    ).fetchall()


def _set_item(ctx: PhaseContext, batch_id: int, path_lower: str, status: str, **columns: Any) -> None:
    assignments = ", ".join(["status=?", *(f"{name}=?" for name in columns)])
    with ctx.state.connection:
        ctx.state.connection.execute(
            f"UPDATE batch_items SET {assignments} WHERE batch_id=? AND path_lower=?",
            (status, *columns.values(), batch_id, path_lower),
        )


def _details(reason: str, **extra: Any) -> str:
    return json.dumps({"reason": reason, **extra}, sort_keys=True)


def local_path(paths: Any, path_display: str) -> Path:
    return paths.staging / path_display.lstrip("/")


def parent_cli_path(destination: str, path_display: str) -> str:
    parts = PurePosixPath(path_display.lstrip("/")).parent.parts
    if not parts:
        return destination.rstrip("/")
    return destination.rstrip("/") + "/" + "/".join(escape_component(part) for part in parts)


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


def fetch(ctx: PhaseContext, rclone: Any, batch_id: int) -> dict[str, int]:
    """rclone copies by path_lower (Dropbox's own spelling, so the match is exact);
    each file is then moved to its NFC display path."""
    _clear(ctx.paths.staging)
    rows = items(ctx, batch_id, "PLANNED")
    list_file = ctx.paths.root / "files-from.txt"
    list_file.write_text("".join(str(r["path_lower"]).lstrip("/") + "\n" for r in rows), encoding="utf-8")
    log_path = ctx.phase_dir(PHASE) / f"rclone-copy-{batch_id}.jsonl"
    code = rclone.copy_files_from(list_file, ctx.paths.staging, log_path)
    counts: Counter[str] = Counter()
    for row in rows:
        target = local_path(ctx.paths, str(row["path_display"]))
        # rclone names the copy after the listed path, which is path_lower; a backend that
        # answers with display casing is not ruled out, so either spelling is accepted.
        source = next((p for p in (ctx.paths.staging / str(row["path_lower"]).lstrip("/"), target)
                       if p.is_file()), None)
        if source is None:
            _set_item(ctx, batch_id, row["path_lower"], "VANISHED", details_json=_details("vanished"))
            counts["vanished"] += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            os.replace(source, target)
        _set_item(ctx, batch_id, row["path_lower"], "FETCHED")
        counts["fetched"] += 1
    if rows and counts["vanished"] == len(rows) and code == 0:
        # rclone copied everything, yet nothing is where the listing said: the staging
        # layout and path_lower disagree. Silent as VANISHED, this would checkpoint an
        # empty batch as success and never mirror a byte.
        raise PhaseError("rclone exited 0 but no listed file reached staging under its listed name")
    _prune_empty_dirs(ctx.paths.staging)
    list_file.unlink(missing_ok=True)
    ctx.logger.info(PHASE, "fetch", "batch fetched", batch=batch_id, **counts)
    return {"fetched": counts["fetched"], "vanished": counts["vanished"]}


def verify(ctx: PhaseContext, batch_id: int) -> dict[str, int]:
    """A mismatch is a file edited between listing and fetch: removed from staging so the
    upload never sees it, counted, never recorded; the next listing catches it. Every
    file mismatching is corruption, not editing, and fails the batch."""
    counts: Counter[str] = Counter()
    rows = items(ctx, batch_id, "FETCHED")
    for row in rows:
        staged = local_path(ctx.paths, str(row["path_display"]))
        hashes = hash_file(staged)
        if hashes.size != int(row["size"]) or hashes.dropbox_content_hash != str(row["content_hash"]):
            staged.unlink()
            _set_item(ctx, batch_id, row["path_lower"], "HASH_MISMATCH", details_json=_details("content_hash"))
            counts["hash_mismatch"] += 1
            continue
        _set_item(ctx, batch_id, row["path_lower"], "VERIFIED", sha1=hashes.sha1, sha256=hashes.sha256)
        counts["verified"] += 1
        counts["bytes"] += hashes.size
    _prune_empty_dirs(ctx.paths.staging)
    if rows and counts["hash_mismatch"] == len(rows):
        raise PhaseError("content hash mismatch on every staged file; the fetch path is corrupt")
    ctx.logger.info(PHASE, "verify", "batch verified", batch=batch_id, **counts)
    return {"verified": counts["verified"], "bytes": counts["bytes"], "hash_mismatch": counts["hash_mismatch"]}


def upload(ctx: PhaseContext, proton: Any, batch_id: int) -> dict[str, int]:
    rows = items(ctx, batch_id, "VERIFIED")
    if not rows:
        return {"uploaded_files": 0, "uploaded_bytes": 0}
    sources = sorted(path for path in ctx.paths.staging.iterdir())
    proton.upload_tree(sources, ctx.cfg.proton.destination, PHASE)
    total = sum(int(r["size"]) for r in rows)
    ctx.logger.info(PHASE, "upload", "batch uploaded", batch=batch_id, files=len(rows), bytes=total)
    return {"uploaded_files": len(rows), "uploaded_bytes": total}


def confirm(ctx: PhaseContext, proton: Any, batch_id: int) -> dict[str, int]:
    """Independent observation: list each parent and match name, size, SHA-1."""
    by_parent: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in items(ctx, batch_id, "VERIFIED"):
        by_parent[parent_cli_path(ctx.cfg.proton.destination, str(row["path_display"]))].append(row)
    counts: Counter[str] = Counter()
    for parent, rows in sorted(by_parent.items()):
        try:
            children = proton.list_folder(parent, PHASE)
        except ProtonCLIError as exc:
            for row in rows:
                _set_item(ctx, batch_id, row["path_lower"], "CONFIRM_FAILED",
                          details_json=_details("parent_listing_failed", error=ctx.logger.redact(str(exc))))
                counts["confirm_failed"] += 1
            continue
        names: Counter[str] = Counter(str(unwrap(node.get("name"))) for node in children)
        by_name = {str(unwrap(node.get("name"))): node for node in children}
        for row in rows:
            name = PurePosixPath(str(row["path_display"])).name
            node = by_name.get(name)
            reason = _mismatch(node, row)
            if reason:
                _set_item(ctx, batch_id, row["path_lower"], "CONFIRM_FAILED", details_json=_details(reason))
                counts["confirm_failed"] += 1
                continue
            uid = str(unwrap(node["uid"]))
            _set_item(ctx, batch_id, row["path_lower"], "CONFIRMED", proton_uid=uid,
                      cli_path=child_cli_path(parent, name, uid, names[name] > 1))
            counts["confirmed"] += 1
    ctx.logger.info(PHASE, "confirm", "batch confirmed by listing", batch=batch_id, **counts)
    return {"confirmed": counts["confirmed"], "confirm_failed": counts["confirm_failed"]}


def _mismatch(node: dict[str, Any] | None, row: sqlite3.Row) -> str | None:
    if node is None:
        return "absent"
    if str(unwrap(node.get("type"))).casefold() != "file":
        return "type"
    active = unwrap(node.get("activeRevision")) or {}
    claimed = unwrap(active.get("claimedSize"))
    if claimed is None or int(claimed) != int(row["size"]):  # an empty file claims 0, not "missing"
        return "size"
    digests = unwrap(active.get("claimedDigests")) or {}
    if str(unwrap(digests.get("sha1")) or "").casefold() != str(row["sha1"]).casefold():
        return "sha1"
    return None


def _downloaded_regular_file(staging: Path) -> Path:
    objects = list(walk_tree(staging, hash_files=False))
    files = [item for item in objects if item.object_type == "file"]
    if len(files) != 1 or any(item.stat_error for item in objects):
        raise PhaseError("Proton download did not produce exactly one regular file")
    return staging / files[0].relative_path


def roundtrip(ctx: PhaseContext, proton: Any, batch_id: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in items(ctx, batch_id, "CONFIRMED"):
        staging = ctx.paths.roundtrip / hashlib.sha256(str(row["proton_uid"]).encode()).hexdigest()[:16]
        _clear(staging)
        try:
            proton.download_file(str(row["cli_path"]), staging, PHASE)
            actual = hash_file(_downloaded_regular_file(staging))
            expected = (int(row["size"]), str(row["sha1"]), str(row["sha256"]), str(row["content_hash"]))
            observed = (actual.size, actual.sha1, actual.sha256, actual.dropbox_content_hash)
            if expected == observed:
                _set_item(ctx, batch_id, row["path_lower"], "ROUNDTRIP_OK")
                counts["roundtrip_ok"] += 1
            else:
                _set_item(ctx, batch_id, row["path_lower"], "ROUNDTRIP_MISMATCH", details_json=_details("bytes"))
                counts["roundtrip_mismatch"] += 1
            counts["roundtrip_bytes"] += actual.size
        except (OSError, ProtonCLIError, PhaseError) as exc:
            _set_item(ctx, batch_id, row["path_lower"], "ROUNDTRIP_MISMATCH",
                      details_json=_details("download_failed", error=ctx.logger.redact(str(exc))))
            counts["roundtrip_mismatch"] += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    ctx.logger.info(PHASE, "roundtrip", "batch round-tripped", batch=batch_id, **counts)
    return {"roundtrip_ok": counts["roundtrip_ok"], "roundtrip_mismatch": counts["roundtrip_mismatch"],
            "roundtrip_bytes": counts["roundtrip_bytes"]}


def checkpoint(ctx: PhaseContext, store: Store, batch_id: int) -> dict[str, int]:
    """Merge every ROUNDTRIP_OK row, then push the state. Always the last step."""
    connection = ctx.state.connection
    good = items(ctx, batch_id, "ROUNDTRIP_OK")
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
            [(r["path_lower"], r["path_display"], int(r["size"]), r["content_hash"], r["sha1"], r["sha256"],
              r["proton_uid"], ctx.run_id, now) for r in good],
        )
        connection.execute(
            "UPDATE batch_items SET status='CHECKPOINTED' WHERE batch_id=? AND status='ROUNDTRIP_OK'", (batch_id,))
        failed = int(connection.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND status IN ('CONFIRM_FAILED', 'ROUNDTRIP_MISMATCH')",
            (batch_id,)).fetchone()[0])
        status = "FAILED" if failed else "CHECKPOINTED"
        connection.execute("UPDATE batches SET status=?, completed_at=? WHERE id=?", (status, now, batch_id))
    number = int(connection.execute("SELECT number FROM batches WHERE id=?", (batch_id,)).fetchone()[0])
    statefile.push(ctx.state, ctx.runtime, ctx.paths, store, label=f"{history_label(ctx)}-{number}")
    _clear(ctx.paths.staging)
    ctx.logger.info(PHASE, "checkpoint", "batch checkpointed", batch=batch_id, checkpointed=len(good), failed=failed)
    return {"checkpointed": len(good), "failed": failed}
```

- [ ] **Step 4: Run tests**

Run: `task test -- tests/test_batch.py`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/migrator/phases/batch.py tests/test_batch.py
git commit -m "feat(batch): fetch, verify, upload, confirm, round-trip, checkpoint"
```

---

### Task 14: Batches phase: the budgeted loop and the chain decision

**Files:**
- Create: `src/migrator/phases/p40_batches.py`, `tests/test_batches_loop.py`
- Modify: `src/migrator/runner.py` (register `batches`)

**Interfaces:**
- Consumes: every `batch.*` function, `ProtonCLIProvider.root_uid`, `session.writeback`, `Store`, `runs.budget_minutes`, `runs.start_epoch`.
- Produces: `p40_batches.run(ctx)`; sets `runs.remaining_batches` and `runs.chain`; outputs `{planned, completed, remaining, chain, budget_seconds, elapsed_seconds, longest_batch_seconds, **summed step counts}`; `should_start(elapsed, longest, budget, completed) -> bool`; module attribute `now = time.time` for tests.

- [ ] **Step 1: Write the failing tests**

`tests/test_batches_loop.py`:

```python
from __future__ import annotations

import json

import pytest

from migrator.phases import batch, p40_batches
from migrator.phases.base import PhaseContext, PhaseError


def _ctx(state_context, apply=True, budget_minutes=10):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(start_epoch=1000, hour_utc=0, weekday=0, budget_minutes=budget_minutes,
                             host="t", reconcile=False)
    phase_run_id = state.start_phase(40, "40_batches", apply=apply, inputs={})
    return PhaseContext(cfg, paths, state, logger, apply, phase_run_id, run_id, runtime)


def _planned(ctx, count):
    with ctx.state.connection:
        for number in range(1, count + 1):
            cursor = ctx.state.connection.execute(
                "INSERT INTO batches(run_id, number, bytes, file_count, status) VALUES (?, ?, 10, 1, 'PLANNED')",
                (ctx.run_id, number))
            ctx.state.connection.execute(
                """INSERT INTO batch_items(batch_id, path_lower, path_display, size, content_hash, status)
                   VALUES (?, ?, ?, 10, 'h', 'PLANNED')""", (cursor.lastrowid, f"/f{number}", f"/f{number}"))
    ctx.state.update_run(ctx.run_id, planned_batches=count)


def _fake_steps(monkeypatch, seconds_per_batch, failing=()):
    clock = {"t": 1000.0}
    monkeypatch.setattr(p40_batches, "now", lambda: clock["t"])
    monkeypatch.setattr(p40_batches, "Store", lambda runtime, paths: object())
    monkeypatch.setattr(p40_batches, "ProtonCLIProvider", lambda *a, **k: type("P", (), {"root_uid": lambda self, phase: "uid-destination"})())
    monkeypatch.setattr(p40_batches, "DropboxRcloneProvider", lambda *a, **k: object())
    monkeypatch.setattr(p40_batches.session, "writeback", lambda *a: False)
    for name in ("fetch", "verify", "upload", "confirm", "roundtrip"):
        monkeypatch.setattr(batch, name, lambda ctx, *a, _n=name: {_n: 1})

    def checkpoint(ctx, store, batch_id):
        clock["t"] += seconds_per_batch
        failed = 1 if batch_id in failing else 0
        with ctx.state.connection:
            ctx.state.connection.execute("UPDATE batches SET status=? WHERE id=?",
                                         ("FAILED" if failed else "CHECKPOINTED", batch_id))
        return {"checkpointed": 1 - failed, "failed": failed}

    monkeypatch.setattr(batch, "checkpoint", checkpoint)
    return clock


def test_should_start_rule():
    assert p40_batches.should_start(elapsed=0, longest=0, budget=600, completed=0)
    assert p40_batches.should_start(elapsed=5000, longest=0, budget=600, completed=0)  # first batch always
    assert p40_batches.should_start(elapsed=400, longest=100, budget=600, completed=1)
    assert not p40_batches.should_start(elapsed=550, longest=100, budget=600, completed=1)


def test_all_batches_fit_no_chain(state_context, monkeypatch):
    ctx = _ctx(state_context, budget_minutes=10)
    _planned(ctx, 3)
    _fake_steps(monkeypatch, seconds_per_batch=60)
    result = p40_batches.run(ctx)
    assert result.outputs["completed"] == 3 and result.outputs["remaining"] == 0
    assert result.outputs["chain"] is False
    run = ctx.state.current_run()
    assert run["remaining_batches"] == 0 and run["chain"] == 0
    details = json.loads(ctx.state.connection.execute("SELECT details_json FROM batches WHERE number=1").fetchone()[0])
    assert details["fetch"] == 1 and "seconds" in details


def test_budget_stops_and_chains(state_context, monkeypatch):
    ctx = _ctx(state_context, budget_minutes=5)
    _planned(ctx, 5)
    _fake_steps(monkeypatch, seconds_per_batch=120)
    result = p40_batches.run(ctx)
    # 0s -> batch1 (120s) -> 120+120<300 batch2 -> 240+120>300 stop
    assert result.outputs["completed"] == 2 and result.outputs["remaining"] == 3
    assert result.outputs["chain"] is True
    assert ctx.state.current_run()["chain"] == 1


def test_failed_batch_stops_run_without_chain(state_context, monkeypatch):
    ctx = _ctx(state_context)
    _planned(ctx, 3)
    first = ctx.state.connection.execute("SELECT id FROM batches WHERE number=2").fetchone()[0]
    _fake_steps(monkeypatch, seconds_per_batch=1, failing={first})
    with pytest.raises(PhaseError, match="failed"):
        p40_batches.run(ctx)
    run = ctx.state.current_run()
    assert run["remaining_batches"] == 2 and run["chain"] == 0


def test_no_progress_does_not_chain(state_context, monkeypatch):
    ctx = _ctx(state_context)
    _planned(ctx, 2)
    first = ctx.state.connection.execute("SELECT id FROM batches WHERE number=1").fetchone()[0]
    _fake_steps(monkeypatch, seconds_per_batch=1, failing={first})
    with pytest.raises(PhaseError):
        p40_batches.run(ctx)
    assert ctx.state.current_run()["chain"] == 0


def test_without_apply_is_planned_only(state_context, monkeypatch):
    ctx = _ctx(state_context, apply=False)
    _planned(ctx, 2)
    result = p40_batches.run(ctx)
    assert result.status == "PLANNED" and result.outputs["planned"] == 2
    assert ctx.state.current_run()["remaining_batches"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_batches_loop.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write p40_batches.py**

```python
from __future__ import annotations

import json
import time
from collections import Counter

from .. import session
from ..logging import utc_now
from ..providers.dropbox_rclone import DropboxRcloneProvider
from ..providers.proton_cli import ProtonCLIProvider
from ..store import Store
from . import batch
from .base import PhaseContext, PhaseError, PhaseResult

PHASE = "40_batches"
now = time.time


def should_start(*, elapsed: float, longest: float, budget: float, completed: int) -> bool:
    """The first batch always runs; afterwards a batch starts only if the longest
    batch so far would still finish inside the budget."""
    return completed == 0 or elapsed + longest <= budget


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    planned = ctx.state.connection.execute(
        "SELECT * FROM batches WHERE run_id=? AND status='PLANNED' ORDER BY number", (ctx.run_id,)
    ).fetchall()
    if not ctx.apply:
        return PhaseResult(status="PLANNED", outputs={"planned": len(planned)})
    store = Store(ctx.runtime, ctx.paths)
    proton = ProtonCLIProvider(ctx.cfg, ctx.state, ctx.logger,
                               after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store))
    rclone = DropboxRcloneProvider(ctx.cfg, ctx.paths, ctx.state, ctx.logger)
    # Unconditional: the one Proton call a quiet night is guaranteed to make. It forces any
    # pending token rotation (after_call writes the session back) and keeps the 60-day
    # idle expiry away, besides gating on the destination UID.
    proton.root_uid(PHASE)
    budget = int(run["budget_minutes"]) * 60
    start_epoch = int(run["start_epoch"])
    durations: list[float] = []
    totals: Counter[str] = Counter()
    completed = 0
    failed_batch = None
    for row in planned:
        elapsed = now() - start_epoch
        if not should_start(elapsed=elapsed, longest=max(durations, default=0.0), budget=budget,
                            completed=completed):
            break
        batch_id = int(row["id"])
        began = now()
        details: dict[str, object] = {}
        with ctx.state.connection:
            ctx.state.connection.execute("UPDATE batches SET started_at=? WHERE id=?", (utc_now(), batch_id))
        steps = (
            ("fetch", lambda: batch.fetch(ctx, rclone, batch_id)),
            ("verify", lambda: batch.verify(ctx, batch_id)),
            ("upload", lambda: batch.upload(ctx, proton, batch_id)),
            ("confirm", lambda: batch.confirm(ctx, proton, batch_id)),
            ("roundtrip", lambda: batch.roundtrip(ctx, proton, batch_id)),
            ("checkpoint", lambda: batch.checkpoint(ctx, store, batch_id)),
        )
        try:
            for name, step in steps:
                step_began = now()
                details.update(step())
                details[f"{name}_seconds"] = round(now() - step_began, 1)
        except Exception:  # provider errors included: the batch row must say FAILED
            with ctx.state.connection:
                ctx.state.connection.execute(
                    "UPDATE batches SET status='FAILED', completed_at=?, details_json=? WHERE id=?",
                    (utc_now(), json.dumps(details, sort_keys=True), batch_id))
            ctx.state.update_run(ctx.run_id, remaining_batches=len(planned) - completed, chain=0)
            raise
        details["seconds"] = round(now() - began, 1)
        with ctx.state.connection:
            ctx.state.connection.execute("UPDATE batches SET details_json=? WHERE id=?",
                                         (json.dumps(details, sort_keys=True), batch_id))
        durations.append(now() - began)
        totals.update({k: v for k, v in details.items() if isinstance(v, int) and not k.endswith("_seconds")})
        status = ctx.state.connection.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()[0]
        if status == "FAILED":
            failed_batch = int(row["number"])
            break
        completed += 1
    remaining = len(planned) - completed
    chain = remaining > 0 and completed > 0 and failed_batch is None
    ctx.state.update_run(ctx.run_id, remaining_batches=remaining, chain=int(chain))
    outputs = {
        "planned": len(planned), "completed": completed, "remaining": remaining, "chain": chain,
        "budget_seconds": budget, "elapsed_seconds": round(now() - start_epoch, 1),
        "longest_batch_seconds": round(max(durations, default=0.0), 1), **dict(totals),
    }
    if failed_batch is not None:
        ctx.logger.error(PHASE, "gate", "a batch failed confirmation or round-trip",
                         provider_category="VERIFICATION_FAILURE", batch=failed_batch, **outputs)
        raise PhaseError(f"batch {failed_batch} failed confirmation or round-trip")
    if remaining and completed == 0:
        raise PhaseError("no batch completed inside the budget; not chaining")
    ctx.logger.info(PHASE, "gate", "batches finished for this run", **outputs)
    return PhaseResult(outputs=outputs)
```

- [ ] **Step 4: Register the phase**

Add `"batches": PhaseDefinition(40, "batches", "40_batches", p40_batches.run),` with the import.

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_batches_loop.py tests/test_batch.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/migrator/phases/p40_batches.py src/migrator/runner.py tests/test_batches_loop.py
git commit -m "feat(batches): budgeted batch loop with chain decision"
```

---

### Task 15: Trash phase

**Files:**
- Create: `src/migrator/phases/p50_trash.py`, `tests/test_trash.py`
- Modify: `src/migrator/runner.py` (register `trash`)

**Interfaces:**
- Consumes: `delta_deleted`, `runs.remaining_batches`, `ProtonCLIProvider.list_folder/trash`, `batch.parent_cli_path`, `child_cli_path`, `unwrap`, `session.writeback`, `Store`, `statefile.push`.
- Produces: `p50_trash.run(ctx)`; records `deletions` rows (TRASHED, NOT_FOUND, or LISTING_FAILED when the parent folder could not be listed), removes the TRASHED and NOT_FOUND rows from `mirror_objects` so a LISTING_FAILED file is retried the next night, pushes the state with label `<epoch>-trash`; outputs `{planned, trashed, not_found, listing_failed, folders}`. Runs only when `remaining_batches == 0`; otherwise returns PASS with `{"skipped": "batches remain"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_trash.py`:

```python
from __future__ import annotations

import pytest

from migrator.phases import p50_trash
from migrator.phases.base import PhaseContext
from conftest import FakeProton, FakeStore, proton_node


def _ctx(state_context, apply=True, remaining=0):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=False)
    state.update_run(run_id, planned_batches=1, remaining_batches=remaining)
    phase_run_id = state.start_phase(50, "50_trash", apply=apply, inputs={})
    return PhaseContext(cfg, paths, state, logger, apply, phase_run_id, run_id, runtime)


def _deleted(ctx, displays):
    with ctx.state.connection:
        for display in displays:
            ctx.state.connection.execute(
                """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256, run_id, mirrored_at)
                   VALUES (?, ?, 1, 'h', 's', 's', 0, 'now')""", (display.lower(), display))
            ctx.state.connection.execute(
                "INSERT INTO delta_deleted(run_id, path_lower, path_display) VALUES (?, ?, ?)",
                (ctx.run_id, display.lower(), display))


def _wire(monkeypatch, proton):
    monkeypatch.setattr(p50_trash, "Store", lambda runtime, paths: FakeStore())
    monkeypatch.setattr(p50_trash, "ProtonCLIProvider", lambda *a, **k: proton)
    monkeypatch.setattr(p50_trash.session, "writeback", lambda *a: False)


def test_trash_groups_by_parent_and_drops_state_rows(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context)
    _deleted(ctx, ["/Docs/a.txt", "/Docs/b.txt", "/Other/c.txt", "/Docs/never-there.txt"])
    proton = FakeProton({
        "/my-files/Dropbox/Docs": [proton_node("ua", "a.txt", 1, "s"), proton_node("ub", "b.txt", 1, "s")],
        "/my-files/Dropbox/Other": [proton_node("uc", "c.txt", 1, "s")],
    }, {})
    proton.trashed = []
    proton.trash = lambda paths, phase: proton.trashed.append(sorted(paths))
    _wire(monkeypatch, proton)
    result = p50_trash.run(ctx)
    assert result.outputs == {"planned": 4, "trashed": 3, "not_found": 1, "listing_failed": 0, "folders": 2}
    assert proton.trashed == [["/my-files/Dropbox/Docs/a.txt", "/my-files/Dropbox/Docs/b.txt"],
                              ["/my-files/Dropbox/Other/c.txt"]]
    assert ctx.state.connection.execute("SELECT COUNT(*) FROM mirror_objects").fetchone()[0] == 0
    statuses = {r["path_lower"]: r["status"] for r in ctx.state.connection.execute("SELECT * FROM deletions")}
    assert statuses["/docs/never-there.txt"] == "NOT_FOUND" and statuses["/docs/a.txt"] == "TRASHED"


def test_trash_keeps_state_rows_when_a_parent_listing_fails(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context)
    _deleted(ctx, ["/Docs/a.txt"])
    proton = FakeProton({}, {}, fail_list=["/my-files/Dropbox/Docs"])
    proton.trash = lambda paths, phase: (_ for _ in ()).throw(AssertionError("nothing to trash"))
    _wire(monkeypatch, proton)
    result = p50_trash.run(ctx)
    assert result.outputs == {"planned": 1, "trashed": 0, "not_found": 0, "listing_failed": 1, "folders": 0}
    assert ctx.state.connection.execute("SELECT COUNT(*) FROM mirror_objects").fetchone()[0] == 1
    assert ctx.state.connection.execute("SELECT status FROM deletions").fetchone()[0] == "LISTING_FAILED"


def test_trash_skips_while_batches_remain(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context, remaining=2)
    _deleted(ctx, ["/Docs/a.txt"])
    _wire(monkeypatch, FakeProton({}, {}))
    result = p50_trash.run(ctx)
    assert result.outputs == {"skipped": "batches remain", "planned": 1}
    assert ctx.state.connection.execute("SELECT COUNT(*) FROM mirror_objects").fetchone()[0] == 1


def test_trash_without_apply_is_planned(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context, apply=False)
    _deleted(ctx, ["/Docs/a.txt"])
    result = p50_trash.run(ctx)
    assert result.status == "PLANNED" and result.outputs["planned"] == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_trash.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write p50_trash.py**

```python
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import PurePosixPath

from .. import session, statefile
from ..logging import utc_now
from ..providers.proton_cli import ProtonCLIError, ProtonCLIProvider, child_cli_path, unwrap
from ..store import Store
from .base import PhaseContext, PhaseResult
from .batch import history_label, parent_cli_path

PHASE = "50_trash"


def run(ctx: PhaseContext) -> PhaseResult:
    run = ctx.state.current_run()
    connection = ctx.state.connection
    rows = connection.execute(
        "SELECT * FROM delta_deleted WHERE run_id=? ORDER BY path_lower", (ctx.run_id,)).fetchall()
    if not ctx.apply:
        return PhaseResult(status="PLANNED", outputs={"planned": len(rows)})
    if run["remaining_batches"] is None or int(run["remaining_batches"]) > 0:
        ctx.logger.info(PHASE, "gate", "trash deferred until every batch has landed", planned=len(rows))
        return PhaseResult(outputs={"skipped": "batches remain", "planned": len(rows)})
    if not rows:
        return PhaseResult(outputs={"planned": 0, "trashed": 0, "not_found": 0, "listing_failed": 0, "folders": 0})
    store = Store(ctx.runtime, ctx.paths)
    proton = ProtonCLIProvider(ctx.cfg, ctx.state, ctx.logger,
                               after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store))
    proton.root_uid(PHASE)
    by_parent: dict[str, list] = defaultdict(list)
    for row in rows:
        by_parent[parent_cli_path(ctx.cfg.proton.destination, str(row["path_display"]))].append(row)
    counts: Counter[str] = Counter()
    for parent, group in sorted(by_parent.items()):
        try:
            children = proton.list_folder(parent, PHASE)
        except ProtonCLIError:
            # The files may well still be there: keep their state rows so tomorrow retries.
            for row in group:
                _record(ctx, row, "LISTING_FAILED", None)
            counts["listing_failed"] += len(group)
            continue
        names: Counter[str] = Counter(str(unwrap(node.get("name"))) for node in children)
        by_name = {str(unwrap(node.get("name"))): node for node in children}
        targets = []
        found = []
        for row in group:
            name = PurePosixPath(str(row["path_display"])).name
            node = by_name.get(name)
            if node is None or str(unwrap(node.get("type"))).casefold() != "file":
                _record(ctx, row, "NOT_FOUND", None)
                counts["not_found"] += 1
                continue
            uid = str(unwrap(node["uid"]))
            targets.append(child_cli_path(parent, name, uid, names[name] > 1))
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
            (ctx.run_id,))
    statefile.push(ctx.state, ctx.runtime, ctx.paths, store, label=f"{history_label(ctx)}-trash")
    outputs = {"planned": len(rows), "trashed": counts["trashed"], "not_found": counts["not_found"],
               "listing_failed": counts["listing_failed"], "folders": counts["folders"]}
    ctx.logger.info(PHASE, "gate", "deleted files trashed", **outputs)
    return PhaseResult(outputs=outputs)


def _record(ctx: PhaseContext, row, status: str, uid: str | None) -> None:
    with ctx.state.connection:
        ctx.state.connection.execute(
            """INSERT OR REPLACE INTO deletions(run_id, path_lower, path_display, proton_uid, status, trashed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ctx.run_id, row["path_lower"], row["path_display"], uid, status,
             utc_now() if status == "TRASHED" else None))
```

Trashing a file whose Proton folder is empty afterwards leaves an empty folder; `ponytail:` empty folders are not retired, the weekly reconcile could add that if it ever matters.

- [ ] **Step 4: Register the phase**

Add `"trash": PhaseDefinition(50, "trash", "50_trash", p50_trash.run),` with the import.

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_trash.py`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/migrator/phases/p50_trash.py src/migrator/runner.py tests/test_trash.py
git commit -m "feat(trash): retire deleted files to Proton trash by parent listing"
```

---

### Task 16: Reconcile phase

**Files:**
- Create: `src/migrator/phases/p60_reconcile.py`, `tests/test_reconcile.py`
- Modify: `src/migrator/runner.py` (register `reconcile`)

**Interfaces:**
- Consumes: `ProtonCLIProvider.inventory` (the UID-based resumable walk into `proton_snapshots`/`proton_nodes`), `mirror_objects`, `dropbox_objects`, `filesystem.comparison_key`, `ProtonCLIProvider.trash`, `statefile.push`.
- Produces: `p60_reconcile.run(ctx)`; outputs `{skipped?, snapshot_id, proton_files, dropped, uid_refreshed, strays_trashed}`. Runs only when `runs.reconcile == 1` and `remaining_batches == 0`.

- [ ] **Step 1: Write the failing tests**

`tests/test_reconcile.py`:

```python
from __future__ import annotations

from migrator.filesystem import comparison_key
from migrator.phases import p60_reconcile
from migrator.phases.base import PhaseContext
from conftest import seed_api_inventory
from conftest import FakeStore


def _ctx(state_context, reconcile=True, remaining=0):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(start_epoch=1, hour_utc=0, weekday=0, budget_minutes=1, host="t", reconcile=reconcile)
    state.update_run(run_id, planned_batches=0, remaining_batches=remaining)
    phase_run_id = state.start_phase(60, "60_reconcile", apply=True, inputs={})
    return PhaseContext(cfg, paths, state, logger, True, phase_run_id, run_id, runtime)


def _snapshot(state, nodes):
    with state.connection:
        cursor = state.connection.execute(
            """INSERT INTO proton_snapshots(purpose, started_at, completed_at, status, destination_root, cli_version)
               VALUES ('reconcile:1', 'now', 'now', 'COMPLETE', '/my-files/Dropbox', '0.8.0')""")
        snapshot_id = int(cursor.lastrowid)
        for relative, uid, size in nodes:
            state.connection.execute(
                """INSERT INTO proton_nodes(snapshot_id, uid, parent_uid, visible_segments_json, relative_path, cli_path,
                   comparison_key, name, node_type, claimed_size, raw_json)
                   VALUES (?, ?, '__ROOT__', '[]', ?, ?, ?, ?, 'file', ?, '{}')""",
                (snapshot_id, uid, relative, "/my-files/Dropbox/" + relative, comparison_key(relative),
                 relative.rsplit("/", 1)[-1], size))
    return snapshot_id


def _mirror(state, rows):
    with state.connection:
        for display, size, uid in rows:
            state.connection.execute(
                """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256, proton_uid,
                   run_id, mirrored_at) VALUES (?, ?, ?, 'h', 's', 's', ?, 0, 'now')""",
                (display.lower(), display, size, uid))


def test_reconcile_drops_missing_or_missized_and_trashes_strays(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context)
    inventory_id = seed_api_inventory(ctx.state, "run:1", [("/Keep/ok.txt", 3, "h", 1, "file"),
                                                            ("/Keep/pending.txt", 7, "h", 1, "file")])
    ctx.state.update_run(ctx.run_id, inventory_id=inventory_id)
    _mirror(ctx.state, [("/Keep/ok.txt", 3, None), ("/Keep/lost.txt", 2, "u-lost"), ("/Keep/bad.txt", 5, "u-bad")])
    _snapshot(ctx.state, [("Old/x", "u-old", 1)])  # last week's walk; pruned by this run
    snapshot_id = _snapshot(ctx.state, [("Keep/ok.txt", "u-ok", 3), ("Keep/bad.txt", "u-bad", 99),
                                        ("Keep/pending.txt", "u-p", 7), ("Stray/x.bin", "u-stray", 1)])
    trashed = []
    fake = type("P", (), {
        "root_uid": lambda self, phase: "uid-destination",
        "inventory": lambda self, purpose, phase, reuse_complete=True: snapshot_id,
        "trash": lambda self, paths, phase: trashed.extend(paths),
    })()
    monkeypatch.setattr(p60_reconcile, "ProtonCLIProvider", lambda *a, **k: fake)
    monkeypatch.setattr(p60_reconcile, "Store", lambda runtime, paths: FakeStore())
    monkeypatch.setattr(p60_reconcile.session, "writeback", lambda *a: False)
    result = p60_reconcile.run(ctx)
    assert result.outputs["dropped"] == 2 and result.outputs["strays_trashed"] == 1
    assert result.outputs["uid_refreshed"] == 1
    assert trashed == ["/my-files/Dropbox/Stray/x.bin"]
    left = {r["path_lower"]: r["proton_uid"] for r in ctx.state.connection.execute("SELECT * FROM mirror_objects")}
    assert left == {"/keep/ok.txt": "u-ok"}
    assert ctx.state.connection.execute("SELECT COUNT(*) FROM proton_snapshots").fetchone()[0] == 1


def test_reconcile_skips_when_not_scheduled(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context, reconcile=False)
    assert p60_reconcile.run(ctx).outputs == {"skipped": "not a reconcile run"}


def test_reconcile_skips_while_batches_remain(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context, remaining=1)
    assert p60_reconcile.run(ctx).outputs == {"skipped": "batches remain"}
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_reconcile.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write p60_reconcile.py**

```python
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
    proton = ProtonCLIProvider(ctx.cfg, ctx.state, ctx.logger,
                               after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store))
    proton.root_uid(PHASE)
    snapshot_id = proton.inventory(f"reconcile:{ctx.run_id}", PHASE, reuse_complete=True)
    connection = ctx.state.connection
    with connection:
        # One walk is enough evidence; the previous one is dead weight in every checkpoint.
        stale = [(int(r["id"]),) for r in connection.execute(
            "SELECT id FROM proton_snapshots WHERE id != ?", (snapshot_id,))]
        for table in ("proton_nodes", "proton_folders"):
            connection.executemany(f"DELETE FROM {table} WHERE snapshot_id=?", stale)
        connection.executemany("DELETE FROM proton_snapshots WHERE id=?", stale)
    # proton_nodes.relative_path has no leading slash; Dropbox display paths do.
    nodes = {
        str(row["comparison_key"]): row
        for row in connection.execute(
            "SELECT * FROM proton_nodes WHERE snapshot_id=? AND LOWER(node_type)='file'", (snapshot_id,))
    }
    dropped = refreshed = 0
    with connection:
        for row in connection.execute("SELECT * FROM mirror_objects").fetchall():
            node = nodes.get(comparison_key(str(row["path_display"]).lstrip("/")))
            if node is None or node["claimed_size"] is None or int(node["claimed_size"]) != int(row["size"]):
                connection.execute("DELETE FROM mirror_objects WHERE path_lower=?", (row["path_lower"],))
                dropped += 1
            elif row["proton_uid"] != node["uid"]:
                connection.execute("UPDATE mirror_objects SET proton_uid=? WHERE path_lower=?",
                                   (node["uid"], row["path_lower"]))
                refreshed += 1
    known = {
        comparison_key(str(row["path_display"]).lstrip("/"))
        for row in connection.execute("SELECT path_display FROM mirror_objects")
    }
    known |= {
        comparison_key(str(row["path_display"]).lstrip("/"))
        for row in connection.execute(
            "SELECT path_display FROM dropbox_objects WHERE inventory_id=? AND tag='file' AND is_downloadable=1",
            (run["inventory_id"],))
    }
    strays = sorted(str(node["cli_path"]) for key, node in nodes.items() if key not in known)
    if strays:
        proton.trash(strays, PHASE)
    statefile.push(ctx.state, ctx.runtime, ctx.paths, store, label=f"{history_label(ctx)}-reconcile")
    outputs = {"snapshot_id": snapshot_id, "proton_files": len(nodes), "dropped": dropped,
               "uid_refreshed": refreshed, "strays_trashed": len(strays)}
    ctx.logger.info(PHASE, "gate", "weekly reconcile complete", **outputs)
    return PhaseResult(outputs=outputs)
```

A dropped row re-uploads on the next run because the delta no longer sees it as mirrored. A stray is a Proton file under the mirror root that neither the current Dropbox listing nor the state knows.

- [ ] **Step 4: Register the phase**

Add `"reconcile": PhaseDefinition(60, "reconcile", "60_reconcile", p60_reconcile.run),` with the import.

- [ ] **Step 5: Run tests**

Run: `task test -- tests/test_reconcile.py`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/migrator/phases/p60_reconcile.py src/migrator/runner.py tests/test_reconcile.py
git commit -m "feat(reconcile): weekly Proton walk corrects state and trashes strays"
```

---

### Task 17: Report, empty-trash

**Files:**
- Create: `src/migrator/phases/p70_report.py`, `src/migrator/phases/p80_empty_trash.py`, `tests/test_report.py`
- Modify: `src/migrator/runner.py` (register `report`, `empty-trash`)

**Interfaces:**
- Consumes: `runs`, `batches`, `batch_items`, `deletions`, `events`, `commands`, `phase_runs`, `dropbox_objects`, `mirror_objects`, `WorkPaths.report`, `WorkPaths.chain`.
- Produces: `p70_report.run(ctx)` writing `paths.report` (Markdown), writing `paths.chain` when `runs.chain == 1`, finishing the run row (`SUCCESS` when every phase of this run passed, else `FAIL`), logging one event whose fields are the full figures dict, pushing the state under `--apply` with label `<epoch>-report`, and returning the run status as the phase status so a FAIL run stops the pipeline before the success ping; `figures(ctx) -> dict` is the pure aggregation. The workflow re-runs `report` only when `report.md` is absent, because `finish_run` leaves no RUNNING row for a second pass. `p80_empty_trash.run(ctx)` calls `ProtonCLIProvider.empty_trash` under `--apply`.

- [ ] **Step 1: Write the failing tests**

`tests/test_report.py`:

```python
from __future__ import annotations

import json

from migrator.phases import p70_report
from migrator.phases.base import PhaseContext
from conftest import FakeStore


def _ctx(state_context):
    cfg, paths, state, logger, runtime = state_context
    run_id = state.start_run(start_epoch=1000, hour_utc=2, weekday=0, budget_minutes=165, host="github:1", reconcile=False)
    phase_run_id = state.start_phase(70, "70_report", apply=True, inputs={})
    return PhaseContext(cfg, paths, state, logger, True, phase_run_id, run_id, runtime)


def _populate(ctx, chain=True):
    state = ctx.state
    with state.connection:
        cursor = state.connection.execute(
            """INSERT INTO dropbox_inventory_runs(started_at, completed_at, status, account_id, root_namespace_id, purpose)
               VALUES ('now','now','COMPLETE','dbid:test-account','ns','run:1')""")
        inventory_id = int(cursor.lastrowid)
        state.connection.executemany(
            """INSERT INTO dropbox_objects(inventory_id, object_key, tag, name, path_display, path_lower, comparison_key,
               size, content_hash, is_downloadable, raw_json, first_page, last_page)
               VALUES (?, ?, 'file', 'n', ?, ?, ?, ?, 'h', ?, '{}', 1, 1)""",
            [(inventory_id, "/taxes/a.pdf", "/Taxes/a.pdf", "/taxes/a.pdf", "/taxes/a.pdf", 100, 1),
             (inventory_id, "/b.txt", "/b.txt", "/b.txt", "/b.txt", 50, 1),
             (inventory_id, "/n.paper", "/n.paper", "/n.paper", "/n.paper", 0, 0)])
        state.connection.execute(
            """INSERT INTO mirror_objects(path_lower, path_display, size, content_hash, sha1, sha256, run_id, mirrored_at)
               VALUES ('/taxes/a.pdf', '/Taxes/a.pdf', 100, 'h', 's', 's', ?, 'now')""", (ctx.run_id,))
        state.connection.execute(
            """INSERT INTO batches(run_id, number, bytes, file_count, status, started_at, completed_at, details_json)
               VALUES (?, 1, 100, 1, 'CHECKPOINTED', 'now', 'now', ?)""",
            (ctx.run_id, json.dumps({"fetched": 1, "vanished": 0, "verified": 1, "bytes": 100, "uploaded_files": 1,
                                     "uploaded_bytes": 100, "confirmed": 1, "confirm_failed": 0, "roundtrip_ok": 1,
                                     "roundtrip_mismatch": 0, "roundtrip_bytes": 100, "checkpointed": 1, "failed": 0,
                                     "fetch_seconds": 10, "upload_seconds": 20, "roundtrip_seconds": 5, "seconds": 40})))
        state.connection.execute(
            """INSERT INTO batches(run_id, number, bytes, file_count, status) VALUES (?, 2, 50, 1, 'PLANNED')""",
            (ctx.run_id,))
    state.update_run(ctx.run_id, inventory_id=inventory_id, planned_batches=2, remaining_batches=1, chain=int(chain))
    ctx.logger.warning("10_inventory", "files/list_folder", "Dropbox request will be retried",
                       retry_count=1, provider_category="RATE_LIMIT", wait_seconds=15)
    cid = state.record_command_start("proton", "upload", ["proton-drive"], 1)
    state.record_command_end(cid, 1, "RATE_LIMIT")
    for number, name in ((10, "10_inventory"), (20, "20_delta"), (30, "30_plan"), (40, "40_batches")):
        pid = state.start_phase(number, name, apply=True, inputs={"run_id": ctx.run_id})
        state.complete_phase(pid, "PASS")


def test_figures_and_markdown_carry_counts_never_names(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context)
    _populate(ctx)
    store = FakeStore()
    monkeypatch.setattr(p70_report, "Store", lambda runtime, paths: store)
    figures = p70_report.figures(ctx)
    assert figures["mirror"] == {"inventory_files": 2, "inventory_bytes": 150, "mirrored_files": 1,
                                 "mirrored_bytes": 100, "percent_mirrored": 66.7, "non_downloadable": 1,
                                 "batches_remaining": 1, "bytes_remaining": 50, "projected_runs_remaining": 1,
                                 "chain": True}
    assert figures["run"]["batches_completed"] == 1 and figures["run"]["files_round_tripped"] == 1
    assert figures["throttling"]["dropbox"] == {"rate_limited": 1, "wait_seconds": 15.0, "longest_wait_seconds": 15.0}
    assert figures["throttling"]["proton"]["rate_limited"] == 1
    assert figures["verification"]["mismatches"] == 0
    result = p70_report.run(ctx)
    text = ctx.paths.report.read_text(encoding="utf-8")
    assert "Taxes" not in text and "a.pdf" not in text
    assert "percent mirrored" in text.lower() and "66.7" in text
    assert ctx.paths.chain.exists()
    assert result.outputs["status"] == "SUCCESS"
    row = ctx.state.connection.execute("SELECT status FROM runs WHERE id=?", (ctx.run_id,)).fetchone()
    assert row["status"] == "SUCCESS"
    assert sorted(store.objects) == [".state/history/1000-report.sqlite.xz.age", ".state/state.sqlite.xz.age"]
    event = ctx.state.connection.execute(
        "SELECT fields_json FROM events WHERE operation='figures' ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(event["fields_json"])["mirror"]["mirrored_files"] == 1


def test_report_marks_failed_run_and_writes_no_chain(state_context, monkeypatch, plain_crypt):
    ctx = _ctx(state_context)
    _populate(ctx, chain=False)
    monkeypatch.setattr(p70_report, "Store", lambda runtime, paths: FakeStore())
    pid = ctx.state.start_phase(40, "40_batches", apply=True, inputs={"run_id": ctx.run_id})
    ctx.state.complete_phase(pid, "FAIL", error_summary="batch 1 failed")
    result = p70_report.run(ctx)
    assert result.status == "FAIL" and result.outputs["status"] == "FAIL"
    assert not ctx.paths.chain.exists()
    assert "FAIL" in ctx.paths.report.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run to verify failure**

Run: `task test -- tests/test_report.py`
Expected: FAIL, module not found.

- [ ] **Step 3: Write p70_report.py**

```python
from __future__ import annotations

import json
import math
from typing import Any

from .. import statefile
from ..store import Store
from .base import PhaseContext, PhaseResult
from .batch import history_label

PHASE = "70_report"
ERROR_CLASSES = {
    "content-hash mismatch": "content hash mismatch",
    "upload failure": "upload failed",
    "round-trip mismatch": "ROUNDTRIP_MISMATCH",
    "confirm failure": "CONFIRM_FAILED",
    "listing refused": "floor",
    "session trouble": "login",
}


def _sum(rows: list[dict[str, Any]], key: str) -> int:
    return int(sum(int(row.get(key, 0) or 0) for row in rows))


def _batch_details(ctx: PhaseContext) -> list[dict[str, Any]]:
    return [
        json.loads(row["details_json"] or "{}")
        for row in ctx.state.connection.execute(
            "SELECT details_json FROM batches WHERE run_id=? AND status IN ('CHECKPOINTED', 'FAILED') ORDER BY number",
            (ctx.run_id,))
    ]


def _throttling(ctx: PhaseContext, provider_phase_like: str, command_provider: str,
                since: str) -> dict[str, float]:
    """This run only: `since` is runs.started_at, and the evidence tables carry no run id.
    (`commands.started_at` per the donor DDL, Task 4 step 5.)"""
    connection = ctx.state.connection
    waits = [
        float(json.loads(row["fields_json"] or "{}").get("wait_seconds") or 0)
        for row in connection.execute(
            "SELECT fields_json FROM events WHERE provider_category='RATE_LIMIT' AND operation LIKE ? "
            "AND timestamp >= ?",
            (provider_phase_like, since))
    ]
    commands = int(connection.execute(
        "SELECT COUNT(*) FROM commands WHERE provider=? AND response_category='RATE_LIMIT' AND started_at >= ?",
        (command_provider, since)).fetchone()[0])
    return {
        "rate_limited": len(waits) + commands,
        "wait_seconds": round(sum(waits), 1),
        "longest_wait_seconds": round(max(waits, default=0.0), 1),
    }


def figures(ctx: PhaseContext) -> dict[str, Any]:
    connection = ctx.state.connection
    run = ctx.state.current_run()
    since = str(run["started_at"])
    inventory = connection.execute(
        """SELECT SUM(CASE WHEN is_downloadable=1 THEN 1 ELSE 0 END) AS files,
                  COALESCE(SUM(CASE WHEN is_downloadable=1 THEN size ELSE 0 END), 0) AS bytes,
                  SUM(CASE WHEN is_downloadable=0 THEN 1 ELSE 0 END) AS non_downloadable
           FROM dropbox_objects WHERE inventory_id=? AND tag='file'""", (run["inventory_id"],)).fetchone()
    inventory_files = int(inventory["files"] or 0)
    inventory_bytes = int(inventory["bytes"] or 0)
    mirrored = connection.execute(
        """SELECT COUNT(*) AS files, COALESCE(SUM(m.size), 0) AS bytes FROM mirror_objects m
           JOIN dropbox_objects d ON d.inventory_id=? AND d.path_lower=m.path_lower
           WHERE d.tag='file' AND d.is_downloadable=1 AND d.size=m.size AND d.content_hash=m.content_hash""",
        (run["inventory_id"],)).fetchone()
    remaining = connection.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS bytes FROM batches WHERE run_id=? AND status='PLANNED'",
        (ctx.run_id,)).fetchone()
    details = _batch_details(ctx)
    moved = _sum(details, "checkpointed")
    moved_bytes = _sum(details, "uploaded_bytes")
    projected = math.ceil(int(remaining["bytes"]) / moved_bytes) if moved_bytes and remaining["bytes"] else (
        0 if not remaining["bytes"] else None)
    elapsed = sum(float(d.get("seconds", 0) or 0) for d in details)
    durations = [float(d.get("seconds", 0) or 0) for d in details]
    durations.sort()
    fetch_s = sum(float(d.get("fetch_seconds", 0) or 0) for d in details)
    upload_s = sum(float(d.get("upload_seconds", 0) or 0) for d in details)
    roundtrip_s = sum(float(d.get("roundtrip_seconds", 0) or 0) for d in details)
    gb = 1024**3

    def rate(nbytes: int, seconds: float) -> float:
        return round(nbytes / gb / (seconds / 3600), 2) if seconds else 0.0

    phases = connection.execute(
        """SELECT phase_name, status, error_summary FROM phase_runs
           WHERE json_extract(inputs_json, '$.run_id') = ?
             AND id IN (SELECT MAX(id) FROM phase_runs GROUP BY phase_number)
           ORDER BY phase_number""", (ctx.run_id,)).fetchall()
    failed_phases = [row["phase_name"] for row in phases if row["status"] == "FAIL"]
    errors = {
        label: int(connection.execute(
            "SELECT COUNT(*) FROM events WHERE level='ERROR' AND timestamp >= ? "
            "AND (message LIKE ? OR safe_raw_error LIKE ?)",
            (since, f"%{needle}%", f"%{needle}%")).fetchone()[0])
        for label, needle in ERROR_CLASSES.items()
    }
    errors["command non-zero exit"] = int(connection.execute(
        "SELECT COUNT(*) FROM commands WHERE exit_code IS NOT NULL AND exit_code != 0 AND started_at >= ?",
        (since,)).fetchone()[0])
    deletions = connection.execute(
        "SELECT status, COUNT(*) AS n FROM deletions WHERE run_id=? GROUP BY status", (ctx.run_id,)).fetchall()
    cumulative_verified = int(connection.execute("SELECT COUNT(*) FROM mirror_objects").fetchone()[0])
    return {
        "mirror": {
            "inventory_files": inventory_files,
            "inventory_bytes": inventory_bytes,
            "mirrored_files": int(mirrored["files"]),
            "mirrored_bytes": int(mirrored["bytes"]),
            "percent_mirrored": round(100 * int(mirrored["bytes"]) / inventory_bytes, 1) if inventory_bytes else 100.0,
            "non_downloadable": int(inventory["non_downloadable"] or 0),
            "batches_remaining": int(remaining["n"]),
            "bytes_remaining": int(remaining["bytes"]),
            "projected_runs_remaining": projected,
            "chain": bool(run["chain"]),
        },
        "run": {
            "host": run["host"],
            "reconcile": bool(run["reconcile"]),
            "budget_minutes": int(run["budget_minutes"]),
            "budget_used_minutes": round(elapsed / 60, 1),
            "batches_planned": int(run["planned_batches"] or 0),
            "batches_completed": len(details),
            "files_fetched": _sum(details, "fetched"),
            "files_vanished": _sum(details, "vanished"),
            "files_hash_mismatched": _sum(details, "hash_mismatch"),
            "files_uploaded": _sum(details, "uploaded_files"),
            "bytes_uploaded": moved_bytes,
            "files_confirmed": _sum(details, "confirmed"),
            "files_round_tripped": _sum(details, "roundtrip_ok") + _sum(details, "roundtrip_mismatch"),
            "files_checkpointed": moved,
            "files_trashed": next((int(r["n"]) for r in deletions if r["status"] == "TRASHED"), 0),
            "files_not_found_for_trash": next((int(r["n"]) for r in deletions if r["status"] == "NOT_FOUND"), 0),
        },
        "throughput": {
            "dropbox_down_gb_per_hour": rate(_sum(details, "bytes"), fetch_s),
            "proton_up_gb_per_hour": rate(moved_bytes, upload_s),
            "proton_down_gb_per_hour": rate(_sum(details, "roundtrip_bytes"), roundtrip_s),
            "batch_seconds_min": durations[0] if durations else 0,
            "batch_seconds_median": durations[len(durations) // 2] if durations else 0,
            "batch_seconds_max": durations[-1] if durations else 0,
        },
        "throttling": {
            "dropbox": _throttling(ctx, "files/%", "rclone", since),
            "proton": _throttling(ctx, "proton%", "proton", since),
        },
        "errors": errors,
        "verification": {
            "round_trip_ok_this_run": _sum(details, "roundtrip_ok"),
            "confirm_failed": _sum(details, "confirm_failed"),
            "mismatches": _sum(details, "roundtrip_mismatch"),
            "files_proven_cumulative": cumulative_verified,
        },
        "phases": {row["phase_name"]: row["status"] for row in phases},
        "failed_phases": failed_phases,
    }


def _table(title: str, rows: dict[str, Any]) -> str:
    lines = [f"### {title}", "", "| Figure | Value |", "|---|---|"]
    for key, value in rows.items():
        lines.append(f"| {key.replace('_', ' ')} | {value if value is not None else 'n/a'} |")
    return "\n".join(lines) + "\n\n"


def render(fig: dict[str, Any], status: str) -> str:
    out = f"## dropbox-mirror run: {status}\n\n"
    out += _table("Mirror status", fig["mirror"])
    out += _table("This run", fig["run"])
    out += _table("Throughput", fig["throughput"])
    throttling = {f"dropbox {k}": v for k, v in fig["throttling"]["dropbox"].items()}
    throttling.update({f"proton {k}": v for k, v in fig["throttling"]["proton"].items()})
    out += _table("Throttling", throttling)
    out += _table("Errors and issues", fig["errors"])
    out += _table("Verification", fig["verification"])
    out += _table("Phases", fig["phases"])
    if fig["failed_phases"]:
        out += f"Failed phases: {', '.join(fig['failed_phases'])}\n"
    return out


def run(ctx: PhaseContext) -> PhaseResult:
    label = f"{history_label(ctx)}-report"  # before finish_run: current_run() needs the RUNNING row
    fig = figures(ctx)
    status = "FAIL" if fig["failed_phases"] else "SUCCESS"
    ctx.paths.report.write_text(render(fig, status), encoding="utf-8")
    ctx.paths.chain.unlink(missing_ok=True)
    if fig["mirror"]["chain"] and status == "SUCCESS":
        ctx.paths.chain.write_text("chain\n", encoding="utf-8")
    ctx.state.finish_run(ctx.run_id, status)
    ctx.logger.info(PHASE, "figures", "run figures", **fig)
    if ctx.apply:
        # The run row, its figures event and the final status exist only here until pushed.
        statefile.push(ctx.state, ctx.runtime, ctx.paths, Store(ctx.runtime, ctx.paths), label=label)
    # The phase status is the run status: a FAIL run stops `task pipeline` before `ping`.
    return PhaseResult(status=status,
                       outputs={"status": status, "chain": fig["mirror"]["chain"], "report": str(ctx.paths.report)})
```

Proton's CLI backs off internally, so `throttling.proton.rate_limited` counts only commands whose stderr mentioned a rate limit; the sustained upload rate beside it is the second gauge (spec section 5.5).

- [ ] **Step 4: Write p80_empty_trash.py**

```python
from __future__ import annotations

from .. import session
from ..providers.proton_cli import ProtonCLIProvider
from ..store import Store
from .base import PhaseContext, PhaseResult

PHASE = "80_empty_trash"


def run(ctx: PhaseContext) -> PhaseResult:
    if not ctx.apply:
        return PhaseResult(status="PLANNED", outputs={"planned": "empty Proton trash"})
    store = Store(ctx.runtime, ctx.paths)
    proton = ProtonCLIProvider(ctx.cfg, ctx.state, ctx.logger,
                               after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store))
    proton.root_uid(PHASE)
    proton.empty_trash(PHASE)
    ctx.logger.info(PHASE, "gate", "Proton trash emptied by operator request")
    return PhaseResult(outputs={"emptied": True})
```

- [ ] **Step 5: Register both phases**

Add `"report": PhaseDefinition(70, "report", "70_report", p70_report.run),` and `"empty-trash": PhaseDefinition(80, "empty-trash", "80_empty_trash", p80_empty_trash.run),` with imports.

- [ ] **Step 6: Run tests**

Run: `task test`
Expected: PASS across the suite.

- [ ] **Step 7: Commit**

```bash
git add src/migrator/phases/p70_report.py src/migrator/phases/p80_empty_trash.py src/migrator/runner.py tests/test_report.py
git commit -m "feat(report): counts-only step summary, chain marker, empty-trash phase"
```

---

### Task 18: Taskfile pipeline, banner, op.env, and the render check

**Files:**
- Modify: `Taskfile.yml` (add global `env`, pipeline tasks, read/write groups, banner lines), `op.env`
- Create: `tests/test_taskfile.py`

**Interfaces:**
- Consumes: every `python -m migrator` command from Tasks 9 through 17.
- Produces: `task pipeline` (inside the toolbox), `task sync` (host wrapper with secrets), `task plan`, `task status`, `task render`, `task empty-trash`, `task state-rollback -- <key>`, `task session-seal -- <dir>`, and the internal step tasks `clock session state inventory delta plan-phase batches trash reconcile report-phase ping ping-fail empty-trash-pipeline`. The banner lists every operator-facing task.

- [ ] **Step 1: Write op.env**

```
# op:// references only; `op run --env-file=op.env` resolves them at run time.
# Vault is addressed by UUID so a rename cannot break it. REPLACE the UUID at bootstrap.
MIRROR_DROPBOX_APP_KEY=op://VAULT-UUID/dropbox/app_key
MIRROR_DROPBOX_APP_SECRET=op://VAULT-UUID/dropbox/app_secret
MIRROR_DROPBOX_REFRESH_TOKEN=op://VAULT-UUID/dropbox/refresh_token
RCLONE_CONFIG_DROPBOX_CLIENT_ID=op://VAULT-UUID/dropbox/app_key
RCLONE_CONFIG_DROPBOX_CLIENT_SECRET=op://VAULT-UUID/dropbox/app_secret
RCLONE_CONFIG_DROPBOX_TOKEN=op://VAULT-UUID/dropbox/rclone_token
RCLONE_CONFIG_R2_ACCESS_KEY_ID=op://VAULT-UUID/r2/access_key_id
RCLONE_CONFIG_R2_SECRET_ACCESS_KEY=op://VAULT-UUID/r2/secret_access_key
RCLONE_CONFIG_R2_ENDPOINT=op://VAULT-UUID/r2/endpoint
MIRROR_R2_BUCKET=op://VAULT-UUID/r2/bucket
MIRROR_AGE_IDENTITY=op://VAULT-UUID/age/identity
MIRROR_HEALTHCHECK_URL=op://VAULT-UUID/healthcheck/url
```

- [ ] **Step 2: Add the global env and the pipeline tasks to Taskfile.yml**

Insert after `vars:` (before `tasks:`):

```yaml
  RUN_EPOCH:
    sh: date +%s

# Run-scoped environment for the step tasks below. The rclone remote literals are ENV
# lines in the Dockerfile so that every in-toolbox command sees them, wrapped by an
# inner `task` or not.
env:
  MIRROR_RUN_EPOCH: '{{.RUN_EPOCH}}'
  MIRROR_WORK_DIR: .run
  MIRROR_CONFIG: config/mirror.toml
```

Replace the `default` banner body with:

```yaml
        printf '%s\n' \
          "" \
          "${b}dropbox-mirror${r}   ${d}nightly Dropbox -> Proton Drive mirror; R2 holds the state${r}" \
          "" \
          "${c}read -- looks at Dropbox, Proton and R2, changes nothing${r}" \
          "  task plan                 Fetch state, list Dropbox, print what a sync would move" \
          "                            ${d}needs secrets; starts a run row but never mutates Proton${r}" \
          "  task status               Counts and the last run's figures from the state in R2" \
          "" \
          "${c}checks -- offline, no credentials${r}" \
          "  task test                 pytest inside the toolbox" \
          "  task lint                 ruff check + format check" \
          "  task fmt                  ruff format" \
          "  task render               Dry-run the whole pipeline: every command, no network" \
          "" \
          "${c}write -- changes Proton Drive and the state in R2${r}" \
          "  task sync                 One budgeted run: batches, trash, reconcile, report, ping" \
          "                            ${d}RUN_BUDGET_MIN=n overrides the budget; RECONCILE=true forces a Proton walk${r}" \
          "  task empty-trash          Permanently delete Proton trash (never scheduled)" \
          "  task state-rollback -- k  Copy a history object over the canonical state" \
          "                            ${d}with no key it lists the history${r}" \
          "  task session-seal -- dir  Encrypt a laptop Proton CLI session into R2" \
          "" \
          "${c}toolbox${r}" \
          "  task image                Build the toolbox image (no-op while it exists)" \
          "  task image-clean          Remove it so the next task image rebuilds" \
          "  task run -- <cmd>         Run any command in the toolbox with the repo at /work" \
          "  task op -- <cmd>          Same, with secrets from 1Password via op.env" \
          ""
```

Append these tasks:

```yaml
  # ---- pipeline (runs inside the toolbox; each step is one migrator command) ----
  pipeline:
    desc: The full budgeted run, inside the toolbox
    cmds:
      - task: clock
      - task: session
      - task: state
      - task: inventory
      - task: delta
      - task: plan-phase
      - task: batches
      - task: trash
      - task: reconcile
      - task: report-phase
      - task: ping

  plan-pipeline:
    desc: Read-only half of the pipeline, inside the toolbox
    cmds:
      - task: clock
      - task: session
      - task: state
      - task: inventory
      - task: delta
      - task: plan-phase
      - python -m migrator report   # no --apply: a read-only plan pushes nothing to R2

  clock:        { internal: true, cmds: ['python -m migrator clock'] }
  session:      { internal: true, cmds: ['python -m migrator session'] }
  state:        { internal: true, cmds: ['python -m migrator state'] }
  inventory:    { internal: true, cmds: ['python -m migrator inventory'] }
  delta:        { internal: true, cmds: ['python -m migrator delta'] }
  plan-phase:   { internal: true, cmds: ['python -m migrator plan'] }
  batches:      { internal: true, cmds: ['python -m migrator --apply batches'] }
  trash:        { internal: true, cmds: ['python -m migrator --apply trash'] }
  reconcile:    { internal: true, cmds: ['python -m migrator --apply reconcile'] }
  report-phase: { internal: true, cmds: ['python -m migrator --apply report'] }
  ping:         { internal: true, cmds: ['python -m migrator ping'] }
  ping-fail:    { internal: true, cmds: ['python -m migrator ping fail'] }
  empty-trash-pipeline:
    internal: true
    cmds:
      - task: clock
      - task: session
      - task: state
      - python -m migrator --apply empty-trash

  # ---- read ----
  plan:
    desc: Fetch state, list Dropbox, print what a sync would move
    cmds:
      - task: op
        vars: { CLI_ARGS: task plan-pipeline }
      - cat .run/report.md

  status:
    desc: Counts from the state in R2
    cmds:
      - task: op
        vars: { CLI_ARGS: python -m migrator status }

  render:
    desc: Dry-run the whole pipeline inside the toolbox with no network
    cmds:
      - task: run
        vars: { CLI_ARGS: 'task --dry --force pipeline' }

  # ---- write ----
  sync:
    desc: One budgeted run with secrets from 1Password
    cmds:
      - task: op
        vars: { CLI_ARGS: task pipeline }

  empty-trash:
    desc: Permanently delete Proton trash
    prompt: This permanently deletes everything in Proton Drive trash. Continue?
    cmds:
      - task: op
        vars: { CLI_ARGS: task empty-trash-pipeline }

  state-rollback:
    desc: 'Copy a history object over the canonical state: task state-rollback -- <key>'
    cmds:
      - task: op
        vars: { CLI_ARGS: 'python -m migrator state-rollback {{.CLI_ARGS}}' }

  session-seal:
    desc: 'Encrypt a laptop Proton CLI session into R2: task session-seal -- <cache dir>'
    cmds:
      - task: op
        vars: { CLI_ARGS: 'python -m migrator session-seal {{.CLI_ARGS}}' }
```

`task status` fetches the state itself and starts no run row, so it leaves nothing behind in R2 and cannot mask the reconcile day. Every operator task that needs the run-scoped environment (`MIRROR_RUN_EPOCH`) goes through an inner task; `state-rollback` and `session-seal` need none.

- [ ] **Step 3: Write the banner drift check**

`tests/test_taskfile.py` (a regex walk; no YAML dependency):

```python
from __future__ import annotations

import re
from pathlib import Path

TASKFILE = Path(__file__).resolve().parents[1] / "Taskfile.yml"


def _tasks_with_desc(text: str) -> set[str]:
    names = set()
    for match in re.finditer(r"^  ([a-z][a-z0-9-]*):\n((?:    .*\n)+)", text, re.MULTILINE):
        block = match.group(2)
        if "desc:" in block and "internal: true" not in block:
            names.add(match.group(1))
    return names


def test_every_operator_task_is_in_the_banner():
    text = TASKFILE.read_text(encoding="utf-8")
    banner = text[text.index("default:"):text.index("\n  image:")]
    listed = set(re.findall(r"task ([a-z][a-z0-9-]*)", banner))
    expected = _tasks_with_desc(text) - {"default", "pipeline", "plan-pipeline"}
    assert expected <= listed, sorted(expected - listed)
```

- [ ] **Step 4: Render and test**

Run: `task render`
Expected: every pipeline command prints, in order, with no network access and no secrets in the environment.

Run: `task test -- tests/test_taskfile.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Taskfile.yml op.env tests/test_taskfile.py
git commit -m "feat(taskfile): pipeline steps, operator menu, op.env references"
```

---

### Task 19: GitHub workflows

**Files:**
- Create: `.github/workflows/sync.yml`, `.github/workflows/check.yml`

**Interfaces:**
- Consumes: `task run -- task pipeline`, `.run/report.md`, `.run/chain`, `task run -- task report-phase`, `task run -- task ping-fail`, `config/toolchain.lock.toml` (go-task version and checksum for the runner).
- Produces: a dispatch-only sync workflow that chains itself, and a pull-request check workflow.

- [ ] **Step 1: Write sync.yml**

```yaml
name: sync

on:
  workflow_dispatch:
    inputs:
      reconcile:
        description: Force a full Proton walk this run
        type: boolean
        default: false
      budget_minutes:
        description: Wall-clock budget for batches (default from config/mirror.toml)
        type: string
        default: ""

# Overlap kills the Proton session; the group also queues a chained or scheduled
# run behind the one in progress.
concurrency:
  group: sync
  cancel-in-progress: false

permissions:
  contents: read
  actions: write   # chain: gh workflow run

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 180
    env:
      RECONCILE: ${{ inputs.reconcile }}
      RUN_BUDGET_MIN: ${{ inputs.budget_minutes }}
    steps:
      - uses: actions/checkout@REPLACE_WITH_SHA   # v4.x; pin the full commit SHA

      - name: Install go-task at the locked version
        run: |
          set -eu
          version="$(python3 -c 'import tomllib; print(tomllib.load(open("config/toolchain.lock.toml","rb"))["task"]["version"])')"
          sha="$(python3 -c 'import tomllib; print(tomllib.load(open("config/toolchain.lock.toml","rb"))["task"]["linux_amd64"]["sha256"])')"
          curl -fsSL "https://github.com/go-task/task/releases/download/v${version}/task_linux_amd64.tar.gz" -o /tmp/task.tgz
          echo "${sha}  /tmp/task.tgz" | sha256sum -c -
          sudo tar -xzf /tmp/task.tgz -C /usr/local/bin task

      - name: Load secrets from 1Password
        uses: 1password/load-secrets-action@REPLACE_WITH_SHA   # v2.x; pin the full commit SHA
        with:
          export-env: true
        env:
          OP_SERVICE_ACCOUNT_TOKEN: ${{ secrets.OP_SERVICE_ACCOUNT_TOKEN }}
          MIRROR_DROPBOX_APP_KEY: op://VAULT-UUID/dropbox/app_key
          MIRROR_DROPBOX_APP_SECRET: op://VAULT-UUID/dropbox/app_secret
          MIRROR_DROPBOX_REFRESH_TOKEN: op://VAULT-UUID/dropbox/refresh_token
          RCLONE_CONFIG_DROPBOX_CLIENT_ID: op://VAULT-UUID/dropbox/app_key
          RCLONE_CONFIG_DROPBOX_CLIENT_SECRET: op://VAULT-UUID/dropbox/app_secret
          RCLONE_CONFIG_DROPBOX_TOKEN: op://VAULT-UUID/dropbox/rclone_token
          RCLONE_CONFIG_R2_ACCESS_KEY_ID: op://VAULT-UUID/r2/access_key_id
          RCLONE_CONFIG_R2_SECRET_ACCESS_KEY: op://VAULT-UUID/r2/secret_access_key
          RCLONE_CONFIG_R2_ENDPOINT: op://VAULT-UUID/r2/endpoint
          MIRROR_R2_BUCKET: op://VAULT-UUID/r2/bucket
          MIRROR_AGE_IDENTITY: op://VAULT-UUID/age/identity
          MIRROR_HEALTHCHECK_URL: op://VAULT-UUID/healthcheck/url

      - name: Run the pipeline in the toolbox
        run: task run -- task pipeline

      - name: Publish the report; ping /fail unless the job succeeded
        if: always()
        run: |
          # report.md exists whenever the report phase ran (it also finished the run row, so
          # a second pass has no RUNNING run). A run that died earlier gets its report now.
          if [ ! -f .run/report.md ]; then task run -- task report-phase || true; fi
          if [ -f .run/report.md ]; then cat .run/report.md >> "$GITHUB_STEP_SUMMARY"; fi
          if [ "${{ job.status }}" != "success" ]; then task run -- task ping-fail; fi

      - name: Chain the next run
        if: success()
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          if [ -f .run/chain ]; then
            gh workflow run sync.yml --ref "$GITHUB_REF_NAME"
            echo "chained: next run queued" >> "$GITHUB_STEP_SUMMARY"
          fi
```

The same `op://` references appear in `op.env`; that duplication is the spec's "named in exactly two places". Resolve both `REPLACE_WITH_SHA` values from the actions' release tags before committing. `actions/checkout` is GitHub's own; the 1Password loader is the one third-party action (spec section 7).

- [ ] **Step 2: Write check.yml**

```yaml
name: check

on:
  pull_request:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@REPLACE_WITH_SHA
      - name: Install go-task at the locked version
        run: |
          set -eu
          version="$(python3 -c 'import tomllib; print(tomllib.load(open("config/toolchain.lock.toml","rb"))["task"]["version"])')"
          sha="$(python3 -c 'import tomllib; print(tomllib.load(open("config/toolchain.lock.toml","rb"))["task"]["linux_amd64"]["sha256"])')"
          curl -fsSL "https://github.com/go-task/task/releases/download/v${version}/task_linux_amd64.tar.gz" -o /tmp/task.tgz
          echo "${sha}  /tmp/task.tgz" | sha256sum -c -
          sudo tar -xzf /tmp/task.tgz -C /usr/local/bin task
      - run: task test
      - run: task lint
      - run: task render
```

- [ ] **Step 3: Verify locally**

Run: `grep -c 'op://' .github/workflows/sync.yml; grep -c '^[A-Z].*=op://' op.env`
Expected: both print `12`, so the workflow and `op.env` name the same secrets.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/sync.yml .github/workflows/check.yml
git commit -m "ci(workflows): dispatch-only sync with self-chaining, PR checks"
```

---

### Task 20: README runbook

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: every task and command above.
- Produces: the bootstrap and recovery runbook the spec's sections 10, 11, and 12 refer to.

- [ ] **Step 1: Write README.md**

```markdown
# dropbox-mirror

Nightly mirror of a Dropbox account into Proton Drive under `/my-files/Dropbox`, run from
GitHub Actions inside one pinned toolbox image. Every uploaded file is downloaded back and
byte-compared before the state records it. R2 holds the only record of progress. Design:
`docs/superpowers/specs/2026-09-03-dropbox-mirror-design.md`.

`task` prints the menu.

## Bootstrap

1. **1Password.** Create a vault for this repo and a service account scoped to it. Put the
   service-account token in your personal vault and as the repo secret
   `OP_SERVICE_ACCOUNT_TOKEN`. Replace `VAULT-UUID` in `op.env` and
   `.github/workflows/sync.yml` with the vault's UUID (`op vault get <name> --format json`).
2. **Dropbox.** Create a private "Full Dropbox" app with `files.metadata.read` and
   `files.content.read`. Run the OAuth code flow with `token_access_type=offline` on the
   laptop and store `app_key`, `app_secret`, `refresh_token` in the vault item `dropbox`.
   Run `rclone authorize dropbox --client-id <key> --client-secret <secret>` and store the
   printed token JSON as field `rclone_token`. Put your account id in
   `config/mirror.toml` (`dbid:...`, from `users/get_current_account`).
3. **Proton.** Turn telemetry off in account settings. On the laptop:
   `PROTON_DRIVE_CACHE_DIR=./pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file proton-drive auth login`,
   sign in in the browser, create `/my-files/Dropbox`, then
   `proton-drive filesystem list -j /my-files` and copy the folder's `uid` into
   `config/mirror.toml`. Check `proton-drive filesystem upload --help` and confirm the flags
   in `src/migrator/providers/proton_cli.py` `upload_tree` match; record any change here.
4. **age.** `age-keygen` once; store the `AGE-SECRET-KEY-...` line as vault item `age`, field
   `identity`.
5. **R2.** Create a bucket, an API token scoped to it, and a lifecycle rule that expires
   `.state/history/` after 30 days. Store `access_key_id`, `secret_access_key`, `endpoint`
   (`https://<account>.r2.cloudflarestorage.com`), and `bucket` in vault item `r2`.
6. **healthchecks.io.** Create a check on the nightly cron with a grace period long enough
   for a queued run plus a full one; store the ping URL as vault item `healthcheck`, field `url`.
7. `task session-seal -- ./pd` uploads the encrypted session. `task plan` proves the whole
   read path: it lists Dropbox, finds an empty state, and prints what the seed would move.
8. **Seed.** Dispatch `sync` once in GitHub Actions. The first run finds no state and no
   history, treats the whole tree as the delta, and chains itself every budget until the
   tree is mirrored. Each run's summary shows percent mirrored and projected runs remaining.
9. **Schedule.** Add `schedules/dropbox-mirror.ts` to jshvn/dispatch with `10 2 * * *`.
   A scheduled run queues behind a chained one. After three green nights, edit and delete
   one file in Dropbox and confirm both appear in Proton the next morning.

## Runbook

- **A run fails with `PhaseError` and `login first` in the state events.** The Proton
  session is gone. Repeat bootstrap step 3's login and `task session-seal -- ./pd`.
- **The state looks wrong after a run.** `task state-rollback` lists
  `.state/history/<epoch>-<batch>` objects; `task state-rollback -- <key>` copies one over the
  canonical state. The next run repeats from there; re-uploads skip SHA-1-identical files.
- **`state object is missing but history exists`.** Roll back as above. Never delete
  history to make a run start fresh; that would treat a lost state as an empty mirror.
- **Reading error messages.** The summary and CI logs carry counts only. `task status`
  prints the last run's figures; error text lives in the `events` table of the state:
  `task status` leaves `.run/state.sqlite` behind, and
  `task run -- python -c "import sqlite3; c=sqlite3.connect('.run/state.sqlite'); print(*c.execute(\"SELECT timestamp, message, safe_raw_error FROM events WHERE level='ERROR' ORDER BY id DESC LIMIT 20\"), sep='\\n')"`
  lists the latest errors.
- **Throttling.** The summary's throttling table is the gauge. If Proton 429s appear or the
  upload rate collapses, lower `batch_gb` in `config/mirror.toml`; Dropbox stays at 10 rps.
- **Empty the trash.** `task empty-trash` asks for confirmation and is never scheduled.
- **Never run `task sync` or `task empty-trash` from the laptop while a CI run may be in
  progress.** Two processes holding the Proton session race the refresh token; the loser
  wipes its copy and the next run needs a login. `task plan` and `task status` make no
  Proton call and are safe at any time.
- **Move the runner.** The same image and Taskfile run on atium under cron with the same
  `op.env`: `task sync` there is the whole job, and the next cron tick is the chain.
```

- [ ] **Step 2: Final full check**

Run: `task test && task lint && task render`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs(readme): bootstrap and recovery runbook"
```

---

## Live verification after the plan (not automatable here)

The first dispatched run answers the spec's open unknowns; record each answer in the README's bootstrap step 3 or in `config/mirror.toml`:

1. Whether `proton-drive filesystem upload` accepts several local sources in one call (else the `ponytail` in `upload_tree`).
2. Whether Proton challenges the refresh call from Azure egress (spec risk 4).
3. The real per-batch duration, which sets whether `batch_gb=4` and `run_budget_minutes=165` stay.
4. That `.run/chain` triggers the next run and the concurrency group queues it.
5. Seconds per file in `roundtrip_seconds` against the batch's file count: the CLI is spawned once per round-tripped file. `batch_files` in `config/mirror.toml` is 5,000, which needs under about 1.8 s per file to finish inside `RUN_BUDGET_MIN`; if the first batch is killed by the job timeout before it checkpoints, lower the cap before the next dispatch (the run would otherwise repeat that batch every night), and if the cost is seconds per file build the recursive folder download from the review notes.
