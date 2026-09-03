# dropbox-mirror: a nightly Dropbox mirror into Proton Drive from GitHub Actions

Status: approved design, 2026-09-03. Research behind every platform claim is in
`2026-09-03-dropbox-mirror-research.md`. The local versioned copy of the same data is a
separate pipeline, specified in jgrid.net's atium spec; neither depends on the other.

## 1. Shape of the whole system

Dropbox stays authoritative. Every night, Proton Drive comes to hold what Dropbox held at
listing time, under `/my-files/Dropbox/`, with files that left Dropbox moved to Proton's
trash. The pipeline runs on GitHub-hosted runners inside a pinned container image, moves batches
of `BATCH_GB` until a wall-clock budget of about three hours is spent, checkpoints a SQLite
state database to R2 after every batch so a killed run repeats at most one batch,
re-dispatches itself while work remains, and pings healthchecks.io. Silence is the only
alert.

There is no seed mode. The first run finds an empty state, treats the whole tree as the
delta, and chains itself run after run until the tree is mirrored; a nightly run is the
same run with a small delta that fits in one budget. R2 holds the only record of how far
the mirror has got, and no run is ever told where to start.

```
Dropbox (primary)
  -> nightly GitHub Actions run (this repo)
       fetch batch -> verify hashes -> upload via proton-drive CLI -> round-trip -> checkpoint
  -> Proton Drive /my-files/Dropbox  (encrypted cloud mirror, Proton's version history)
```

## 2. Provenance

The core implementation is imported from
[donphi/dropbox_proton](https://github.com/donphi/dropbox_proton) (MIT; its copyright
notice is retained in `LICENSE`). Imported nearly verbatim, with their tests:

- `state.py` — the SQLite checkpoint and evidence schema (`phase_runs`, `artifacts`,
  `commands`, `events`, `upload_attempts`, resumable queues)
- `providers/dropbox_api.py`, `providers/dropbox_rclone.py`, `providers/proton_cli.py`
- `hashing.py` — one streaming pass computing SHA-256, SHA-1, and the Dropbox content hash
- `atomic.py`, `config.py`, `logging.py` (redaction), `normalization.py` (comparison keys),
  `paths.py`, `runner.py`, the phase framework, `toolchain.lock.toml` and the Dockerfile
  approach, the pytest suite and fixtures

Deliberately not imported, because this is a recurring mirror and not a one-shot
migration: the durable local canonical archive and the four phases that exist only for it
(recover-missing, restore-metadata, conflict-normalization, archive-rescan), the
drift-fails-permanently gate, the empty-destination requirement, and the
keyring/pass/dbus/GPG credential stack. Two mutations their safety model forbids are added
in well-marked places: upload with `create-new-revision` (a mirror must revise changed
files) and a Proton `trash` provider method (a mirror must retire deleted files). Their
remaining safety posture is kept: plan-by-default with `--apply`, strict config validation
that rejects unknown keys, secret redaction, atomic artifact writes, and "a command exit
status is never evidence" — every mutation is confirmed by independent observation.

## 3. Goal and non-goals

Goal: every morning, `/my-files/Dropbox/` matches the previous night's Dropbox listing;
changed files are new Proton revisions; removed files are in Proton's trash; the state
database records what landed with the hashes that prove it, and no file is recorded until
its bytes have been downloaded back from Proton and compared.

Non-goals:

- Two-way sync. Nothing written in Proton Drive flows back.
- Anything outside `/my-files/Dropbox/` in Proton Drive.
- Non-downloadable entries (Dropbox Paper, cloud-created docs, `.web` shortcuts): skipped
  and counted in the report. Nothing is exported.
- Real-time. Nightly is the cadence; Proton's fair-use rules are the reason.
- Permanent deletion. The nightly run trashes; only a manually invoked task empties trash.

## 4. What the research settled

- **Dropbox is easy to read.** One recursive `files/list_folder` walk returns every file
  with `content_hash` (reproducible locally: SHA-256 over 4 MiB blocks), `size`,
  `path_lower`, `path_display`. Auth is a non-rotating refresh token: one browser login,
  one secret, never rewritten. Limits are per user, about 12 calls a second, always with
  `Retry-After`.
- **Only one Proton write path survives CI.** rclone's Proton backend fails from hosted
  runners four independent ways (CAPTCHA on fresh datacenter logins, corrupt uploads on
  the current stable, a "temporary exemption" from Proton, no successful datacenter
  report). The official `proton-drive` CLI is the one Proton-endorsed unattended client:
  its browser-seeded session persists as a file (`unsafe_file` mode) and can be carried
  between runs.
- **The CLI session is a file with a rotating refresh token.** The pipeline restores it
  before the first CLI call and writes it back after every CLI call; runs must never
  overlap; a 4xx on refresh deletes the session and costs a manual re-login.
- **The CLI can do a mirror's three verbs.** `upload -f create-new-revision -d merge`
  skips SHA-1-identical files and revisions changed ones; `trash` accepts many UID paths
  in one call; `list --json` returns `uid`, `claimedSize`, `claimedDigests.sha1` per
  child, one folder per call.
- **Proton names are case-sensitive, unique per folder, not normalized; folders should
  stay under ~30,000 entries.** Dropbox is case-insensitive: the mirror keys on
  `path_lower` and writes `path_display` normalized to NFC.
- **GitHub-hosted runners:** 6 h per job, ~14 GB free disk, single-use single-tenant VMs.

## 5. Design

### 5.1 Taskfile pipeline, Python phases

`Taskfile.yml` is the operational surface, in the shape of jshvn/ctan: bare `task` prints
a menu grouped by effect, `task sync` is the pipeline, every step is a task, every run
happens inside the toolbox image, and offline checks run canned fixtures with no network.
Each step's body is one line: `python -m migrator <phase>`. Task owns sequencing,
environment, dry-run rendering and the menu; the imported Python owns all logic.

```
task sync:
  clock -> session -> state -> inventory -> delta -> plan -> batches -> trash -> reconcile? -> report -> ping -> chain?
                                     per batch: fetch -> verify -> upload -> confirm -> roundtrip -> checkpoint
```

| Step | What it does |
|---|---|
| `clock` | Stamp the run start (epoch + UTC hour); clear per-run outputs. The hour keys `reconcile`, read here because a long run crosses hours; the epoch anchors `RUN_BUDGET_MIN`. |
| `session` | Fetch `.state/session.tar.age` from R2, decrypt with the age identity, unpack to `PROTON_DRIVE_CACHE_DIR`. One cheap `filesystem list` forces any pending token rotation now; write-back (re-encrypt, upload) runs immediately, and again after every later CLI call. |
| `state` | Fetch `.state/state.sqlite.xz.age`, decrypt, decompress. A missing object is accepted as an empty state only when `.state/history/` is empty too, which is the first run ever; a missing object beside existing history is a failure, because a lost state must never be mistaken for an empty mirror. `RECONCILE=true` rebuilds from a Proton walk instead. |
| `inventory` | Recursive `files/list_folder` walk via the imported `dropbox_api` provider, each page committed to SQLite with its cursor (interrupt-resumable). Refuse a listing under a sanity floor so a truncated listing never becomes a trash list. Non-downloadable entries are recorded and excluded from the delta. On reconcile runs the rclone observer also lists, and the imported reconciliation gate must pass before anything moves. |
| `delta` | Compare inventory against `mirror_objects` on `(path_lower, size, content_hash)`: changed rows (upstream has, state lacks) and deleted rows (state has, upstream lacks). |
| `plan` | Refuse a tree over `CEILING_GB` or a file the runner's disk cannot hold twice (staging plus its round-trip copy) with 1 GiB headroom. Split changed rows into batches of at most `BATCH_GB` from listing sizes alone; an oversized file is a batch by itself. |
| `batches` | Run planned batches in order. Before each one: stop if elapsed time plus the longest batch so far would pass `RUN_BUDGET_MIN`. Stopping with batches left is a success that marks the run for `chain`. |
| `fetch` | Empty staging; rclone `copy --files-from --ignore-existing` of the batch. A path that vanished since listing is skipped and never enters the state. |
| `verify` | The one-pass hasher recomputes each staged file's Dropbox content hash against the listing and records SHA-1/SHA-256. A mismatch is a file edited between listing and fetch: it is removed from staging, counted, never recorded, and the next listing catches it. A batch where every file mismatches fails, since that is corruption rather than editing. |
| `upload` | One `proton-drive filesystem upload STAGING/ /my-files/Dropbox -f create-new-revision -d merge -t --json --skip-thumbnails`. Session write-back follows. |
| `confirm` | Parse the JSON summary: uploaded + skipped must equal the batch's row count, failures zero — guards the known zero-exit-with-missing-files case. |
| `roundtrip` | Download every just-uploaded file back by UID path into isolated staging and byte-compare (resumable queue in SQLite). Every file, every run, no sampling: a row enters `mirror_objects` only after its bytes have come back from Proton identical. Every byte crosses Proton twice; the run budget and chaining absorb that. |
| `checkpoint` | Merge the batch into `mirror_objects` with its hashes and UIDs; xz, age-encrypt, PutObject to `.state/history/<epoch>-<batch>.sqlite.xz.age` and then to `.state/state.sqlite.xz.age`. The last step of a batch, always: a run that dies repeats at most one batch, and a run stopped on budget with batches left is a success. |
| `trash` | Only in a run where every planned batch landed: group deleted rows by parent folder, one `list --json` per folder to map names to UIDs, one `filesystem trash` call per folder with every UID, all recorded. Session write-back follows. |
| `reconcile` | `auto` = the run whose start hour is the reconcile hour, weekly; or `RECONCILE=true`. Full Proton walk with the imported snapshot code (UID-based, resumable folder queue): state rows Proton lacks or mis-sizes are dropped (they re-upload next night); Proton nodes under the mirror root that neither upstream nor state knows are trashed, with evidence recorded. |
| `report`, `ping` | The step summary of section 5.5, built from the state database alone. Ping healthchecks.io; `/fail` on error. |
| `chain` | A workflow step, not a task: when `report` left a `chain` marker (batches remain and this run checkpointed at least one), `gh workflow run sync.yml` queues the next run behind this one in the concurrency group. A run that made no progress does not chain; it fails and pings `/fail`, so a batch that fails identically cannot loop. On atium the next cron tick is the chain. |

### 5.2 State: the SQLite database is the checkpoint, R2 is where it lives

`state.sqlite` is the single durable artifact: the imported evidence tables plus
`mirror_objects` (`path_lower` key → size, `content_hash`, `sha1`, `path_display` NFC,
proton UID) — the record of what landed, advanced only after upload is confirmed. It lives
at `.state/state.sqlite.xz.age` in an R2 bucket beside `.state/session.tar.age`, both
encrypted to the same age recipient because the state's path names are themselves
sensitive (section 7). R2 has no object versioning, so every checkpoint also writes a
dated copy under `.state/history/`; a bad write-back rolls back with one `aws s3 cp` from
the README runbook, and a bucket lifecycle rule expires history after 30 days. The session
gets no history: a stale copy holds a rotated-out refresh token and cannot be restored.
The delta is recomputed from a fresh inventory every run, so there is no
cross-run queue to corrupt and source drift is simply the next run's delta. The state is
also the progress record: the share of the current inventory present in `mirror_objects`
is what the report calls percent mirrored, and nothing about where a run should start
lives anywhere else.

### 5.3 Deletion and history policy

A file removed from Dropbox is trashed in Proton the same night and stays recoverable
until trash is emptied by hand (`task empty-trash` exists and is never scheduled). A file
changed in Dropbox becomes a new Proton revision; Proton's version history is the
cloud-side history. Trash and revisions count toward quota. The Proton plan holds 6 TB
and Dropbox holds about 600 GB, so `CEILING_GB=4000` leaves 2 TB for trash, revisions and
the rest of the account; the ceiling exists to catch runaway growth, not to ration space.

### 5.4 Throttles

The rule: stay about 10% under every published limit. Where a provider publishes none,
keep its client's defaults and let the throttle counters in the report drive changes.

| Provider | Known limit | Setting |
|---|---|---|
| Dropbox API | about 12 calls/s per user, always with `Retry-After` | listing serialized, one request in flight; rclone `--tpslimit 10 --tpslimit-burst 1 --transfers 4`; `Retry-After` honored and every occurrence counted |
| Proton | none published; the CLI ships 5 files in flight and its own 429 back-off | CLI defaults kept as shipped; one upload invocation per batch, one trash pass, one listing walk a week, the pattern Proton's CLI page calls compliant |
| GitHub runner | 6 h per job, ~14 GB disk | `timeout-minutes: 180`, `RUN_BUDGET_MIN=165`; a batch needs at most `2 * BATCH_GB` of disk |

Proton fair use is measured, not known: sustained GB/h and 429s in CLI output are the
gauge, and `BATCH_GB` (default 4) drops if they say so. The budget leaves the last
batch's round-trip, session write-back and report room to finish inside the job timeout.

### 5.5 Reporting

Every run writes one GitHub step summary, built by `report` from the state database
alone so it is identical on any host. Counts only, never names (section 7); error
messages stay in the encrypted state, where `task report` renders the same summary
locally with them. Sections:

- **Mirror status**, from R2 as the source of truth: inventory files and bytes; mirrored
  files and bytes (`mirror_objects` rows matching the inventory); percent mirrored;
  non-downloadable entries skipped; batches and bytes remaining; projected runs remaining
  at this run's throughput; whether this run chained.
- **This run:** budget used of `RUN_BUDGET_MIN`; batches planned, completed, and repeated
  from a killed run; files and bytes fetched, uploaded, skipped as SHA-1 identical,
  round-tripped, mismatched; files trashed; paths that vanished between listing and fetch.
- **Throughput:** GB/h down from Dropbox, up to Proton, down from Proton on round-trip;
  batch durations (min, median, max); hasher throughput.
- **Throttling**, per provider: 429 and `Retry-After` responses, total seconds waited,
  longest single wait, seconds spent in retries; Proton session refreshes seen. The
  Proton CLI backs off internally, so its count comes from CLI output and may undercount;
  the sustained upload rate beside it is the second gauge.
- **Errors and issues:** counts by class (content-hash mismatch, upload failure,
  round-trip mismatch, listing refused, session trouble, command non-zero exit), from
  the `events` and `commands` tables.
- **Verification:** files proven identical by round-trip this run and cumulatively, and a
  mismatch line that must read zero.

Every figure is also written to `events`, so the trend across runs is a query against
the state.

## 6. Secrets: 1Password at runtime, the terraform pattern

A dedicated 1Password vault for this repo, read by a service account scoped to that vault
alone. Secrets are named in exactly two places, and 1Password is the only place one is
stored:

- `op.env`, committed: one `op://<vault-uuid>/<item>/<field>` reference per line,
  vault addressed by UUID so renaming cannot break it. Locally, `task op -- <cmd>` wraps
  the command in `op run --env-file=op.env`; nothing is written to disk.
- `.github/workflows/sync.yml`: the same references, resolved by a SHA-pinned
  `1password/load-secrets-action`. `OP_SERVICE_ACCOUNT_TOKEN` is the only GitHub secret.

The vault holds: `dropbox` (app key, app secret, refresh token — the app is a private
"Full Dropbox" app with only `files.metadata.read` + `files.content.read`; the rclone
remote's token blob rides as a fourth field, injected as `RCLONE_CONFIG_*` environment
variables so no config file exists), `r2` (access key id, secret, endpoint), `age`
(the session/state identity), `healthcheck` (the ping URL). `AWS_REGION=auto` is a
literal in the Taskfile, not a secret — `op run` masks every value it resolves, and
masking the string `auto` corrupts ordinary output.

Never store the service-account token in the vault it reads (circular); it lives in the
personal vault and in the one GitHub secret.

## 7. PII on hosted runners, stated plainly

The mirrored tree includes health and tax documents. During a batch its files sit
decrypted on the runner's ephemeral disk and in memory — inherent to the design: Dropbox
serves plaintext and Proton encrypts client-side inside the CLI at upload. What bounds
this: the runner is a single-tenant VM destroyed after the job, and the same bytes already
sit plaintext-at-rest on Dropbox's own servers, so the run adds a transient infrastructure
party (GitHub/Azure) for minutes a night, not a new steady-state holder.

The engineering consequences, each binding:

1. **Logs carry counts, never names.** A filename is itself PII. The imported redaction
   layer covers secret values; this repo additionally never prints path listings to logs
   or step summaries. The repo is private regardless; Actions logs persist ~90 days to
   anyone with read access.
2. **No third-party code in the job.** `sync.yml` uses exactly one third-party action,
   the SHA-pinned 1Password loader; everything else runs inside the toolbox image with
   dependencies locked by `toolchain.lock.toml`. A dependency in the job can read staging;
   the answer is to have almost none.
3. **No artifacts.** Staging is never uploaded as a workflow artifact.
4. **The state is encrypted at rest.** `state.sqlite` holds every path name, so it is
   age-encrypted in R2 like the session.
5. **Credentials are read-only where possible.** The Dropbox app cannot write; the R2
   token reaches one bucket; the service account reads one vault.
6. **The pipeline is host-portable by construction.** The same image and Taskfile run on
   atium under cron with the same `op.env`. If plaintext on Microsoft infrastructure ever
   becomes unacceptable, the runner moves and nothing else changes.

## 8. Toolbox image and repo layout

`docker/Dockerfile` in the imported style: python-slim pinned via `toolchain.lock.toml`,
which also pins rclone and the `proton-drive` Linux x64 binary by version with Proton's
published SHA-512 checked at build; versions asserted again at preflight so a mounted
binary cannot drift. Added to the image: `age`, `xz`, AWS CLI v2 (s3 only), `task`,
`icu`-based NFC normalization. No keyring, dbus, GPG or pass. The CLI's app version
header stays the official `cli-drive@x.y.z`.

```
dropbox-mirror/
├── Taskfile.yml              the pipeline surface (menu, sync, op, image, run, offline checks)
├── op.env                    op:// references, committed
├── aws.config                single-part under 4 GiB, multipart above
├── config/
│   ├── mirror.toml           the one behavior input (imported strict schema, mirror keys)
│   └── toolchain.lock.toml
├── docker/Dockerfile
├── src/migrator/             imported package: providers/, phases/, state.py, hashing.py, ...
├── tests/                    imported suite + mirror-phase tests; fixtures/ git-excluded
├── docs/superpowers/specs/   this document and its research sidecar
└── .github/workflows/        sync.yml, check.yml
```

## 9. Workflow and dispatch

`sync.yml`: `workflow_dispatch` only, inputs `reconcile`, `empty_trash`,
`budget_minutes`; `concurrency: {group: sync, cancel-in-progress: false}` (overlap kills
the Proton session, and it is what queues a chained or scheduled run behind a running
one); `timeout-minutes: 180`; `permissions: {contents: read, actions: write}`, the write
only for `chain`. Nothing else in this repo starts it: jshvn/dispatch POSTs the dispatch
nightly from a `schedules/dropbox-mirror.ts` on its own cron expression (proposed
`10 2 * * *`; 3 of the account's 5 are in use). The healthchecks.io check matches that
cron with a grace that absorbs a queued run plus a full one; chained runs ping more often,
which it ignores.

`check.yml` on pull requests: pytest, ruff, and `task --dry --force sync` — renders every
command with no network and no credentials.

## 10. Bootstrap and seed

1. Create the 1Password vault and service account; store the token in the personal vault
   and as the repo's one GitHub secret.
2. Create the Dropbox app; run the OAuth code flow with `token_access_type=offline` on the
   laptop; store the three Dropbox fields and the rclone token blob in the vault.
3. Turn Proton telemetry off in account settings. On the laptop:
   `PROTON_DRIVE_CACHE_DIR=./pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file proton-drive auth login`,
   sign in in the browser, then `task session-seal` to age-encrypt and upload the session
   bundle. Create `/my-files/Dropbox` in Proton.
4. Measure: Dropbox total size and count, largest file, largest folder (the ~30k
   entries-per-folder ceiling). `CEILING_GB=4000` (section 5.3).
5. Seed: dispatch `sync.yml` once. The first run finds no state and no history, treats
   the whole tree as the delta, and chains itself until the tree is mirrored; each step
   summary shows percent mirrored and projected runs remaining. This is where fair-use
   throttling shows; slow is fine.
6. Enable the dispatch schedule, at once if convenient: a scheduled run queues behind a
   chained one. Three green nights, then a deliberate edit and a deliberate delete in
   Dropbox must appear in Proton the next morning.

## 11. Acceptance

- A file edited in Dropbox has a new revision in Proton the next morning; a deleted file
  is in Proton's trash; an added file is present with its SHA-1 recorded in
  `mirror_objects`.
- The whole tree is mirrored from one dispatch with no operator input, and every row in
  `mirror_objects` carries round-trip evidence.
- A run killed mid-batch repeats only that batch the next run.
- A run that stops on budget with batches left reports success, states percent mirrored
  and runs remaining, and the next run resumes from R2 alone.
- A run that cannot refresh the Proton session fails loudly, pings `/fail`, and the
  README runbook restores it with one laptop login.
- `task --dry --force sync` renders every command with no network; the imported pytest
  suite passes in `check.yml`.
- A weekly reconcile finds nothing to correct on a quiet week.
- No workflow log line contains a mirrored path name.

## 12. Risks, stated plainly

1. **Session loss.** A crash between token rotation and write-back costs a manual
   re-login; write-back after every CLI call bounds the window, and nightly runs keep the
   60-day idle expiry away.
2. **The key password sits in plaintext inside the session file.** Age-encrypted at rest,
   decrypted only inside the runner for the job. The fallback if unacceptable: a dedicated
   Proton account holding only the mirror.
3. **CLI churn.** 0.8.0 broke a flag; Proton can block a version; the 2027 crypto
   migration will force an upgrade. The binary is pinned and `proton_cli.py` is the only
   place its invocation lives.
4. **Proton's posture toward Azure egress** is untested for refresh calls. The first
   nights answer it.
5. **Scale.** Folders past ~30k entries fail on Proton; measure before seeding.
6. **A Python codebase instead of a Taskfile-only pipeline** — offset by inheriting the
   donor's test suite and keeping all operational behavior in the Taskfile surface.
7. **Chain loop.** A batch failing identically every run would chain forever; a run that
   checkpoints nothing does not chain and fails loudly instead.

## 13. Open questions for iteration

1. The nightly hour (`10 2 * * *` proposed) and the reconcile hour keyed from it.
   Deferred: settled when the dispatch schedule is enabled (bootstrap step 6).
