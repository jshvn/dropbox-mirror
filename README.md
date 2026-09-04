## 🪞 dropbox-mirror

A self-chaining GitHub Actions pipeline that mirrors a Dropbox account into Proton Drive.
After each run Proton Drive holds what Dropbox held at listing time, under one folder of
your choosing. Changed files become new Proton revisions, files that left Dropbox move to
Proton's trash, and nothing is recorded as mirrored until Proton's own upload summary has
accounted for it. The only durable state is one SQLite database, age-encrypted in a
Cloudflare R2 bucket; no run is ever told where to start.

Dropbox stays the primary. Nothing written in Proton Drive flows back.

```
Dropbox (primary, read-only credentials)
  -> GitHub Actions run (this repo, inside one pinned toolbox image)
       inventory -> delta -> plan -> per batch: fetch -> verify -> upload -> confirm -> checkpoint
       -> trash -> reconcile (weekly) -> report -> ping -> chain if batches remain
  -> Proton Drive <destination folder>   (Proton's version history is the cloud-side history)
  -> R2 .state/                          (encrypted state database, dated history, CLI session)
```

The repository is public and holds no account: every credential and every account
identifier (the Dropbox account id, the Proton destination folder and its UID) lives in
one 1Password vault and reaches a run by name.

### Provenance

The infrastructure modules under `src/migrator/` (SQLite evidence schema, one-pass hasher,
atomic writes, redacting logger, path guards, and the two providers for the Dropbox API
and the official `proton-drive` CLI) come from
[donphi/dropbox_proton](https://github.com/donphi/dropbox_proton) at commit `cfd0e57`,
MIT, whose copyright notice is retained in [LICENSE](LICENSE). The mirror phases, the
Taskfile, the toolbox image, and the workflows are this repo's own. The design and its
research live in [docs/superpowers/specs/](docs/superpowers/specs/).

## 🧭 How it works

One run is `task pipeline`, executed inside the toolbox image. Each step is one
`python -m migrator <command>`; the Taskfile owns sequencing and the menu, the Python owns
every decision. Steps before `inventory` run without a state database and are called
commands; the rest are phases and record their evidence in the state.

| Step | What it does |
|---|---|
| `clock` | Stamps the run start epoch, UTC hour and weekday to `.run/clock.json`; clears `staging/`, the report and the chain marker. |
| `session` | Fetches `.state/session.tar.age` from R2, decrypts it with the age identity, unpacks it to `.run/session/`, which is `PROTON_DRIVE_CACHE_DIR` for every later Proton call. |
| `state` | Fetches `.state/state.sqlite.xz.age` and starts the run row. A missing state is accepted as an empty mirror only when `.state/history/` is empty too and the bucket answers a probe; a missing state beside history is refused, because a lost state must never look like an empty mirror. |
| `inventory` | Recursive Dropbox `files/list_folder` walk, each page committed with its cursor. Entries with no `content_hash` (Paper docs, cloud files) are recorded as non-downloadable and excluded. Keeps the newest two inventories. |
| `delta` | Compares the inventory against `mirror_objects` on `(path_lower, size, content_hash)`. Refuses a listing under half the mirrored file count so a truncated listing can never become a trash list. |
| `plan` | Refuses a tree over `ceiling_gb` or a batch the runner's disk cannot hold in staging. Packs changed files into batches of at most `batch_gb` and `batch_files`, an oversized file being a batch by itself. Earlier PLANNED batches are dropped; this run's plan is the only plan. |
| `batches` | Touches the Proton session once with a listing of the destination's parent, then runs each batch through the five steps below. Before each batch it stops when the elapsed time plus the longest batch so far would pass the budget; stopping with batches left is a success that marks the run for chaining. |
| `fetch` | Empties staging and downloads the batch from Dropbox over the API by listed path, `download_workers` files in flight. A path that vanished since listing is counted and skipped. |
| `verify` | Recomputes every staged file's Dropbox content hash and records SHA-1 and SHA-256. A mismatch is a file edited between listing and fetch: removed, counted, never recorded. A batch where every file mismatches fails. |
| `upload` | One `proton-drive filesystem upload` of the staging tree with `-f create-new-revision -d merge --skip-thumbnails`; Proton skips files whose content it already holds. |
| `confirm` | Reads the upload summary and matches transferred and skipped counts against the batch; failures must be zero and the failures list empty, or nothing is recorded. Proton's server verifies every ciphertext block hash at upload; the weekly reconcile walk is the independent observation of what Proton holds. |
| `checkpoint` | Merges the confirmed rows into `mirror_objects` and pushes the state to R2 under `.state/history/<epoch>-<batch>` and then, server-side, to the canonical key. Always the last step of a batch, so a killed run repeats at most one batch. |
| `trash` | Only when every planned batch landed: groups deleted rows by parent folder, one listing and one `filesystem trash` per folder. A folder that cannot be listed is recorded and retried the next run. |
| `reconcile` | On the first run of the configured weekday, or with `RECONCILE=true`: a full Proton walk, comparing Proton's own listed size and SHA-1 against `mirror_objects`. State rows Proton lacks, mis-sizes or mismatches are dropped so they re-upload; Proton nodes under the destination that neither Dropbox nor the state knows are trashed. A walk that does not fit one run's budget resumes where it stopped on the next reconcile run, and a partial walk drops and trashes nothing. |
| `report` | Builds the step summary from the state alone, finishes the run row, writes the chain marker, pushes the state, and returns the run status so a failed run stops before the success ping. |
| `ping` | Pings healthchecks.io; the workflow pings `/fail` instead when anything failed. |

Every step is plan-by-default. `batches`, `trash`, `reconcile`, `report` and `empty-trash`
change anything only with `--apply`, which the Taskfile passes in `task pipeline` and never in
`task plan-pipeline`. No mutation is trusted on its exit status: the state push is trusted
only once the object lands, and nothing is recorded as uploaded on a command's exit code
alone. The evidence has three layers: the upload summary per batch (transferred plus
skipped counts matched against what was verified, zero failures); Proton's own
server-side block hashes, checked at upload time and out of this repo's hands; and the
weekly reconcile walk, which compares Proton's own listing, its size and SHA-1 for every
node under the destination, against `mirror_objects`, independent of anything a batch
claimed.

## 🛠️ Prerequisites

- **A container engine**, running: Apple `container` on macOS, or Docker. The Taskfile picks
  Apple `container` when its daemon is up, else Docker; override with `ENGINE=docker`.
  Every command in this repo, tests included, runs inside the toolbox image built from
  [docker/Dockerfile](docker/Dockerfile) and pinned by
  [config/toolchain.lock.toml](config/toolchain.lock.toml) (Python, `proton-drive`,
  `age`, go-task, all checksum-verified). Nothing else is installed on the host.
- **[go-task](https://taskfile.dev/)**: `brew install go-task`.
- **The 1Password CLI** `op`, signed in and unlocked, for anything that needs the vault on
  the laptop. Nothing from the vault touches disk: `task op -- <cmd>` wraps a command in
  `op run --env-file=op.env`, which resolves the `op://` references in [op.env](op.env) at
  run time and masks their values in output.
- **Accounts**: Dropbox, Proton Drive, a Cloudflare R2 bucket, a healthchecks.io check, and
  a 1Password vault dedicated to this repo.

## 🚀 First-time setup

Every value that names an account is stored in exactly one place, the 1Password vault,
and referenced in exactly two: `op.env` for the laptop and
`.github/workflows/sync.yml` for CI, both holding only `op://<vault-uuid>/<item>/<field>`
references. The twelve references resolve five vault items:

| Item | Fields | Reaches a run as |
|---|---|---|
| `dropbox` | `app_key`, `app_secret`, `refresh_token`, `account_id` | `MIRROR_DROPBOX_APP_KEY`, `MIRROR_DROPBOX_APP_SECRET`, `MIRROR_DROPBOX_REFRESH_TOKEN`, `MIRROR_DROPBOX_ACCOUNT_ID` |
| `proton` | `destination`, `destination_uid` | `MIRROR_PROTON_DESTINATION`, `MIRROR_PROTON_DESTINATION_UID` |
| `r2` | `access_key_id`, `secret_access_key`, `endpoint`, `bucket` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `MIRROR_R2_BUCKET` |
| `age` | `identity` | `MIRROR_AGE_IDENTITY` |
| `healthcheck` | `url` | `MIRROR_HEALTHCHECK_URL` |

### 1. 1Password

Create a vault for this repo and a service account scoped to that vault alone. Store the
service-account token in your personal vault (never in the vault it reads) and as the one
GitHub repository secret, `OP_SERVICE_ACCOUNT_TOKEN`. Find the vault's UUID with
`op vault get <name> --format json` and put it in the references in `op.env` and
`.github/workflows/sync.yml`; the vault is addressed by UUID so renaming it cannot break a
run. The UUID is not a secret: without the service-account token it opens nothing.

### 2. Dropbox

The pipeline reads Dropbox over the API alone, both for listing and for downloading.

1. At https://www.dropbox.com/developers/apps create a Scoped access, Full Dropbox app.
   Under Permissions enable only `files.metadata.read` and `files.content.read`. The
   mirror can never write to Dropbox. Note the App key and App secret.
2. Get a refresh token. Open, with your key filled in,
   `https://www.dropbox.com/oauth2/authorize?client_id=APP_KEY&response_type=code&token_access_type=offline`.
   `token_access_type=offline` is what makes the token permanent. Approve, copy the code,
   and exchange it:

   ```bash
   curl https://api.dropboxapi.com/oauth2/token \
     -d code=THE_CODE -d grant_type=authorization_code -u APP_KEY:APP_SECRET
   ```

   The response holds both an `access_token` (four hours) and a `refresh_token` (never
   expires, never rotates). Store `app_key`, `app_secret` and `refresh_token` as fields of
   the vault item `dropbox`.
3. Record the account id. Mint an access token from the refresh token (this is what every
   run does) and ask who you are:

   ```bash
   curl https://api.dropboxapi.com/oauth2/token \
     -d grant_type=refresh_token -d refresh_token=REFRESH_TOKEN -u APP_KEY:APP_SECRET
   curl -X POST https://api.dropboxapi.com/2/users/get_current_account \
     -H "Authorization: Bearer ACCESS_TOKEN"
   ```

   Store the `account_id` (the whole `dbid:...` string) as field `account_id` of the same
   item. Every run verifies the account it is reading against this value and refuses a
   mismatch, so a swapped credential can never turn another account's listing into a
   trash list.

### 3. Proton Drive

Proton's CLI can only be seeded by a browser sign-in, and fresh sign-ins from datacenter
addresses are blocked, so the session is created once on the laptop and carried to CI as an
encrypted file. Every run restores it before the first CLI call and writes it back after
every CLI call, because its refresh token rotates.

1. Turn telemetry off in Proton account settings.
2. Install the macOS `proton-drive` CLI from https://proton.me/download/drive/cli at the
   version pinned in `config/toolchain.lock.toml`. The session file format is tied to the
   version; the Linux binary in the toolbox must be able to read what the laptop wrote.
3. Sign in, with the session written as plain files under `.run/pd` (the directory must be
   inside this repo, since only the repo is mounted into the toolbox; `.run/` is ignored by
   git and deleted by `task clean`):

   ```bash
   PROTON_DRIVE_CACHE_DIR=.run/pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file proton-drive auth login
   ```

   Afterwards `.run/pd` holds `auth-session.json` and `clientUid.json`.
4. Create the destination folder. In the CLI, `/my-files` is the existing "My files" root
   of your drive, not a folder you create. Create one folder under it:

   ```bash
   PROTON_DRIVE_CACHE_DIR=.run/pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
     proton-drive filesystem create-folder /my-files Dropbox
   PROTON_DRIVE_CACHE_DIR=.run/pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file \
     proton-drive filesystem list -j /my-files
   ```

   Store the folder's CLI path (`/my-files/Dropbox`) as field `destination` of vault item
   `proton`. In the listing find the entry whose `name.value` is `Dropbox` and store its
   `uid` as field `destination_uid`. Every run lists the parent, finds the folder by name,
   and compares its UID with this value before touching it. A listing that shows no
   `Dropbox` entry, or one whose UID differs, fails the run with `configured Proton
   destination did not resolve to exactly one folder` or `did not exactly match the
   listing`; the usual cause is the folder created one level too deep or the wrong
   entry's UID copied.
5. Seal the session into R2 (after step 5 below has created the bucket):

   ```bash
   task session-seal -- .run/pd
   ```

Checked against CLI 0.8.0: the upload flags in
[src/migrator/providers/proton_cli.py](src/migrator/providers/proton_cli.py) (`-f
create-new-revision -d merge --json --skip-thumbnails`) match `filesystem upload --help`;
`-t` is the short form of `--skip-thumbnails` and `--json` is a general option. Re-check
this whenever the pinned CLI version changes.

### 4. age

Run `age-keygen` once and store the `AGE-SECRET-KEY-...` line as field `identity` of vault
item `age`. Both the state and the session in R2 are encrypted to this identity, since the
state holds every mirrored path name.

### 5. Cloudflare R2

Create a bucket and an API token scoped to it, and a lifecycle rule that expires objects
under `.state/history/` after 30 days. Store `access_key_id`, `secret_access_key`,
`endpoint` (`https://<account-id>.r2.cloudflarestorage.com`) and `bucket` as fields of
vault item `r2`; `op.env` resolves them as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_ENDPOINT_URL_S3` and `MIRROR_R2_BUCKET`, the names boto3's S3 client reads directly.
`AWS_REGION=auto` is a literal `ENV` line in [docker/Dockerfile](docker/Dockerfile), not a
vault value: R2 has one region, and `op run` masking the word `auto` would corrupt
ordinary output.

### 6. healthchecks.io

Create a check whose schedule matches the nightly dispatch, with a grace period long enough
for a queued run plus a full one. Store the ping URL as field `url` of vault item
`healthcheck`. Chained runs ping more often than the schedule, which the check ignores.

### 7. Prove the read path

```bash
task image     # builds the toolbox once; every later task reuses it
task test      # the pytest suite, offline
task render    # every pipeline command rendered, no network, no credentials
task plan      # the real thing, read-only: lists Dropbox, fetches the state, prints the plan
```

`task plan` needs the vault and the session, and mutates nothing in Proton. Its output is
the same report a run produces, with the mirror status and the batches it would move.

### 8. Seed

Dispatch the `sync` workflow once from the Actions tab or with
`gh workflow run sync.yml`. The first run finds no state and no history, treats the whole
tree as the delta, and chains itself run after run until the tree is mirrored: each run
stops starting batches at its budget, checkpoints what landed, and queues the next run,
which picks up from the state in R2. Each run's step summary shows percent mirrored and
projected runs remaining. The first run also builds the toolbox image on the runner,
which takes a few minutes before any step logs.

The default budget is 165 minutes under a 180-minute job timeout, which leaves the last
batch's upload and the report room to finish. GitHub's hard limit for one job is six
hours, so `budget_minutes` up to about 340 is safe if fewer, longer runs are wanted.
Actions minutes on a public repository are free, so the seed can run on Actions alone.
For a 200,000-file tree expect about 36 hours of Proton upload at the measured 0.65 s per
file plus 6 to 10 hours of Dropbox downloads, around 45 hours in all, or roughly sixteen
chained runs at the default budget.

`task sync` from a laptop behaves exactly like one chained run: it stops on
`RUN_BUDGET_MIN`, reports percent mirrored, and the next `task sync` picks up from R2. Use
it only while no Actions run is in progress.

### 9. Schedule

Add `schedules/dropbox-mirror.ts` to jshvn/dispatch with `10 2 * * *`. A scheduled run
queues behind a chained one in the workflow's concurrency group. After three green nights,
edit one file and delete one file in Dropbox and confirm both appear in Proton the next
morning: the edit as a new revision, the delete in Proton's trash.

## ▶️ Running it

`task` alone prints the menu, grouped by effect. Everything runs inside the toolbox.

#### Read: looks at Dropbox, Proton and R2, changes nothing

```bash
task plan      # fetch the state, list Dropbox, print what a sync would move
task status    # counts and the last run's figures from the state in R2
```

`task plan` starts a run row in its local copy of the state and never pushes it. `task
status` fetches the state, prints, and starts nothing; both leave `.run/state.sqlite`
behind for inspection. Neither makes a Proton call, so both are safe while CI may be
running.

#### Checks: offline, no credentials

```bash
task test      # pytest inside the toolbox
task lint      # ruff check and format check
task fmt       # ruff format
task render    # dry-run the whole pipeline: every command, no network
```

#### Write: changes Proton Drive and the state in R2

```bash
task sync                          # one budgeted run, the same thing CI runs
RUN_BUDGET_MIN=30 task sync        # with a shorter budget
RECONCILE=true task sync           # force the weekly Proton walk (the literal word true)
task empty-trash                   # permanently delete Proton trash; asks first; never scheduled
task state-rollback                # list the dated history objects in R2
task state-rollback -- <key>       # copy one of them over the canonical state
task session-seal -- .run/pd       # encrypt a laptop Proton CLI session into R2
```

Never run `task sync` or `task empty-trash` from the laptop while a CI run may be in
progress. Two processes holding the Proton session race its rotating refresh token, the
loser's copy dies, and the next run needs a fresh login.

#### Toolbox

```bash
task image              # build the toolbox image (no-op while it exists)
task image-clean        # remove it so the next task rebuilds
task clean              # delete .run, the caches and every other file git ignores
task run -- <cmd>       # any command in the toolbox with the repo at /work
task op -- <cmd>        # the same with secrets from 1Password via op.env
```

`task clean` is `git clean -fdX`: it removes only files git already ignores, so the
decrypted state, a laptop Proton session under `.run/pd`, staging, caches and stray
lockfiles go, and nothing tracked or unignored is touched.

## ⚙️ GitHub Actions

[sync.yml](.github/workflows/sync.yml) is dispatch-only. Inputs: `reconcile` (force the
Proton walk) and `budget_minutes` (override the run budget). `concurrency: {group: sync,
cancel-in-progress: false}` is what queues a chained or scheduled run behind a running
one; overlap would kill the Proton session. `timeout-minutes: 180` against a default budget
of 165 leaves the last batch's upload and the report room to finish. The job uses two
third-party actions, both SHA-pinned: `actions/checkout` and the 1Password loader that
resolves the same `op://` references as `op.env`; everything else runs inside the toolbox.
One `always()` step publishes `.run/report.md` as the step summary and pings `/fail`
unless the job succeeded. When `report` left a `.run/chain` marker, a final step queues the
next run with `gh workflow run`, which is the only reason the job has `actions: write`.
Nothing else in this repo starts a run; the nightly dispatch comes from jshvn/dispatch.

[check.yml](.github/workflows/check.yml) runs `task test`, `task lint` and `task render` on
pull requests. It has no access to the vault, so a pull request from a fork can run it
safely. GitHub registers it when the first pull request is opened.

On a public repository the run logs and step summaries are public too. What they carry:
phase lines, counts, retry warnings with the provider's error class, and the report
tables. What they never carry: a path name, a credential, or an account identifier; the
1Password loader masks every value it exported, `MIRROR_VERBOSE` is off by default so an
error prints as its class, and the report is built from counts alone. Only collaborators
can dispatch the workflow.

## 🔧 Configuration

[config/mirror.toml](config/mirror.toml) is the one behavior input; the schema is strict
and rejects unknown keys. It names no account.

| Key | Meaning |
|---|---|
| `mirror.id` | Name recorded in the state. |
| `dropbox.root` | Subtree to mirror; empty means the whole Dropbox. |
| `dropbox.page_limit`, `minimum_call_interval_seconds` | Listing page size and the serialised call spacing. |
| `dropbox.download_workers` | Files fetched in flight during `fetch`, under the shared Dropbox rate limit. |
| `budget.batch_gb`, `batch_files` | A batch's byte and file caps; an oversized file is a batch by itself. |
| `budget.run_budget_minutes` | Wall-clock budget from the run's start; batches stop starting when it runs out. |
| `budget.ceiling_gb` | Refuse a Dropbox tree larger than this. |
| `budget.disk_headroom_gb` | Free disk the runner must keep beyond a batch's staging. |
| `budget.listing_floor_ratio` | Refuse a listing smaller than this share of the mirrored file count. |
| `reconcile.weekday` | UTC weekday (0 is Monday) whose first run does the Proton walk. |

The account is the environment, all of it read in `src/migrator/env.py` and
`src/migrator/config.py`: the twelve names in `op.env`, of which
`MIRROR_DROPBOX_ACCOUNT_ID` (the `dbid:` the run must be reading; anything else is
refused), `MIRROR_PROTON_DESTINATION` (the CLI path of the mirror root) and
`MIRROR_PROTON_DESTINATION_UID` (its UID, verified on every run before any write) override
the TOML keys `dropbox.expected_account_id`, `proton.destination` and
`proton.expected_destination_uid`, which exist for a private fork that prefers a file.
Run overrides: `RUN_BUDGET_MIN` and `RECONCILE=true`; `MIRROR_VERBOSE=1` to print an
error's full text instead of its class; `MIRROR_WORK_DIR` (default `.run`) and
`MIRROR_CONFIG` (default `config/mirror.toml`). The non-vault `AWS_REGION` literal is an
`ENV` line in the Dockerfile so every process in the toolbox sees it.

## 📊 Reading a run

The step summary is built from the state database alone, so `task status` on a laptop
shows the same figures as the Actions page. It carries counts only, never a path name:

- **Mirror status**: inventory files and bytes, mirrored files and bytes, percent mirrored,
  non-downloadable entries, batches and bytes remaining, projected runs remaining, chain.
- **This run**: budget used, batches planned and completed, files fetched, vanished and
  hash-mismatched, files and bytes uploaded, skipped as content-identical by Proton,
  confirmed, checkpointed, trashed.
- **Throughput**: GB/h down from Dropbox and up to Proton; batch durations (min, median,
  max).
- **Throttling** per provider: rate-limit responses, seconds waited, longest wait.
- **Errors and issues** by class, and **Verification**: files confirmed this run and
  cumulatively, plus the last reconcile walk's state (complete or partial), matched,
  dropped, strays trashed and mismatches.
- **Phases**: the status of every phase of the run.

Error text lives in the encrypted state, in the `events` table. After `task status`:

```bash
task run -- python -c "import sqlite3; c=sqlite3.connect('.run/state.sqlite'); print(*c.execute(\"SELECT timestamp, phase, message, safe_raw_error FROM events WHERE level='ERROR' ORDER BY id DESC LIMIT 20\"), sep='\n')"
```

## 🗄️ State and R2 layout

```
.state/state.sqlite.xz.age                     the state: evidence tables + mirror_objects, runs, batches, deletions
.state/history/<epoch>-<label>.sqlite.xz.age   one copy per checkpoint; label is the batch number, trash, reconcile or report
.state/session.tar.age                         the Proton CLI session (auth-session.json, clientUid.json); no history
```

R2 has no object versioning, so every checkpoint writes the dated copy first and then a
server-side copy to the canonical key; a bad state rolls back with `task state-rollback`.
The session gets no history because a stale copy holds a rotated-out refresh token and
cannot be restored. The work directory `.run/` on the runner holds the decrypted state,
the session and staging for the life of one job; it is never uploaded as an artifact and
is ignored by git.

## 🩹 Runbook

- **`login first` in the state events, or a run that fails at the first Proton call.** The
  session is gone. Repeat setup step 3's sign-in and `task session-seal -- .run/pd`.
- **`configured Proton destination did not resolve to exactly one folder`.** The folder
  named in the vault's `proton/destination` is not a direct child of its parent in
  Proton. List the parent with `filesystem list -j` and either move the folder or fix the
  field.
- **`did not exactly match the listing`.** The folder exists but its UID differs from the
  vault's `proton/destination_uid`. Copy the UID from the listing if the folder was
  recreated on purpose.
- **`MIRROR_DROPBOX_ACCOUNT_ID ... must be a full dbid: identifier` at startup.** The
  vault field is empty or the reference in `op.env` or `sync.yml` names the wrong field.
- **`state object is missing but history exists`.** Roll back with `task state-rollback`.
  Never delete the history to make a run start fresh.
- **The state looks wrong after a run.** `task state-rollback` lists the dated copies;
  `task state-rollback -- <key>` copies one over the canonical state. The next run repeats
  from there, and re-uploads skip content Proton already holds.
- **A run stops on budget every night without finishing.** Lower `batch_files` or
  `batch_gb`; the throughput and batch-duration rows say which. A run that checkpointed
  nothing does not chain and fails instead, so a batch that fails identically cannot loop.
- **Proton 429s or a collapsing upload rate.** The throttling table is the gauge; lower
  `batch_gb`. Dropbox stays at 10 requests a second.
- **A weekly reconcile does not finish in one run.** Normal on a large tree: the walk
  resumes where it stopped on the next run that reconciles, whether that is the following
  scheduled weekday or a run forced with `RECONCILE=true`, and finishes over as many runs
  as it needs.
- **A flag such as `RUN_BUDGET_MIN` seems ignored.** The report's "budget minutes" row
  shows what the run saw. `RECONCILE` takes the literal word `true`.
- **Move the mirror folder in Proton.** Rename or move it anywhere under My files, then
  change the vault's `proton/destination`. The UID survives both, and every run verifies
  it.
- **Switch the Dropbox account.** New `refresh_token` and `account_id` fields from setup
  step 2 signed in as the new account; no commit is needed. The next run trashes what the
  old account had and mirrors the new tree; to start clean instead, empty the Proton
  folder and delete both the state object and everything under `.state/history/` before
  the switch.
- **Move the runner.** The same image, Taskfile and `op.env` run anywhere with a container
  engine and `op`: `task sync` is the whole job and the next cron tick is the chain.

## 🔒 What sits where

The mirrored tree includes personal documents. During a batch its files sit decrypted on
the runner's ephemeral disk and in memory, which is inherent: Dropbox serves plaintext and
Proton encrypts client-side inside the CLI. What bounds it: the runner is a single-tenant
VM destroyed after the job; logs and the step summary carry counts, never names; no
workflow artifact is ever uploaded; the state, which holds every path name, is
age-encrypted at rest; the Dropbox credentials cannot write, the R2 token reaches one
bucket, and the service account reads one vault.

What the public repository holds: code, the toolbox definition, the behavior config, and
`op://` references made of a vault UUID and field names. What it does not hold: any
credential, the Dropbox account id, the Proton folder path or UID, the bucket name or
endpoint, the healthcheck URL, or any mirrored path name.

## 🗂️ Repository layout

```
Taskfile.yml              the operator surface: menu, pipeline, toolbox, op wrapper
op.env                    op:// references, committed; the one place vault names are listed for the laptop
config/mirror.toml        the one behavior input; names no account
config/toolchain.lock.toml  python image digest, proton-drive, age, go-task versions and checksums
docker/Dockerfile         the toolbox image; the repo is bind-mounted at /work
src/migrator/             the package: commands, phases/, providers/, state, store, crypt, session
tests/                    pytest suite, no network; tests/fixtures/live/ is ignored by git
.github/workflows/        sync.yml (dispatch-only, self-chaining), check.yml (pull requests)
docs/superpowers/         the design spec and its research
.run/                     work directory at run time; ignored by git, removed by task clean
```
