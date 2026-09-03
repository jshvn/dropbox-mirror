# Research: mirroring Dropbox into Proton Drive from GitHub Actions (2026-09-03)

Sidecar to `2026-09-03-dropbox-mirror-design.md`. Five research passes, kept as
written so the spec's claims stay checkable. Legend: [verified] read on the cited primary
page or source; [snippet] from a search snippet or a JS-rendered page not fetched in full;
[code] read from a cloned repo; [unverified] could not confirm.

Contents:

1. Pulling a whole personal Dropbox from a headless GitHub Actions job
2. Prior art: Dropbox to Proton Drive, cloud-to-cloud, from CI
3. Proton SDK terms, Proton-provided ingestion, Proton Drive limits, GitHub Actions constraints
4. rclone `protondrive:` as the writer from CI
5. Official Proton Drive CLI as the writer from CI (path A)

---

## 1. Pulling a whole personal Dropbox from a headless GitHub Actions job

All community.dropbox.com threads render client-side and returned title-only, so those are
snippet-grade.

### Bottom line

- The API gives what a diff-and-fetch mirror needs: one recursive `list_folder` walk yields
  `content_hash`, `size`, `server_modified`, `rev`, `path_lower`, `id` for every regular
  file; store the final cursor and the next night `list_folder/continue` returns only
  adds/modifies/deletes. The hash is reproducible locally (4 MiB SHA-256 blocks, SHA-256 of
  the concatenation) and verifies downloads without any extra API call.
- Auth: one-time browser OAuth on a laptop with `token_access_type=offline`, then store the
  refresh token as a GitHub secret. Refresh tokens do not expire and are not rotated on
  refresh, so the secret never needs rewriting. Access tokens last 4 h; refresh once per job.
  Nothing "permanent" exists since 2021-09-30.
- Rate limits are unpublished, per user (per authorization), 429 + always `Retry-After`.
  Practical ceiling reported by rclone users is about 12 API calls/s. No documented
  download-bandwidth or downloads-per-day quota.
- rclone works but will not use recursive listing (no `ListR`; ~119k objects took 14-39 min
  to list in 2025 reports) and needs a writable config to persist refreshed tokens. A
  custom `list_folder` + `content_hash` diff out-performs `rclone sync` on listing cost.
- `dbxcli` is not archived: Dropbox shipped v3.6-v3.7.3 in Jul-Aug 2026 with JSON output,
  `ls --recursive`, refreshable saved credentials via `DBXCLI_AUTH_FILE`, and Paper export.
  All four official SDKs had 2026 releases.
- No rsync/WebDAV/SFTP/S3. Web "download folder" zips are capped at 250 GB / 10,000
  entries; the API's `download_zip` at 20 GB / 4 GB per file / 10,000 entries.
- Nothing in the Developer Terms (effective 2025-03-01) or AUP forbids mirroring your own
  account; they reserve the right to throttle.
- GitHub Actions: 6 h per job, ~14 GB free disk, 10 GB cache per repo, IPs are 7,251 shared
  Azure CIDRs. No evidence Dropbox throttles by IP; limits are per user.

### 1.1 `files/list_folder` with `recursive=true`

- `recursive`: "the response will contain contents of all subfolders. In some cases, setting
  recursive to true may lead to performance issues or errors, especially when traversing
  folder structures with a large number of items. A workaround ... is to set recursive to
  false and traverse subfolders one at a time." `limit` is approximate.
  `include_mounted_folders` default true. [verified:
  https://github.com/dropbox/dropbox-sdk-go-unofficial/blob/master/v6/dropbox/files/types.go ;
  https://dropbox-sdk-python.readthedocs.io/en/latest/api/files.html]
- `FileMetadata` carries `content_hash`, `size`, `server_modified`, `client_modified`,
  `rev`, `is_downloadable`, `export_info`, `symlink_info`, `sharing_info`,
  `file_lock_info`. `content_hash` is `omitempty` in the wire schema; treat absence as
  possible (observed for exportable/cloud docs). [unverified: which non-regular files omit it]
- Ordering of entries in recursive mode is not documented. Do not depend on it.
  https://developers.dropbox.com/dbx-file-access-guide
- "auth.RateLimitError may be returned if multiple files_list_folder() or
  files_list_folder_continue() calls with same parameters are made simultaneously by same
  API app for same user." [verified: https://dropbox-sdk-python.readthedocs.io/en/latest/api/dropbox.html]
- Large-account failure mode: recursive root cursors growing from ~6,800 to ~7,400 chars
  then 504s; Dropbox's answer was a ticket. Mitigation: per-top-level-folder cursors.
  [snippet: https://community.dropbox.com/en/discussion/721093/ ;
  https://www.dropboxforum.com/discussions/101000014/if-the-cursor-is-too-long-list-folder-continue-api-always-encounter-504-timeout/603410]
- rclone does not use recursive mode because `path_display` casing is unreliable for deep
  entries. Use `path_lower` as the key and `path_display` for the on-disk name. [verified:
  https://github.com/rclone/rclone/blob/master/backend/dropbox/dropbox.go]

### 1.2 Pagination and delta between runs

- `has_more` + `cursor` -> `list_folder/continue`. Cursors are "pointers to the folder at a
  particular time"; a stored cursor replayed later returns only adds/modifies/deletes
  (`DeletedMetadata`). "long-lived, but may expire if unused for an extend time"; expired ->
  409 `reset` -> restart with `list_folder`. `list_folder/get_latest_cursor` gives a cursor
  without enumerating. [verified: https://developers.dropbox.com/detecting-changes-guide]
- `list_folder/longpoll`: irrelevant for a nightly job.

### 1.3 Download endpoints

- `files/download`: returns bytes; `unsupported_file` = use `files/export`. No documented
  size ceiling. Range requests reported to work (resume). [snippet:
  https://community.dropbox.com/en/discussion/468046/download-api-resume]
- `files/get_temporary_link`: "This link will expire in four hours and afterwards you will
  get 410 Gone." Handy for streaming with plain curl. [verified: Python SDK docs]
- `files/download_zip`: "folder must be less than 20 GB ... any single file within must be
  less than 4 GB ... fewer than 10,000 total file and folder entries". Not useful here.
- `files/export`: for `export_info.export_as` files; `ExportResult.export_metadata.export_hash`
  "Similar to content hash". A 2021 thread reported hash mismatches on Paper docs with
  emoji; treat export verification as best-effort.

### 1.4 What is NOT downloadable

- "Google documents (Docs, Sheets, or Slides), Dropbox Paper, and Microsoft Office docs"
  created in the cloud; listed with `is_downloadable=false`, must be exported. [verified:
  https://developers.dropbox.com/dbx-file-access-guide]
- Paper: post-2019 docs are `.paper` files; export formats html, markdown.
  https://developers.dropbox.com/paper-migration-guide
- Shortcuts: non-downloadable `.web` files; no documented export format. Expect
  `unsupported_file`; log and skip.
- Files "shared with you" but not added to your Dropbox are outside the namespace.
- macOS packages: stored as ordinary folders. [unverified]

### 1.5 `content_hash`

"Split the file into blocks of 4 MB (4,194,304 bytes)"; SHA-256 each block; concatenate
the binary digests; SHA-256 the concatenation; hex. Empty file = SHA-256 of empty string.
Reference implementations: https://github.com/dropbox/dropbox-api-content-hasher (2018,
still correct). [verified: https://www.dropbox.com/developers/reference/content-hash]

### 2. Auth for headless use

- Authorize URL `https://www.dropbox.com/oauth2/authorize?client_id=APP_KEY&token_access_type=offline&response_type=code`,
  exchange the code at `https://api.dropboxapi.com/oauth2/token`. [verified:
  https://developers.dropbox.com/oauth-guide]
- Refresh: POST `/oauth2/token` with `grant_type=refresh_token`; response has only
  `access_token`, `expires_in`, `token_type`. "Refresh tokens are long-lived and do not
  expire automatically" but can be revoked. No rotation. [verified:
  https://dropbox.tech/developers/using-oauth-2-0-with-offline-access ;
  https://github.com/dropbox/dropbox-sdk-python/blob/main/dropbox/dropbox_client.py]
- Access tokens: "sl." prefix, ~4 hours. [snippet]
- Permanent tokens retired 2021-09-30. [verified:
  https://dropbox.tech/developers/migrating-app-permissions-and-access-tokens]
- Scopes for read-only mirror: `files.metadata.read`, `files.content.read`, optionally
  `account_info.read`. Permission type "Full Dropbox". [verified:
  https://www.dropbox.com/developers/reference/developer-guide]
- Development status: "can only be linked with up to 500 total Dropbox users"; nothing
  limits the owner's own account.
- PKCE lets you drop the app secret for a public client.

### 3. Rate limits and quotas

- "enforces rate limits ... on a per-authorization basis"; "does not publish exact rate
  limits"; 429 with `too_many_requests` or `too_many_write_operations`; "Rate limited
  responses always include a Retry-After header"; "Rate limited requests themselves also
  count". [verified: https://developers.dropbox.com/dbx-performance-guide ;
  https://developers.dropbox.com/error-handling-guide]
- Staff: "we cannot increase the limits for any particular app, user, or team." [snippet]
- Empirical: rclone users converge on `--tpslimit 12 --tpslimit-burst 0`; 429s come with
  15 s or 300 s waits. [verified: https://forum.rclone.org/t/dropbox-sync-is-very-slow/50205 ;
  https://forum.rclone.org/t/dropbox-sync-stuck-at-checking-when-theres-a-lot-of-files-too-many-requests/44832]
- File sizes: account max 2 TB per file; path length "recommended under 260 characters".
  https://help.dropbox.com/sync/upload-limitations
- File counts: limited by storage; 300,000 is a desktop-sync soft limit only.
  https://help.dropbox.com/storage-space/file-storage-limit

### 4. rclone `dropbox` backend (v1.75.0)

- Headless auth: `rclone authorize "dropbox"` on the laptop, paste into config; or own app
  via `--dropbox-client-id/secret` and `--dropbox-token` JSON blob. https://rclone.org/remote_setup/
- "When token-based authentication are used, the configuration file must be writable."
  Tolerable for Dropbox because the refresh token never rotates. Open issue #7825.
- `--fast-list` unsupported (no ListR); one call per folder. https://rclone.org/overview/
- Hashes: `rclone lsjson --recursive --hash --hash-type dropbox remote:` or
  `rclone lsf --format "phs" --hash dropbox`; `--checksum` sync compares them.
- `--dropbox-batch-mode` is upload-only. Exports: `--dropbox-export-formats html,md`.
- Useful for batches: `--max-transfer 4G` (exit 8), `--files-from`, `--no-traverse`.
- Open Dropbox issues 2026-09-03: #9706/#9705 shared-folder root/case, #9221, #8391,
  #8392, #8204, #5491.

### 5. CLIs and SDKs

| Project | Archived? | Latest | Notes |
|---|---|---|---|
| dropbox/dbxcli | No | v3.7.3, 2026-08-31 | "scriptable Dropbox CLI ... for scripts, CI jobs": `--output=json`, `ls --recursive`, `get -r`, Paper export, `DBXCLI_AUTH_FILE` refreshable credentials, exit 6 = `rate_limited`. "not a formally supported Dropbox product". Whether JSON `ls` includes `content_hash`: [unverified] |
| dropbox-sdk-python | No | v12.2.1, 2026-07-20 | auto refresh via `oauth2_refresh_token`+`app_key` |
| dropbox-sdk-js | No | v10.38.0, 2026-07-23 | |
| dropbox-sdk-go-unofficial | No | v6.6.1, 2026-08-18 | underpins dbxcli and rclone |
| dropbox-sdk-java | No | v8.0.2, 2026-07-15 | |
| dropbox-api-content-hasher | No | last commit 2018 | reference hash code |

### 6. Non-API routes

"WebDAV, email attachments, and FTP access is not on the current feature roadmap"
(https://help.dropbox.com/integrations/webdav-or-ftp). No SFTP/rsync/S3 (absence inferred).
Web folder download: under 250 GB and 10,000 files (https://help.dropbox.com/sync/download-entire-folders).

### 7. Policy

- Developer Terms effective 2025-03-01: 1.3.2 "Dropbox may set and enforce limitations on
  Developer's use of the DBX Platform, such as limiting the number of calls accepted".
  https://www.dropbox.com/developers/reference/tos
- AUP bars scraping outside supported interfaces and using Dropbox "to back up, or as
  infrastructure for, your own cloud services". A nightly delta pull of your own account
  fits. https://www.dropbox.com/acceptable_use [interpretation]

### 8. GitHub Actions specifics

- 4 vCPU / 16 GB / 14 GB SSD (public); 6 h per job; ~14 GB free on current images.
  https://docs.github.com/en/actions/reference/runners/github-hosted-runners ;
  https://docs.github.com/en/actions/reference/limits ;
  https://github.com/actions/runner-images/discussions/9329
- `actions/cache`: immutable per key, 10 GB/repo, evicted after 7 days unused.
- IPs: Azure ranges from `api.github.com/meta`, 7,251 CIDRs. No report of Dropbox
  throttling Actions ranges. [unverified absence]
- No documented egress cap.

### Design notes from this pass

1. `list_folder(path="", recursive=true, limit=2000)` then `continue`; key by `path_lower`;
   write under `path_display`. If root cursors ever 504, shard per top-level folder.
2. Change detection on `(content_hash, size)`; ignore `server_modified`/`rev`.
3. Batches of at most 4 GB; `files/download` (Range for resume) or `get_temporary_link` +
   curl; recompute the Dropbox hash; only then update state. `is_downloadable=false` ->
   `export`, verify `export_hash`, store under a distinct key.
4. Serialize listing calls, ~10-12 rps total, obey `Retry-After`.
5. Refresh once at job start; the refresh token in secrets is stable.

---

## 2. Prior art: Dropbox to Proton Drive, cloud-to-cloud, from CI

Bottom line: nothing matches "GitHub Actions nightly, list-diff against a state file, <=4 GB
batches, mirror Dropbox into Proton Drive with deletes." Closest: (a) one migration
appliance (Dropbox to Proton, official CLI, Docker, one-shot, no deletes), (b) one reusable
Actions workflow uploading PDFs to Proton via rclone with username/password secrets, (c)
generic `rclone sync` cron workflows between other clouds. No hosted transfer service
supports Proton Drive except CloudsLinker (self-claimed; unverified). Proton offers no
Dropbox import.

### "Dropbox to Proton Drive"

| Hit | What | Updated | Distance |
|---|---|---|---|
| https://github.com/donphi/dropbox_proton "Dropbox to Proton Drive Migrator" | Dockerized Python appliance. Two Dropbox inventories (API + rclone) reconciled, download to a verified local archive (SHA-256/SHA-1/Dropbox hash), SQLite checkpoints, "delta uploads" via the official CLI only, round-trip verification. "No Dropbox deletion implementation, no Proton deletion implementation." Auth via `proton-drive auth login` into a mounted keyring volume. | Created 2026-08-27, pushed 2026-09-03; 0 stars; pre-release | Closest existing tool; one-shot, local staging, no deletes, no schedule, no CI. Its `MIGRATION_SPEC.md` shows `filesystem list --json` + sha1 diff on the Proton side. |
| https://rcloneview.com/support/blog/migrate-dropbox-to-proton-drive-rcloneview | Desktop rclone GUI with scheduler | 2026-06-07 | Desktop, not hosted |
| https://medium.com/@cyberwojak/how-to-transfer-files-from-dropbox-to-proton-drive-89f43159733b | `rclone copy dropbox: protondrive:` once | 2024 | Manual |
| https://blober.io/kb/articles/migrate-to-or-from-proton-drive/ | Desktop transfer tool, "not a sync client" | 2026 | Desktop |
| SEO guides (revolutechs, cloudslinker, openmyanything) | download/re-upload | 2025 | Nothing automated |
| Python Dropbox SDK + Proton SDK scripts | None found (Proton's SDK is TypeScript/C#; a community Go SDK exists: https://github.com/clayliddell/proton-drive-go-sdk/) | | |
| Reddit / HN / rclone forum / uservoice | No thread on Dropbox->Proton specifically. Uservoice "Easy Switch for Google Photos" (212 votes) has comments asking for Dropbox import; no Proton response. https://protonmail.uservoice.com/forums/932839-proton-drive/suggestions/48121973-easy-switch-for-google-photos | | Empty |

Adjacent: https://github.com/jat255/google-proton-drive-sync (GDrive <-> Proton via a fork of
the official CLI, "vibe coded", 2026-05-06), https://github.com/gcruchon/ditch-google
(Google Photos -> Proton Photos via Takeout; ADR on why `rclone sync gphotos: protondrive:`
does not work).

### Cloud-to-cloud services with Proton Drive as destination

- MultCloud: not supported (https://www.multcloud.com/product.html ; forum thread 3466).
- CloudFuze, cloudHQ, Mover.io, Air Explorer: no Proton. Wondershare InClowdz: no Proton,
  shutting down 2026-08-25.
- CloudsLinker (HK): lists "ProtonDrive" among connectors (https://www.cloudslinker.com/);
  can only be a reverse-engineered login server-side. Unverified; do not trust with credentials.
- https://github.com/josep2/overthelake (2021, dead): a SaaS using a GitHub Actions
  `workflow_dispatch` as transfer worker, `rclone copy --max-transfer --cutoff-mode=cautious`.

### GitHub Actions running rclone between two clouds

Actions that write to Proton Drive:

- https://github.com/marvinkassabian/latex-document-ops/blob/main/.github/workflows/upload-proton-drive.yml
  reusable `workflow_call`: installs rclone, writes `rclone.conf` with `type = protondrive`,
  `username`, obscured `password`, optional `mailbox_password` from secrets, uploads PDFs.
  No 2FA, no schedule, no deletes. Pushed 2026-03-20, 0 stars. The one proof that rclone's
  protondrive backend has been driven from `ubuntu-latest` with plain secrets.
- https://github.com/yaudahj/github-actions-sandbox: a commented-out `rclone copy ... protondrive:`.
- No workflow anywhere invokes the official `proton-drive auth login` / `filesystem upload`
  from Actions.

Cloud-to-cloud rclone on Actions cron (no Proton): https://github.com/dotfrankruan/drive-sync
(OneDrive -> Google Drive every 6 h; README now says "we do not recommend this project as
your replication method"; last push 2022-12), https://janlukas.blog/dev/backup-onedrive-s3-actions
(nightly `rclone sync --delete-during`), https://github.com/wekingchen/aliyundrive-sync
(persists a refreshed token by `rclone copyto` into a cloud remote),
https://github.com/jarvanh/actions , https://github.com/yukaidi1220/openlist-to-rclone
(`--max-transfer 50G`, pushed 2026-09-02). Wrappers: https://github.com/AnimMouse/setup-rclone ,
https://github.com/wei/rclone , https://github.com/biati-digital/rclone-github-action ,
https://github.com/digital-clouds/rclone-action , https://github.com/andreiio/rclone-action .

Token refresh persistence in CI: `AnimMouse/setup-rclone/update-config@v1` writes the
modified config back to the repo secret with a fine-grained PAT;
https://github.com/KevinNitroG/Rclone-Auto-Refresh-Token keeps tokens warm. For Dropbox
this is moot (refresh token non-rotating; rclone#4792, rclone forum 31145).

### Chunked/batched mirrors with a state file in CI (ctan-like)

Nothing comparable found. Only `overthelake` and `openlist-to-rclone` use `--max-transfer`
per run. The "list upstream, diff against state stored in the destination, cap per run,
resume across runs" shape appears to be jshvn/ctan alone in public.

### Proton-side: Dropbox import, Easy Switch, roadmap

- Official guidance is manual: https://proton.me/support/import-files-dropbox (download
  zip, unzip, drag into desktop or web app). Easy Switch for Drive exists only for Google.
- Roadmap (https://proton.me/drive/roadmap): 2026 items are SDK rebuild, CLI, crypto
  update, mobile/perf, Sheets. No import item. Q1 2026 recap: none.
- Proton Drive CLI: `filesystem list/upload/download/trash/delete`, `sharing`, `--json`,
  conflict strategies. No `sync`. Auth browser-based; session in OS keyring or
  `PROTON_DRIVE_CREDENTIALS_STORE=pass|unsafe_file`. Trap documented by
  https://github.com/osirison/proton-drive-sync-engine : with non-interactive stdin, a
  conflict prompt sees EOF and the CLI silently skips the file while exiting 0 unless a
  strategy is passed.
- Uservoice: rclone support (199 votes, open; support reply: "we are not actively blocking
  Rclone" but it "does not implement local state caching"); "Auto delete trash" (60 votes).

### Proton as an rclone `sync` destination with deletes; trash, versions, quota

Quota facts: "Trashed files and folders ... still count toward your Proton Drive storage"
(https://proton.me/support/proton-drive-delete-restore-synced-files). No auto-empty.
Versions count: free 10 versions/7 days, paid up to 200 versions for 10 years; can be
disabled per account (https://proton.me/blog/drive-version-history). The official CLI's
`delete` is permanent but only for paths already under `/trash`, so a mirror-with-deletes
has to `trash` then `delete`. Nobody has published a long-run report of quota growth.

Reported experiences with `rclone sync ... protondrive:`:
https://forum.rclone.org/t/protondrive-sync-not-deleting-files-from-destination-no-longer-in-the-source/50648
(deletes silently didn't happen on a large tree),
https://forum.rclone.org/t/sync-to-proton-drive-never-finishes/44538 ,
https://forum.rclone.org/t/proton-drive-too-many-recent-api-requests/44276 (throttled after
~3 min; advised `--checkers=2 --transfers=1`),
https://forum.rclone.org/t/does-proton-silently-rate-limit-accounts-that-use-rclone-api-with-proton-drive/52548
(rate limiting bleeds into the web UI; support can raise thresholds per account),
CAPTCHA `Code=9001`: https://github.com/rclone/rclone/issues/9397 (Proton's dlaumen: "Proton
does not prohibit the usage of rclone, but does not offer formal support"),
https://github.com/rclone/rclone/issues/7967 ,
https://forum.rclone.org/t/rclone-can-t-connect-to-proton-drive-captcha-errors/52302 ,
HN https://news.ycombinator.com/item?id=46024584 ("they also block many cloud provider's egress").
https://github.com/NorthLabPro/ugreen-proton-rclone-backup uses `copy` not `sync`, pins 1.74.4.

rclone protondrive status: Tier 4; #8873 (mark unsupported) became the tier system; #9252
(find a maintainer) open; PR 9260 (merged 2026-04-24) sends `external-drive-rclone@<ver>`
at Proton's request; open breakage #9622 (3.x-client-created paths unresolvable; PR 9851
open); #9844 fixed in Proton-API-Bridge v1.0.5 (2026-08-01); PR 9522 (browser-fork login)
open.

Community mirror-with-deletes tools (all local -> Proton, none from CI):
https://github.com/lafontaj/proton-drive-cli-sync (`--delete` mirror to trash or
permanent, fingerprint cache, systemd), https://github.com/thanosipsis/proton-drive-sync-wrapper
(SQLite generations, mass-delete limits), https://github.com/will-roscoe/protonfs
(`.protonfs/index.json` manifest, headless keyring bootstrap `doctor --fix`,
verify-before-delete), https://github.com/ColinMario/Protondrive-for-Linux ,
https://github.com/DamianB-BitFlipper/proton-drive-sync (headless auth "does not work with
accounts that have 2FA"), https://github.com/traktuner/docker-proton-drive-backup ,
https://github.com/ashishdevasia/ha-proton-drive-backup .

---

## 3. Proton SDK terms, Proton-provided ingestion, limits, GitHub Actions

### `@protontech/drive-sdk` from a custom script

- Root README: the SDK "does not include: Authentication or login flows; Session
  management; User address provider." https://github.com/ProtonDriveApps/sdk/blob/main/README.md
- `incubating/account/README.md`: "This module is not planned to be published or promoted
  outside of the incubating directory ... a temporary solution until the Account SDK is
  available." `package.json` is `"private": true`; vendor it from the repo.
- `incubating/account/js/src/auth.ts`: `authViaPassword(username, password)` does full SRP
  but has no TOTP/2FA handling in the JS module (only the generated types and the C#
  module mention 2FA), and no mailbox-password handling seen [unverified beyond grep].
  `authViaWeb(onSignInUrl)` is the session fork the CLI uses; the URL can be opened on any
  device.
- The CLI has no password login command; login is browser-only.

Third-party rules (root README, verbatim):

- "Personal, non-commercial projects | Allowed under Guidelines below." "Commercial or
  production third-party apps | The SDK is not yet ready for third-party production use."
- "Rate limits are per session and user, thus third-party applications use the same
  rate-limiting policy as Proton first-party Drive clients."
- "Use the SDK ... If you make direct API calls, your application must implement the same
  correctness and safety guarantees as the SDK."
- "Identify your application | Set the `x-pm-appversion` HTTP header ... `external-drive-myapp@1.2.3-stable`
  ... Third-party clients that seek to masquerade as official Proton first-party clients
  are forbidden."
- "Use event-based sync | Synchronize data using Drive events. Do not poll the API or
  perform frequent recursive traversals of the file tree."
- "No Proton branding"; "Credential handling disclosure"; "failure to comply ... may result
  in your third-party application being limited or blocked".
- "The MIT license governs use of the source code in this repository only. Access to
  Proton's hosted services ... remains subject to separate terms of service."
- Crypto migration "end of 2026/early 2027 ... any client that only implements the previous
  cryptography including older SDK releases will not interoperate until upgraded."
- App registration / approval: none; the header naming convention is the entire
  "registration". CLI README: forks should set `CLI_APP_VERSION_NAME`; official builds use
  `cli-drive`, "Do not use this identifier for any other purpose."

DamianB-BitFlipper/proton-drive-sync: own SRP impl with TOTP support in code, sends
`external-drive-proton_drive_sync@1.0.0`; Docker headless "does not work with accounts
that have 2FA/TOTP enabled"; trashes remote on local delete; effectively stalled (last
commit 2026-03-05, pins SDK ^0.7.0 vs npm latest 0.21.0, 62 open issues, fix PR #156 for
the June-2026 "Invalid access token" breakage #155 unmerged as of 2026-08-28). CAPTCHA
`code 9001` on login (#147, #129, #132; users report disabling Proton Sentinel clears it).

Verdict: within terms for personal use with an honest header; practical via the CLI, marginal
via the raw SDK (`authViaPassword` works headless only without 2FA).

### Proton-provided ingestion

- Easy Switch covers mail, calendars, contacts only. https://proton.me/support/easy-switch
- Dropbox -> Proton Drive is manual (zip, unzip, drag). https://proton.me/support/import-files-dropbox
- Nothing Proton-provided is continuous or scriptable.

### Proton Drive limits

| Limit | Value | Source |
|---|---|---|
| Max file size | Only quota: "if you have 10 GB of unused storage, you can store a file of up to 10 GB" | https://proton.me/support/drive |
| Files per folder | [unverified] No Proton page documents 30,000 or 32,000; the uservoice post is gone; the macOS app source hard-codes `maxChildrenInFolder = 32_000` (see the first research sidecar) | https://community.cryptomator.org/t/too-many-files-to-use-with-proton-drive/13642 |
| Nesting depth / path length | Not documented | |
| Filename length | 1 to 255 characters | https://proton.me/support/drive-filenames |
| Rejected names | "." and "..", leading/trailing spaces, trailing periods, RTL-override, narrow no-break spaces, invisible characters. Otherwise "supports all Unicode characters." Windows-forbidden characters are download-side problems | same |
| Case sensitivity | Case-sensitive: "filename", "Filename", "fileName" are three files | same |
| Duplicate names | Not allowed in the same location | same; https://rclone.org/protondrive/ |
| Unicode NFC/NFD | Not normalized server-side (implied by a 2026-04-09 feature request, no response). Normalize to NFC before upload. [unverified] | https://protonmail.uservoice.com/forums/932839-proton-drive/suggestions/51184711 |
| Bulk download | 250 GB or 10,000 files per download [snippet] | |
| Bandwidth / daily upload cap | None documented. Over-quota: "You can't upload new files" | https://proton.me/support/free-plan-limits |
| Rate limiting | Not numerically documented; "may be rate-limited or blocked" | SDK README |
| Network/device blocks | "we may occasionally and temporarily limit the activity originating from a network or device"; VPN/datacenter users "subject to more security checks, rate limits, and 'Are you a human?' verification" | https://proton.me/support/log-in-temporarily-blocked |

Proton Terms of Service (2026-06-23, https://proton.me/legal/terms): 2.10 prohibits
"Accessing the Services through automated means ... in a manner that is distinguishable
from the standard client behavior of human users, that deviates significantly from normal
usage patterns"; 2.11 "automated access to the Services is permitted provided that the
resulting traffic remains indistinguishable from the standard client behavior of human
users ... Users engaging in such activity shall not be eligible for support"; "We reserve
the right to limit service capacity for Accounts which use of resources (e.g. bandwidth)
is excessive".

### GitHub Actions constraints

| Topic | Fact | Source |
|---|---|---|
| `schedule` reliability | "can be delayed during periods of high loads ... High load times include the start of every hour." Min 5 min. Default branch only. | https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#schedule |
| 60-day auto-disable | Public repos: scheduled workflows disabled after 60 days without activity | same |
| Timeouts | 6 h per job; 35-day run limit | https://docs.github.com/en/actions/reference/limits |
| Disk | 14 GB SSD guaranteed; observed 13-21 GB free depending on image | https://github.com/actions/runner-images/discussions/9329 ; issue 13528 |
| Egress | No documented cap | |
| Runner IPs | Azure ranges, `api.github.com/meta`, updated weekly | |
| Proton blocking GHA IPs | No report naming Actions/Azure. CAPTCHA evidence is about non-browser SRP login; the CLI's session-fork bypasses it because the SRP login happens in the browser. Risk is on login, not on uploads with a valid session; the June-2026 refresh breakage (#155) shows refresh behaviour can change. | rclone #9397; proton-drive-sync #147 |
| Secrets | 48 KB each; 100 per repo. A CLI `auth-session.json` fits, but the refresh token rotates, so the runner must persist the updated session somewhere writable | https://docs.github.com/en/actions/reference/security/secrets |
| Cache as session store | Entries unused for 7 days are removed; 10 GB/repo; immutable per key | https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/caching-dependencies-to-speed-up-workflows |
| Artifacts | 90-day default retention (1-400 private); `gh api` can update a repo secret from a workflow | |
| Headless keyring | `dbus-run-session` + `libsecret-1-0`, or `PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file` + `PROTON_DRIVE_CACHE_DIR` | https://baizaar.tools/proton-drive-cli-2026-guide/ ; traktuner README |

Could not verify: per-folder file limit from a Proton page; nesting/path length; NFC
normalization; the 250 GB / 10,000 download figure; two-password handling in
`authViaPassword`; any Proton statement about Azure/GitHub ranges.

---

## 4. rclone `protondrive:` as the writer from CI

Verdict: not viable as specified (fresh SRP login every run from a GitHub-hosted runner).
Four independent blockers:

1. Fresh login per run is the documented CAPTCHA trigger with no programmatic recovery.
   `Code=9001` on `/auth/v4`; go-proton-api only defines the code
   (https://github.com/rclone/go-proton-api/blob/master/response.go). A forum log shows it
   firing after two attempts ~22 s apart from a home IP
   (https://forum.rclone.org/t/rclone-can-t-connect-to-proton-drive-captcha-errors/52302);
   Proton support told an rclone user to sync "once per day (or even less frequently)" and
   "avoid its usage" (https://github.com/rclone/rclone/issues/9397).
2. v1.75.0 writes files the Proton web app cannot open and can corrupt uploads on retry.
   Fixes are on master only (2026-08-24, 2026-08-03). No v1.75.1 as of today
   (https://forum.rclone.org/t/proton-drive-unable-to-decrypt/54087 ;
   https://github.com/rclone/rclone/commit/d9aa9033). jonvk (Proton): "There is no plan to
   work on v6 support for secret key packets ... delete and re-upload those files."
3. Proton calls rclone's continued functionality a "temporary exemption" ending after an
   SDK grace period (https://forum.rclone.org/t/proton-drive-x-rclone/53609, dlaumen
   2026-03-31), and as of 2026-09-02 ncw suspects newer accounts/shares already reject
   legacy-format uploads with `422 Could not verify the nodeKey (Code=200501)`
   (https://github.com/rclone/rclone/issues/9622).
4. No evidence anyone runs it from GitHub Actions or any datacenter IP successfully. The one
   VPS report hit CAPTCHA on the first attempt
   (https://forum.rclone.org/t/captcha-error-when-using-proton-drive/47879).

Backend facts (from https://raw.githubusercontent.com/rclone/rclone/master/backend/protondrive/protondrive.go):
options `username`, `password`, `mailbox_password`, `2fa`, `otp_secret_key` (real; added
v1.72.0 via PR #8843), hidden `client_uid/access_token/refresh_token/salted_key_pass`
written back after login and on every refresh (session reuse works if the config is
persisted and writable; the refresh token rotates), `app_version` sends
`external-drive-rclone@<version>`, `replace_existing_draft`, `enable_caching` (no event
system, stale if other clients write). #9764: a 2FA-less account gets
`Code=2028 "This version of the app is no longer supported"`; enabling 2FA fixed it. Argon2
key unlock at 64-128 MiB per Fs (#9816).

Feature matrix: SHA-1 hashes (case bug fixed PR #9445); modtime written on upload, read
back, not settable afterwards; `sync` with deletes yes, deletes go to trash
(`MoveFileToTrashByID`); `--backup-dir` yes (Move/DirMove implemented); server-side copy
no; `--max-transfer`/`--max-size` yes; `rclone cleanup` empties trash; no stream upload; no
multithread; uploads are 4 MB blocks; duplicates not allowed. Retry: 429/503 via
`Retry-After`; `Update` uses `CallNoRetry` because of #9722.

Throughput: no benchmark. #7413 (270 GB / ~100k files, days, 4-5 MB/s check phase); PR
#8058 cut a 500-file bisync resync from ~45 to ~5 min; listing "tens of seconds"; `429 Too
many recent API requests` mid-sync on the July 2026 beta (#9132). Account actions: May 2024
support "stop using Rclone" (https://forum.rclone.org/t/rclone-blocked-by-proton/46203);
Sept 2024 "TOO MANY API REQUESTS" in web UI too (forum 52548). Users run `--transfers 2-4
--checkers 4`.

If proceeding anyway: pin a v1.76.0 beta dated 2026-08-24 or later; bootstrap from a
browser-capable IP and persist the full `rclone.conf`; self-hosted runner with fixed IP;
once per day; `--protondrive-replace-existing-draft=true --retries 1 --backup-dir`;
`rclone cleanup` weekly; `rclone check`; never write to the destination with an official
3.x client (#9622).

---

## 5. Official Proton Drive CLI as the writer from CI (path A)

Source snapshot: `ProtonDriveApps/sdk` at commit `c8d03244` (2026-08-26). File paths are
relative to that checkout. CLI v0.8.0 released 2026-08-12 per `cli/CHANGELOG.md`.

### What `unsafe_file` persists

File: `<PROTON_DRIVE_CACHE_DIR>/auth-session.json`, mode `0600`
(`cli/src/credentials/fileCredentialsStore.ts`). With `PROTON_DRIVE_CACHE_DIR` set, cache,
app data and logs all collapse into that one directory (`cli/src/config.ts`), beside
`clientUid.json`, `events.json`, `events.lock`, `cache-crypto.sqlite`,
`cache-entities.sqlite`, `proton-drive.log`.

Format (`cli/src/credentials/interface.ts`, validated by `parseCredentials.ts`):

```json
{
  "cachePassword": "<base64 32 random bytes, optional>",
  "userKeyPassword": "<key password = bcrypt-derived mailbox password>",
  "session": { "uid": "...", "accessToken": "...", "refreshToken": "..." },
  "telemetryEnabled": true
}
```

- `userKeyPassword` is the decrypted key password delivered by the browser fork payload
  (`incubating/account/js/src/authWeb.ts`). It unlocks the user keys. In plaintext, which
  is why the README says "do not use, for testing only".
- `cachePassword` encrypts the local sqlite crypto cache; loss means the cache is rebuilt.
- Access token lifetime is not stored. The OpenAPI type for `/auth/v4/refresh` shows
  `ExpiresIn` deprecated with example values 86400 and 360000. A nightly-backup user saw the
  access token expire mid-run (#43, #44). Exact lifetime: unverified.

### Refresh rotation and stale tokens

`incubating/account/js/src/apiClient.ts`:

- Refresh is triggered only on HTTP 401 for non-auth URLs. No proactive refresh.
- POST `/auth/v4/refresh` with `{ResponseType:'token', GrantType:'refresh_token', RefreshToken}`.
- On 200: stores `data.RefreshToken ?? refreshToken`, so the token rotates when the server
  returns a new one, and the file is rewritten immediately.
- On any 4xx except 429: `credentials.signOut()` wipes memory and deletes
  `auth-session.json`. On 429/5xx/network: returns false, session kept.
- Since v0.5.0 the hook dedupes concurrent refreshes in-process (fixes #43/#44).

Does a stale-but-unused refresh token stay valid across days? Proton: "unless you
explicitly log out of a session, all sessions expire after 60 days of inactivity"
(https://proton.me/support/log-out-all-other-sessions). No official statement on whether
the previous refresh token is invalidated the moment a new one is issued. Evidence it is
single-use: `Invalid refresh token` (code 10013, HTTP 422) is what reuse produces; #43's
author lost the session roughly every 2 days when overlapping 401s re-refreshed with an
already-rotated token. Treat the old token as dead after rotation. [inferred]

The session appears in account.proton.me > Security > Session management with a Revoke
button. The CLI forks with `app=drive` and client id `cli-drive`; the label it shows there
is unverified.

### Restore / write-back from CI: feasible, with specific failure modes

- `auth login` needs no browser on the host: it prints the sign-in URL and polls
  `/auth/v4/sessions/forks/<selector>` every 5 s for up to 10 min. Maintainer (horejsek,
  2026-06-26, #39): "If you don't have browser on the system, you can type the URL on
  different device".
- `unsafe_file` never touches `Bun.secrets`/libsecret/D-Bus (`credentials/index.ts`).
- Also persist `clientUid.json` (per-machine client UID); dropping it generates a new UUID
  each run. `events.json` is optional.

| Scenario | What happens | Mitigation |
|---|---|---|
| Run refreshes, then dies before write-back | Stored blob has the old refresh token; next run's first 401 triggers refresh -> 4xx -> `signOut()` deletes the file -> "You need to login first". Manual re-login. | Write the session back immediately after every CLI step, not only at job end; run a cheap authenticated call at job start so rotation happens early. Keep the file in durable object storage, not a 48 KB Actions secret. |
| Two runs overlap | Both hold the same refresh token; the second to refresh gets 4xx and wipes its copy. `events.lock` denies event subscriptions to the second process. | `concurrency: { group, cancel-in-progress: false }`. Never overlap. |
| Refresh returns 429 | Session kept; CLI exits non-zero. | Retry later; not a re-login. |
| Account-side revoke or 60 idle days | Refresh 4xx -> session wiped. | Nightly cadence keeps it warm; alert on "login first". |
| Access token expiry mid-run | Transparent refresh + retry of the single failed request. | Write-back after. |

Not verified: whether Proton caps concurrent sessions per account.

### Upload / mirror semantics

`cli/src/commands/fileSystem/commandFileSystemUpload.ts`:

- Per file: computes local SHA-1 first (full read every run, by design, #51), then
  uploads. A name clash surfaces as `NodeWithSameNameExistsValidationError`; the CLI fetches
  the existing node and auto-skips if `activeRevision.claimedDigests.sha1 === expectedSha1`,
  regardless of strategy. Otherwise `-f create-new-revision` uploads a new revision,
  `-f replace` trashes and re-uploads, `-f skip` skips. `-d merge` reuses an existing folder.
- mtime: `modificationTime: new Date(file.lastModified)` is written into extended
  attributes (`client/js/src/internal/nodes/extendedAttributes.ts`) and comes back as
  `activeRevision.claimedModificationTime`. Local mtime is preserved as the claimed
  modification time. Folder creation does not set one.
- Concurrency: 5 files at a time (`transferQueue.ts`), 5 blocks of 4 MiB in flight per
  file (`streamUploader.ts`).
- Exit code 1 if any item failed. #55: rc=0 with files missing after interrupted runs;
  verify with a post-run listing.
- #68: `EMFILE` on a 22k-file folder upload (Windows). Raise `ulimit -n` on the runner.

Trash by list: `filesystem trash <path...>` takes many paths in one invocation
(`commandFileSystemTrash.ts`; all in the same section). Path segments may be node UIDs
instead of names (`cli/src/cli/paths.ts` `isNodeUid`), so
`filesystem trash /my-files/<uid1> /my-files/<uid2> ...` works from a listing without
name-walking. `filesystem delete` only accepts `/trash/<name>`; `empty-trash` exists.

`filesystem list --json`: a streaming JSON array of full `NodeEntity` objects: `uid`,
`parentUid`, `name` as `{ok, value}`, `type`, `mediaType`, `creationTime`,
`modificationTime`, `totalStorageSize`, `activeRevision: { uid, claimedSize,
claimedModificationTime, claimedDigests: { sha1, sha1Verified } }`. Enough to diff against
a state file without downloading. One folder level per call; since js/v0.21.0 (2026-08-11)
children are always fetched fresh (fix for #61).

Fair-use fit: upload with merge does not list children; conflicts are detected server-side
per file. A deletion pass walks the remote tree once per night. Proton's rule is "Do not
poll the API or perform frequent recursive traversals"; one nightly walk is not frequent.
The CLI sets `x-pm-appversion: cli-drive@0.8.0`, subscribes to events, and is Proton's own
client.

### Throughput and throttling

- No published MB/s figures. ~25 concurrent 4 MiB block PUTs implied; small files are
  dominated by per-file round trips (create link + revision + commit, 5 in flight).
- SDK rate limiting (`client/js/src/internal/apiService/apiService.ts`): on 429 retry after
  `retry-after` (default 10 s); after 50 consecutive 429s stop for 60 s. 5xx: retry once
  after 1 s, cool down 60 s after 10 in a row.
- Policy (https://proton.me/business/drive/cli ; https://proton.me/blog/proton-drive-cli):
  "Automation scripts should only upload or download files that have actually changed...
  Accounts that generate unusually high traffic are temporarily throttled." A nightly
  1-20 GB / few-thousand-file delta with SHA-1 skipping is the compliant pattern.
- Proton can block a specific `x-pm-appversion` if it ships a serious bug. Pin the binary.

### Naming and size limits

- Client-side: name non-empty and <= 255 chars (`client/js/src/internal/nodes/validations.ts`).
- https://proton.me/support/drive-filenames : 1-255 chars; no leading/trailing space or
  trailing period; `.` and `..` rejected; RTL-override / invisible characters rejected;
  case-sensitive; uniqueness per folder across files and folders. The CLI escapes a literal
  `/` as `\/`. No Unicode normalization anywhere in the SDK (grep for `normalize(` finds
  nothing): NFD names stay NFD; normalize before comparing state.
- Files per folder: community ~30,000; not on an official page.
- Max file size: only a free-tier error code exists (`MAX_FILE_SIZE_FOR_FREE_USER = 200003`).

### Running on `ubuntu-latest`

- Builds: `linux-x64`, `linux-x64-baseline`, `linux-x64-musl`, `linux-arm64*` with SHA-512
  sums at https://proton.me/download/drive/cli/index.html . Hosted x64 runners have AVX2.
- Standalone Bun-embedded binary; with `unsafe_file` nothing links to libsecret or D-Bus.
- Telemetry: `initSentry` runs at start with the DSN baked in; `init.ts` then calls
  `disableSentry()` unless stored `telemetryEnabled` is true, mirroring account Settings >
  Telemetry (#52). Turn telemetry off in account settings before the laptop login so the
  flag is baked into the session file. No env var; building from source with `SENTRY_DSN`
  unset removes it.
- Set `PROTON_DRIVE_LOG_LEVEL=INFO` (default DEBUG), `--skip-thumbnails`, `--json`.

### Recommendation

Use path A. It is the only Proton-endorsed client that runs unattended, and the
unsafe_file restore/write-back scheme works with the code as shipped:

1. One-time: on a laptop, `PROTON_DRIVE_CACHE_DIR=./pd PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file
   proton-drive auth login`, telemetry off in account settings. Encrypt
   `pd/auth-session.json` + `pd/clientUid.json` (age/sops) into an R2/S3 object (R2 has no
   object versioning; keep dated copies of anything that must roll back).
2. Per run: `concurrency` group; restore the dir; a cheap `filesystem list /my-files --json`
   first (forces any pending refresh early); after each CLI invocation re-encrypt and
   upload the session file; alert on exit 1 containing "login first".
3. Mirror logic: state file of `path -> {sha1, size, claimedModificationTime, uid}`; upload
   batches with `-f create-new-revision -d merge -t --json`; trash removed files by UID in
   one call per batch; `delete` from `/trash` only after a retention window.
4. Residual risk: the key password sits in plaintext inside the encrypted blob, and a crash
   between refresh and write-back costs a manual re-login.

Unverified, to test on the first runs: refresh-token single-use behavior, actual
access-token lifetime, whether Proton challenges GitHub's egress IPs on `/auth/v4/refresh`.
