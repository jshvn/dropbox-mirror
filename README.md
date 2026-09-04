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
   Add `http://localhost:53682/` to the app's redirect URIs, run
   `rclone authorize dropbox <key> <secret>`, and store the printed token JSON as field
   `rclone_token`. Put your account id in
   `config/mirror.toml` (`dbid:...`, from `users/get_current_account`).
3. **Proton.** Turn telemetry off in account settings. On the laptop:
   `PROTON_DRIVE_CACHE_DIR=./pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file proton-drive auth login`,
   sign in in the browser, create `/my-files/Dropbox`, then
   `proton-drive filesystem list -j /my-files` and copy the folder's `uid` into
   `config/mirror.toml`. Check `proton-drive filesystem upload --help` and confirm the flags
   in `src/migrator/providers/proton_cli.py` `upload_tree` match; record any change here.
   The argv carries no `-t`: the design lists it, the upload does not need it, and this
   `--help` diff is what settles whether the installed CLI wants it back.
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
