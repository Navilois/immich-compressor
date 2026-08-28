# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **An unusable `log_level` is refused when the settings load, instead of half-applied.** The
  setting was a bare string with two consumers that disagreed about what it may say:
  `_configure_logging` resolves a name it does not know to `INFO`, while `serve` hands the same
  value to uvicorn, which raises on anything outside its own set. A typo therefore ran every
  command at `INFO` with nothing anywhere to say so, and killed `serve` from inside uvicorn —
  after the app was built and hardware detection had already logged, a long way past the point
  where the rest of the configuration is checked. `TRACE` was the worse case, because it looked
  like it worked: uvicorn has that level and `logging` does not, so the service came up with
  uvicorn one step below `DEBUG` and everything this project logs left at `INFO`. The accepted
  set is now `DEBUG`, `INFO`, `WARNING`, `ERROR` and `CRITICAL` — the five both consumers
  understand — checked in the same place and the same way as `delete_mode` and `metadata_verify`.
  Case stays irrelevant, as it has always been: the value is upper-cased before it is matched, so
  `log_level: debug` keeps working. `config.schema.json` carries the five names now, so an editor
  on the modeline completes them and marks a typo as it is typed; it lists the upper-case
  spellings only, so it will also mark a lower-case one the service itself accepts.

- **A `duplicate` upload now records the replacement's checksum, not only its id.** When
  Immich answers `duplicate` to the compressed upload, the row left behind already carries
  `source_checksum`, `owner_id` and `new_asset_id`, which is the whole of the ledger
  predicate — so the shim counts it as a full ledger entry. Only half of one worked. The
  `sync_rewrite` map, which hands the original's checksum to the replacement's sync line,
  was built as always; `upload_check`, which restates a device's "do you already have these
  hashes?" in terms of the replacement, is built from `new_checksum` alone and that column
  was never written on this path. `upload_check` is the fallback for the window after the
  client's mirror has seen the original's delete and before it has received the re-offered
  replacement row, and the re-arm is two-pass by construction, so that window always exists.
  Inside it the question went untranslated and Immich answered *accept*. Measured on a live
  deployment (Immich v3, compressor 1.4.0) on 2026-08-28: 1,257 rows — every `duplicate`
  skip on it, all from one re-upload wave the day before — were ledger entries with no
  `new_checksum`, and backfilling them by hand took `upload_check` from 7,952 entries to
  9,209, equal to the ledger-entry count. The value is read back from `GET /assets/{id}`
  rather than taken from the file the run just encoded: whether Immich's duplicate detection
  makes those two necessarily equal is not something this service has verified, and a
  checksum the server does not hold would make `upload_check` restate the device's question
  as a hash nothing answers for. A read-back that fails leaves the column NULL and says so
  in the log — the same shape this path had before — and the skip itself is unaffected. Rows
  already written are not repaired; `docs/upgrading.md` has the count and the backfill.

- **The shim no longer hands a checksum to a replacement while a re-upload that arrived
  minutes ago still holds it.** 1.4.0 stopped translating once the pipeline had recognised
  a returned original, but the recognition is a job row and the job is `queued` — carrying
  no checksum at all — from the moment Immich answers 201 until a worker reaches it. A
  device syncing inside that window got two rows claiming one `(owner, checksum)` and its
  batch aborted with `SqliteException(2067)` on `updateAssetsV2`, which stalls the client's
  checkpoint and makes Immich re-send the same batch. Seen on a device on 2026-08-28 with
  23 jobs of a re-upload burst still queued. The shim now reads the claim off the sync
  stream itself: a line carrying a checksum it is armed to hand to a different asset means
  the checksum is taken, and the translation stands down from that line onwards and for
  every later request. It suppresses forwards only, so a claim arriving behind the line it
  should have stopped still costs that one batch — but the re-send is then served from maps
  that know, so the client recovers instead of looping. Nothing is written to the job store
  and `_check_re_upload` is untouched.

- **`report` now says how many failed jobs it did not print.** The list stopped at twenty
  and said nothing about it, so a deployment with fifty failures showed twenty lines that
  read like all of them. The count in the header was right the whole time, which is what
  made the silence convincing. There is a `... and 30 more` under the list now, the same
  line `requeue` and `backfill run` have always printed.

- **`immich.connect_timeout_s` now applies to the commands too, not only to the running
  service.** `check`, `backfill scan`, `backfill run` and `restore` each built their Immich
  client by hand and left the setting off, so a deployment that raised it for a slow or
  distant server still got the ten-second default from all four — with nothing to say so.
  There is one factory now, and every caller goes through it.

### Documentation

- **The README leads with the production run and covers the shim.** It opened on
  `Is this safe?` and carried a five-job sample report, so a visitor met the risk before the
  result and never saw a number worth the install. It now opens on the measured backfill —
  22,586 assets, 118.66 GiB reclaimed from a 259 GB library, disk 95 % to 73 % used — with the
  per-lane table, the refusals, what the run cost and the read-only audit two days later that
  found no byte-identical pair in 53,855 assets. The safety chain keeps every claim it had and
  moves below the quickstart, which is where it answers a question the reader is now asking.

  Four things the code has done since 1.3.0 were missing from it entirely: the
  [shim](docs/shim.md), which had no mention on the page or in the documentation table, the
  `re_uploaded` recognition in front of it, `transcode_unsupported_audio`, and the surge
  breaker. The re-upload problem is the objection this project gets asked about most, so it is
  now its own section with both answers in it. No claim was carried over unverified: the
  numbers come from the job store's own byte records and the live-API checks recorded with
  them, and the skip counts are given as an ordering rather than a partition because the
  per-reason figures and the per-lane totals in that record do not reconcile.

- **`LOG_LEVEL` is documented.** It is the environment override for `log_level` like any
  other setting, but `serve` also reads it *before* it loads the settings, so it is the only
  way to raise or silence the startup lines that explain the encoder decision — a
  `log_level` in `config.yaml` arrives after detection has already logged. `docs/operations.md`
  says so under [Logs](docs/operations.md#logs) with a compose snippet, and notes that `.env`
  cannot carry it because `docker-compose.yaml` passes through only the variables it names.
  Both were checked against `docker compose config`.

- **`source_quality` skips a still that is strictly *below* `min_source_quality`, not one
  at it.** `pipeline.py` compares `quality < min_source_quality`, so a source sitting
  exactly on the threshold is encoded. `docs/safety.md`, `docs/troubleshooting.md` and
  `docs/architecture.md` each said "at or below", which is an off-by-one at the boundary
  the setting exists to define. The `Preset.min_source_quality` comment said it too. The
  pages that describe the guard against the preset's *quality target* rather than the
  threshold were right as they stood and are unchanged.

- **The `/metrics` sample in `docs/operations.md` shows the six `shim_*` counters.** They
  are emitted unconditionally, so every scrape since 1.4.0 has carried them, and a page
  that promises "every family is emitted even when empty" was listing a scrape that
  predates them — its `build_info` still read 1.3.1. The sample now matches what the
  renderer produces, and a line under it says the counters read zero with the shim off and
  points at their table on `docs/shim.md`.

- **The module table in `docs/architecture.md` follows the package again.** It still had
  the endpoints in `server.py`, which they left in this cycle, and it had never listed
  `ingest.py`, `routes.py` or `shim.py`.

- **The first sync batch after a duplicate cleanup is now measured rather than open.**
  `docs/upgrading.md` said whether it applies cleanly "has not been established", because
  the delete for the duplicate and the upsert for the replacement can land together and
  their order within a batch had not been checked. Measured on 2026-08-28 against a live
  v3.1.0 instance and the Android app: 69 duplicates removed permanently, the next
  pass delivered all 69 deletes in one 10,955-byte response, the device acked it, and the
  shim re-armed all 69 translations inside 1.5 seconds — no `updateAssetsV2` failure and
  no retry. `shim_gates_opened_total` stayed put and `shim_touches_total` rose by exactly
  69, both as the fix intends.

  The same run turned up the thing that actually bites, which the page now says outright:
  the cleanup opens a re-upload window. The re-arm happens when the shim sees the delete, so
  the corrected replacement is only re-offered on a later pass — here 57 seconds later — and
  a backup scan landing in that gap uploads the files just removed. It is bounded and
  self-correcting, but the page now says to run the removal with device backup off and turn
  it back on after a sync has carried the translations.

## [1.4.0] - 2026-08-27

### Added

- **A checksum-translation shim that stops the re-upload instead of only recognising it.**
  Off by default (`shim.enabled: false`), and inert until a reverse proxy routes
  `POST /api/sync/stream` and `POST /api/assets/bulk-upload-check` to this service.

  The Immich app decides what to back up entirely offline, by joining the SHA-1 of each
  local file against the assets it has mirrored from the server. Once the original is
  permanently deleted its checksum exists nowhere, so the device uploads the file again.
  The shim substitutes the **original's** checksum into the replacement's line in the sync
  stream, so the device finds a match and never queues the file. Nothing in Immich is
  altered; the substitution happens in two responses on their way to a client.

  It is gated, and the gate is the whole design. The app's mirror enforces one row per
  `(owner, checksum)`, so the replacement may only be given that checksum after the
  original has really stopped existing — earlier, the write would either drop the
  original's mirror row or abort the client's sync batch. A new `original_freed_at` column
  on `jobs` records that moment: set by the pipeline right after a `permanent` delete,
  which this service performs itself, and by the shim when it sees the purge of a trashed
  original go past on the sync stream, which happens inside Immich up to 30 days later and
  is never reported here.

  Opening a gate also makes one no-op update to the replacement — it writes back the
  `isFavorite` value it just read — because the sync stream only re-offers assets that have
  changed, and without it the translation would be armed but never sent. Six counters
  (`shim_requests`, `shim_lines_rewritten`, `shim_hashes_translated`, `shim_gates_opened`,
  `shim_touches`, `shim_passthrough_errors`) are exposed at `/metrics`, and
  [docs/shim.md](docs/shim.md) covers the deployment, the staged rollout and the limits —
  including that this is, deliberately, telling one client something untrue.

  `shim_gates_opened` and `shim_touches` count the event and not the code path that saw
  it, so both routes to an open gate bump both: neither counter is structurally zero on a
  given `delete_mode`. `shim_gates_opened` follows the record, so it counts on a
  `permanent` deployment even with the shim switched off; `shim_touches` follows the write,
  so it counts only where the no-op update is actually made. One original cannot be counted
  twice — a deployment that switched `delete_mode` can have both routes observe the same
  purge, and the gate behind them is first-write-wins.


- **A re-uploaded original is recognised instead of compressed a second time.** Every job
  now records the checksum and owner id the server reported for the original, before
  anything mutating happens. An asset that later arrives carrying the checksum of an
  original this service has already replaced is skipped as the new `re_uploaded` reason,
  naming the earlier asset, its replacement and the date in the log. Nothing is downloaded,
  encoded, uploaded or deleted — this recognises the situation, it does not act on it.

  The situation it recognises: the Immich mobile app decides what to back up by joining the
  checksums of the files on the device against the assets it has mirrored from the server
  (`BackupRepository.getCandidates`, verified against `immich-app/immich@fbd5dc2`). A
  deleted asset leaves no row in that mirror, so a device that still holds the file uploads
  it again — as a new asset, with a new id and no compressor marker, which is why the
  existing loop guard cannot see it.

  Two columns, `source_checksum` and `owner_id`, are added to the `jobs` table and applied
  automatically on open. They are empty for jobs that ran before this release and cannot be
  backfilled: the original they would describe is already gone. Recognition is therefore
  complete only from this version onwards.

- **A test that reproduces the Immich app's local mirror and replays the shim's output
  through it.** `tests/test_app_mirror.py` builds `remote_asset_entity` from the app's own
  schema — `STRICT`, `WITHOUT ROWID`, and the partial `UNIQUE (owner_id, checksum)` index —
  and drives it with the same upsert the app performs, so the constraint the whole gate
  exists to respect is enforced in CI rather than argued about. It needs no live Immich and
  no phone.

  Its negative control forces the ungated rewrite and pins down both failure modes, which
  turn out to depend on whether the phone has already mirrored the replacement: with the row
  present the unique index aborts the batch and drift's `rethrow` carries the failure into
  the client's sync, taking healthy lines in the same batch with it; with the row absent,
  `INSERT OR REPLACE` resolves the conflict by deleting the original's row instead,
  silently. Removing the gate turns four of these tests red.

- **`/metrics` says when the surge breaker has latched.** The new
  `immich_compressor_paused` gauge is 1 while the service is paused and 0 otherwise.
  `/healthz` and `/stats` have always reported the latch, and neither is what a homelab
  alerts on: measured on a live deployment on 2026-08-25, the breaker latched at 06:07 UTC
  and the stop was noticed six hours later, with 13,134 jobs waiting behind it. Nothing else
  about a paused service looks wrong — the container is healthy, the log is quiet, and the
  queue simply stops moving.

  ```yaml
  - alert: ImmichCompressorPaused
    expr: immich_compressor_paused == 1
    for: 10m
  ```

  The reason is deliberately not a label: it is free text with counts in it, and as a label
  that is unbounded cardinality. It stays in `/healthz`, in `resume` and in the log.

- **`requeue --failed` brings a batch of failed jobs back.** Until now `requeue` covered only
  the `skipped` state, and the other terminal one had no bulk route at all: a failed job has
  spent its attempts, so the worker's own backoff never returns to it, and every gate fix was
  recovered one `reprocess <asset_id>` call at a time. `--error-contains TEXT` narrows the set
  to the jobs whose recorded error contains that text, which is how a fix to one gate is
  applied to exactly the jobs that gate rejected:

  ```bash
  immich-compressor requeue --failed --error-contains ShutterSpeedValue           # look first
  immich-compressor requeue --failed --error-contains ShutterSpeedValue --apply
  ```

  Dry until `--apply`, like every other command that changes something. The match is a plain
  substring of `last_error`, not a pattern — ffmpeg and exiftool both put `%` and `_` in their
  messages. `--failed` and `--reason` select the two states and are refused together, and
  `requeue` on its own still means exactly what it did before.

- **`behavior.transcode_unsupported_audio` re-encodes audio the container will not carry,
  instead of failing the job.** The video presets copy the audio stream — the point of the
  exercise is the video, and a copy is free and lossless — and MP4 has no mapping for some of
  what an old camera or a DVD rip produces. Measured on a live library on 2026-08-26, ffmpeg's
  muxer refused **119 of the 172** failures in one backfill run on that alone: `pcm_u8` (108),
  `amr_nb` (9) and `pcm_dvd` (2).

  ```
  [mp4 @ 0x...] Could not find tag for codec pcm_u8 in stream #1, codec not currently
                supported in container
  ```

  The first attempt is unchanged and still copies. Only a run that failed *with that
  diagnosis* is retried, with the copy replaced by 128 kbit/s AAC — the bitrate the CPU preset
  has always used. Measured in the shipped image on 2026-08-27, the refusal comes from writing
  the container header before a single frame is encoded: on a 68 MB source the whole first
  attempt exited in 601 ms, which is what makes retrying it cheap. There is a per-preset
  override, and setting it on a preset whose command copies no audio is refused at startup.

  **It ships off**, and stays a deliberate decision, because it is the one setting here that
  turns a job which cannot finish into one that deletes an original — and what it deletes is a
  lossless audio stream. Nothing downstream can see that: the sanity gate counts audio
  streams, it does not listen to them. The container log names every file it happens to.

  Not verified: how many of those 119 then pass the sanity and metadata gates. What this
  removes is the muxer's refusal.

### Changed

- **The surge breaker ships off (`behavior.surge_threshold: null`), and its suggested value
  is now 2000 rather than 200.** The breaker counts assets newly queued from webhooks and
  knows nothing else about them, so a first phone backup, a camera card import and a holiday
  upload all look exactly like the influx it exists to stop. 200 in ten minutes is a rate
  that suits the default `enabled_types: [VIDEO]`; with `IMAGE` enabled it is an ordinary
  day, and a backstop that fires on ordinary use teaches its operator to clear it unread.
  Stills and the breaker shipped in the same release, 1.1.1, and the threshold was never
  re-read against them.

  Nothing about the mechanism changed. Writing a number turns it back on and it behaves
  exactly as before: workers stop claiming, the sweeper stops finalising deletes, further
  webhooks are refused, and the latch survives a restart until `immich-compressor resume
  --apply`. `behavior.max_asset_age_hours` — the gate that can actually tell a bulk
  re-trigger from an upload, and the one refused at startup under `delete_mode: permanent` —
  is unchanged and still on by default.

  **A deployment that relied on the 200 default now has no breaker.** Set
  `behavior.surge_threshold` explicitly to keep one; see
  [upgrading.md](docs/upgrading.md). A pause already latched in the database is *not*
  cleared by this change.

- **The generated configuration reference no longer prints a `null` default as "no
  default".** `scripts/gen_docs.py` rendered an em dash for both, so five options that ship
  with a value documented as if they had none — `behavior.surge_threshold` and the four
  `Preset` overrides, whose own text already said `null` inherits the behavior setting.

### Fixed

- **The shim no longer hands out a checksum that a re-upload has brought back to life.**
  Its gate answers "was the original observed to be gone?", once and for ever. The rule the
  app's mirror actually enforces is narrower — one row per `(owner, checksum)` — and an
  original that a device uploads again after the gate opened puts that checksum back on the
  server under a new id. The pipeline recognises the returning file and, by design, leaves
  it alone: `re_uploaded` deletes nothing. So the checksum was live again while the shim
  went on presenting it for the replacement, and a device syncing both met
  `SqliteException(2067): UNIQUE constraint failed: remote_asset_entity.owner_id,
  remote_asset_entity.checksum` — which aborts the whole batch before its checkpoint is
  acked, so the same batch arrives again and the device stops making progress at all.

  The ledger now reads the `re_uploaded` rows alongside the replacements and holds back any
  translation whose checksum another live asset of the same owner is holding. That costs
  nothing while it lasts: the returning copy is itself the match the device is looking for,
  so the local file is not a backup candidate anyway. When that copy is deleted in turn, the
  shim sees the delete on the same stream, records it in the same `original_freed_at`
  column — which has always meant "the asset this row is about is gone" — makes the same
  no-op update, and the translation is armed again.

  `shim_gates_opened_total` deliberately does not move for any of this: no gate opens, so
  counting one would overstate the number of originals this service has seen go. The no-op
  update is counted as `shim_touches_total`, because that is what it is. The
  `bulk-upload-check` direction is not held back either, and that asymmetry is intended —
  it rewrites a question rather than a mirror row, and while the copy is live Immich answers
  `duplicate` to either form of it.

  `tests/test_app_mirror.py` replays this through Immich's own mobile schema: both failure
  orderings — the batch aborted, and the quieter one where `INSERT OR REPLACE` destroys the
  other row without an error — and the suppression that prevents them.

- **The shim no longer relays Immich's `Date`, which arrived as a duplicate.** Uvicorn writes
  its own `Date` and `Server` and appends the application's rather than replacing them, so
  every proxied response carried two `Date` headers whose values disagreed by a second —
  a field RFC 9110 defines as a singleton — and nginx logged `upstream sent duplicate header
  line: "date: ..."` on each one. Both are now dropped from the relayed set along with the
  hop-by-hop headers. `Server` is latent rather than observed, since Immich sends
  `X-Powered-By` and no `Server`, but anything in front of Immich would duplicate the same
  way. Everything that describes the response rather than the hop — `X-Correlation-ID`,
  `Vary`, `Content-Type` — still comes through untouched.

- **The live test behind the shim's delivery claim never actually ran.** Immich refuses API
  keys on every `/sync` route — `403 {"message": "Sync endpoints cannot be used with API
  keys"}`, whatever the key is scoped to — so
  `test_live_touch_makes_the_sync_stream_reoffer_an_asset` hit the 403, called
  `pytest.skip`, and reported green without executing a single assertion. It now logs in
  with `E2E_IMMICH_EMAIL` / `E2E_IMMICH_PASSWORD` and passes against a live v3.1.0: the
  no-op `isFavorite` write-back does re-offer the asset on the next sync pass, and with the
  touch removed the pass comes back empty, so the control discriminates. The shim itself
  was never affected — on its proxied routes it relays the caller's own credentials and
  never substitutes this service's key. See
  [#17](docs/immich-api-notes.md).

- **The live suite's sync helper acked one line per response, which drained nothing.** Every
  response ends with `SyncCompleteV1`, whose ack advances no asset checkpoint; six
  consecutive passes acking only the last line returned the identical nine lines. It now
  acks the last line of each type, the way a real client does, and drains the same backlog
  in one pass. `make test-live` also gained `-rs`, so a skip is visible rather than
  counted as green. See [#18](docs/immich-api-notes.md).

- **The gate the checksum-translation shim waits on now really opens after a `permanent`
  delete.** With `delete_mode: permanent` and `retention_days: 0` the pipeline deletes the
  original inline and recorded nothing: `original_freed_at` stayed empty on every job it
  finished, so the shim never translated a checksum on the one configuration it exists for.
  The finaliser read `source_checksum` and `owner_id` off the in-memory job object, which is
  claimed *before* those two columns are written to its row, so on that path it always found
  them empty and returned before opening the gate. It reads the row back from the store
  instead — the same source of truth the sweeper's freshly loaded job was already using, so
  `retention_days > 0` and the shim's own `trash`-mode path behave exactly as before.
  Measured on a live deployment on 2026-08-26: 370 permanently deleted originals, 370 ledger
  rows written, 0 gates open. A deployment that ran the affected build can recover those
  timestamps by hand — [docs/upgrading.md](docs/upgrading.md) has the statement, and the one
  configuration it must never be run on.

- **`docs/faq.md` no longer claims that `delete_mode: trash` avoids the re-upload.** It
  delays it. Immich's own trash retention defaults to 30 days (`trash: { enabled: true,
  days: 30 }`, verified against `immich-app/immich@fbd5dc2`); when the scheduled purge
  hard-deletes the original its checksum stops being known and the re-upload becomes
  possible, exactly as it would after a `force` delete. The answer now describes the
  mechanism, the 30-day reprieve and what `re_uploaded` does about it.

- **The metadata gate no longer fails a job on floating-point re-approximation.** Values
  that are numbers on both sides — with an identical unit, if any — are now compared with a
  relative tolerance of 1e-6 instead of character by character. Copying an EXIF rational
  re-approximates the fraction, and for tags that exiftool prints as a raw decimal that drift
  reached the printed value the gate compares. Measured on a live library on 2026-08-24, a
  backfill batch of the 150 largest JPEGs failed 24 of the 67 images that produced an encode,
  every one of them on `EXIF:FocalPlaneYResolution` moving `6734.006734` -> `6734.006711` — a
  difference in the 8th significant digit. Nothing was lost in any of them; a failed job
  leaves the original untouched. A tag that is lost, a value that really changed, and a unit
  that changed (`339.569 m` against `339.569 ft`) are all still reported, and non-numeric
  values still compare exactly.

- **The metadata gate no longer fails a job on a time that gained an explicit `+00:00`.**
  exiftool writes a zero UTC offset onto a time that carried none, and the gate compared the
  two spellings character by character. Measured on a live instance on 2026-08-26,
  `IPTC:TimeCreated` and `IPTC:DigitalCreationTime` came back `'11:24:38'` ->
  `'11:24:38+00:00'` and failed **92 jobs** in a single backfill run, first seen
  2026-08-25T05:18:26Z. Same clock, same displayed value, nothing lost. A time and a
  date-time are now compared as a clock plus an offset, where no offset and a zero offset
  mean the same thing. A **non-zero** offset still has to agree — `'15:46:30'` against
  `'15:46:30+01:00'` and `'+01:00'` against `'+02:00'` are both still findings — and so does
  the clock itself.

- **The metadata gate no longer fails a job on a fraction that was re-approximated.** A value
  that is one whole fraction is now evaluated instead of being read as the number in front of
  the slash: `1/100` was a 1 carrying the unit `/100`, so two fractions were compared
  character by character and the drift the tolerance exists for never reached them. Measured
  on a live library on 2026-08-26, `EXIF:ShutterSpeedValue` came back `'1/999963365'` ->
  `'1/999963296'` and failed **6 jobs** in the same backfill run. Evaluated, the two differ by
  6.9e-8 — comfortably inside the tolerance that was already in the code.

  This cannot loosen the gate on an exposure time. Two *integer* denominators only land within
  1e-6 of each other once the denominator passes a million, so `1/8000` against `1/7999` is
  still a finding, and so is `1/100` against `1/101`. A fraction with anything appended to it
  is not treated as one at all: `4/2/2026` and `2/1/2026` are two dates in a caption, and they
  still compare exactly.

- **`XMP:Orientation` joined `EXIF:Orientation` on the metadata gate's ignore list.** It is
  the XMP mirror of the same tag, describing the same rotation of the same pixels, and
  `normalize_orientation` pins that rotation to 1 after `-auto-orient` has baked it into the
  picture — the reason `EXIF:Orientation` has been ignored since the gate existed. It was
  simply not listed. Measured on a live instance on 2026-08-26: `'Rotate 270 CW'` ->
  `'Horizontal (normal)'` on 2 jobs.

### Documentation

- **The shim's `proxy_buffering` warning now says what was measured.** `docs/shim.md`
  claimed that with buffering on "nginx holds the sync stream and the app stalls waiting for
  a response that never finishes arriving". It does not: measured on v3.1.0 against a
  423-line, 265 KB response, buffering on and off were indistinguishable — first byte inside
  20 ms either way, byte-identical output, no stall — with a fast client and again with one
  reading at roughly 40 KB/s. Immich's sync stream is a finite response that completes and
  closes, so there is nothing to hold open. The recommendation stands, for the reason that
  actually applies: it is a streaming endpoint, the shim yields line by line, and the client
  applies batches as they arrive. The large-library case is now named as unverified rather
  than asserted.

- **Note 17 no longer claims owner resolution works for every credential.** It said
  `GET /users/me` "answers 200 for an API key, so it works whichever credential the caller
  presents" — measured, but only with the key holding `all` that the note's table used.
  Note 13 in the same document already recorded the other half: a key scoped to just the
  permissions this service needs gets **403 Missing required permission: user.read**.
  Measured again on 2026-08-27 against a live instance, this time through the shim: with
  such a key the owner comes back unresolved, `translate_upload_check` translates nothing,
  and a replaced original's checksum returns `accept` where its replacement earns
  `reject`/`duplicate`. The upload-check translation is therefore a silent no-op for a
  narrowly scoped key. Phones are unaffected — they present a session token, and the app
  does not use that route — but `immich-go` and the CLI are, so `docs/shim.md` now lists it
  under Limits and says to grant `user.read`.

- **Every tracked document was read against the 1.3.1 source and corrected.** No behaviour
  changed; what changed is that the documentation now describes what the code does.
  - `docs/upgrading.md` gains the **1.3.0 → 1.3.1** section it never got — the release was a
    change to what `restore` prints, and version-to-version notes with a gap in them are
    read as "nothing happened".
  - The `/metrics` sample in [docs/operations.md](docs/operations.md) advertised
    `build_info{version="1.2.0"}` and listed three of the five `session_*` counters.
  - `SECURITY.md` still supported **1.1.x**, and named `/webhook` and `/reprocess` as the
    token-protected routes — `POST /resume`, which re-arms a service that deletes originals,
    has needed the shared secret since 1.1.1 and was in neither list.
  - The sanity gate was described as eight checks in the README and the FAQ. It is nine:
    both lists left out `min_savings_bytes`, which is the one that decides whether a still
    was worth re-encoding at all.
  - [docs/architecture.md](docs/architecture.md) drew the endpoint map without `POST
    /resume` or `PUT /webhook`, left `metrics.py` out of the module table, and pointed the
    backfill's search-parameter note at finding 15 — the endpoint the scan does *not* use —
    where finding 16 has measured the one it does since 1.3.0.
  - [docs/safety.md](docs/safety.md) still called the `restore --all-pending` output
    unmeasured. The fixed command has run against a live stage-4 deployment since, on
    2026-08-23, and reported `restored 4 asset(s) from the trash`.
  - The FAQ counted "eleven documented ways" this project has been wrong. Sixteen findings
    are written down, and the FAQ now links to them.
  - `serve` was missing from the command table, the two inventory-only backfill verdicts
    (`missing`, `already_known`) were named nowhere, and `docs/hardware.md` introduced four
    preset settings as three.
  - **Release documentation follows the workflow, not the tag.** `CONTRIBUTING.md` and
    [the launch checklist](docs/maintainers/launch-checklist.md) led with the hand-rolled
    `git tag`, which has been the escape hatch since 1.3.0 shipped **Prepare a release**.
    Section 1 of that checklist is also done: description, homepage, topics, Discussions
    and private vulnerability reporting were all confirmed set on 2026-08-24, and the
    checklist now carries the commands that read them back.

## [1.3.1] - 2026-08-23

### Fixed

- **`restore` no longer warns that it cannot do the thing it is doing.** Under
  `delete_mode: permanent` the command opened with *"originals removed by this service were
  not trashed and cannot be restored"* — printed before a single id had been tried, gated on
  the mode the deployment is in *now* rather than the one that removed anything, and printed
  by runs that then restored every original they were given. On a live stage-4 deployment
  that meant a successful rollback announced itself as a refusal. The accurate version is
  still there and unchanged: after the request, `restore` names how many ids the server no
  longer has and why, from the server's own answer.

### Documentation

- **The `force: true` delete was re-verified against a live library**, on a real stage-4 run
  rather than a throwaway asset. All three recorded consequences hold — HTTP 400 afterwards,
  absent from the trash view, file unlinked from the upload directory — and two details are
  now sharper in [docs/immich-api-notes.md](docs/immich-api-notes.md): the row leaves the
  `asset` table entirely rather than being flagged, with no orphan left in `asset_exif`,
  `asset_file` or `album_asset`; and the message is `Not found or no asset.read access`, the
  same permission-shaped wording `/trash/restore/assets` answers with, not the bare
  `Not found` recorded before.

## [1.3.0] - 2026-08-23

### Added

- **`backfill` works from an inventory.** `backfill scan` walks the library once per enabled
  asset type, runs the *worker's own guards* over every asset it sees, and writes the verdict
  into a new `backfill_candidates` table. `backfill run` queues candidates out of that table,
  biggest first. `backfill status` says how many are waiting, how big they are, and why the
  rest were refused. `backfill --type VIDEO --limit 50 --apply` still means exactly what it
  did before — `run` is the default mode, so nothing anybody has in muscle memory moved.
  - **`--limit` counts queued jobs**, not search results. An asset that was deleted, trashed
    or given a named face between the scan and the run is recorded as such and the run moves
    on to the next candidate, so fifty means fifty.
  - **A second run makes progress.** The old one re-read the same answer from the server and
    spent its limit on assets it had already queued; there is now a cursor, and `status`
    reports what is left.
  - **The scan is resumable.** Every page is committed before the cursor moves, so an
    interrupted walk continues instead of starting over.
  - **One live re-check per asset that is actually about to be queued** — bounded by
    `--limit`, not by the size of the library. It catches the assets the inventory has
    outlived: deleted, in the trash, or newly carrying a named face.
  - **`run` says what would otherwise be invisible an hour later:** that `behavior.dry_run`
    is on and every job it queues will end as `skipped: dry_run` (with the `requeue` command
    that brings them back), and that a latched surge breaker means nothing will be claimed.
- `report` grows one line for the inventory once a scan has run.

### Changed

- **The backfill reads `POST /search/metadata` instead of `POST /search/large-assets`.** The
  old endpoint answers with one fixed set of results — 250 items on the measured library,
  every one of them a video — which makes the stills half of a library unreachable through
  it no matter what the client filters afterwards. **`backfill --type IMAGE` therefore
  returned nothing usable on a video-heavy library**, even after the 1.2.0 fix stopped it
  from queueing videos. The new scanner walks page by page and trusts none of the parameters
  it sends: it filters by type itself and stops when a page repeats the one before it.

### Documentation

- **`POST /search/metadata` is measured, not assumed.** Finding 16 in
  [docs/immich-api-notes.md](docs/immich-api-notes.md) closes the gap finding 15 left open:
  on v3.1.0 this endpoint *does* apply `type`, `size` and `page` — unlike
  `/search/large-assets`, which was re-checked the same day and still ignores both `type` and
  `size`. `size` caps at 1000 and answers HTTP 400 above it, `nextPage` is a string that goes
  null on the last page, `total` counts the page rather than the library, and the order is
  `fileCreatedAt` descending. The scanner's client-side type filter and repeated-page check
  stay exactly where they are; what changes is that the cost of a walk is now known — 55
  requests and 16.5 s for a 53 775-asset library.

### Fixed

- **`restore --all-pending` survives originals that no longer exist.** On any deployment
  that had ever run `delete_mode: permanent` it restored **nothing**: it sent the source id
  of every completed job in a single `POST /trash/restore/assets`, originals removed with
  `force: true` are gone from Immich's database, and one id the server cannot find refuses
  the whole request with `HTTP 400 Not found or no asset.delete access` — including the
  originals that really were sitting in the trash. Measured on a live v3.1.0 instance on
  2026-08-23: of the 50 ids it sent, 46 had been force-deleted by earlier stage-4 runs, and
  the one recoverable original stayed trashed while the command exited 1.
  - The selection now goes out in batches, and a refused batch is halved and re-sent until
    each unknown id stands alone, so **a dead id costs only itself**. Once a batch turns out
    to be mostly missing — what a stage-4 deployment looks like — the rest is sent one id at
    a time rather than halved, which is the cheaper way to isolate them.
  - It reports **the server's own `count`** instead of the number of ids it sent, and names
    how many ids Immich no longer knows.
  - The explanation of why ids go missing no longer depends on `delete_mode` being
    `permanent` *right now*. On the measured deployment the mode was already back to `trash`,
    so that message never printed and the operator got a bare HTTP 400 with no reason for it.
  - **Exit codes**: `0` every id came back, `3` some ids are no longer in Immich's database,
    `2` nothing was selected, `1` the call to Immich failed. `3` is new — a rollback that
    could not roll everything back must not look like a clean success in a script.
- **The backfill asked the server for the wrong size threshold.** It sent
  `behavior.min_savings_bytes` as `minFileSize` while the guard that decides the same
  question uses `preset.effective_min_savings_bytes()`, so a preset with its own override —
  which is exactly what the stills presets have, because video and stills have opposite
  economics — was scanned against a threshold nobody configured. The scan now runs the guard
  itself, and the threshold is per preset by construction.

## [1.2.0] - 2026-08-21

Everything here comes from one first-install audit against a live Immich v3.1.0 and a
259 GB library. The pipeline itself came through it unchanged — encoder detection, the
sanity gate, the metadata chain, the verification chain and all four stages ran on the
first try. What cost the tester most of an hour was everything around them, and that is
what this release is.

### Added

- **Webhook counters.** `webhooks_received` and `webhooks_rejected`, in the first line of
  `report`, in `check`, in `/stats` and in `/metrics`. A shared secret that does not match
  was the one failure in this architecture that left no trace anywhere: Immich discards
  the 401 and logs the workflow as *"executed successfully"*, no job row is written, and
  `check`, `report` and `/healthz` all read exactly like a healthy installation with
  nothing to do. `0 received, 7 rejected` now says it outright, and names the cause. The
  counters live in the database, because `report` runs in a different process from `serve`
  and because restarting the container must not erase the evidence.
- **`immich-compressor jobs`** (`--status`, `--limit`, `--json`). `last_error` had one
  documented route, `curl 'localhost:8080/jobs?status=failed'`, and it works nowhere in a
  default install: no port is published, and the image contains neither curl nor wget.
- **`setup --workflow-key`.** A second API key carrying `workflow.create` and nothing
  else, used for the single `POST /workflows` and never written to any file. Keeping that
  permission out of the long-lived service key is right; it did not follow that the only
  ways left were a full-access browser session token or a 64-character secret typed into a
  web form by hand.
- **`TZ`** is passed to the container by `docker-compose.yaml`, by `quickstart.sh`, and
  written into the `.env` `setup` generates. Immich sets it for its own containers, so
  without this the two services timestamped their logs hours apart — while
  troubleshooting.md asks you to read them side by side.
- **The startup block names the `assetFileFilter` pattern** your workflow has to carry. The
  marker couples three things nobody ever sees together, one of which lives inside Immich
  where nothing here can check it.
- `setup` writes `COMPRESSOR_CPUS` and `COMPRESSOR_MEMORY` into the generated `.env`,
  commented out, with this machine's own numbers — and says which mechanism wins when the
  compose override sets them too.

### Changed

- **The capture-date gate is measured against the source.** A video without a
  `creation_time` could not pass the sanity gate at any quality, which ruled out every
  screen recording, messenger clip, drone export and cut file in a library. The gate exists
  to catch a capture date the *encode* lost, and an output cannot lose what the input never
  had. Sources that carry one are checked exactly as before.
- The rejection warning for a bad shared secret names the length and first characters of
  the token that arrived alongside the one expected — what separates a paste cut short from
  a token left over from an earlier install.
- `--help` has a description written for a terminal. It was the module docstring, printed
  verbatim with RST backticks, listing seven of twelve commands.
- `report` prints `average ratio —` rather than Python's `None`, and `reprocess` on an
  unknown asset names `backfill` as the way in.
- `setup` points out a granted `asset.delete`. The quickstart says to leave it out for the
  first run; granted anyway it printed an `ok` shaped like every other permission, and that
  guarantee disappeared unannounced.

### Fixed

- **`backfill --type IMAGE` queued videos.** `POST /search/large-assets` accepts `type` and
  ignores it — measured: `IMAGE` and `VIDEO` answer with the identical 250 items, all of
  them videos, and `size: 5` answers with 250. The stills backfill was therefore
  unreachable, and anybody who thought they were testing 50 photos re-encoded 50 videos.
  Now filtered client-side, with the discarded results counted out loud.
- **`quickstart.sh` did not forward `IMMICH_API_KEY`.** `setup` refuses without a key and
  tells you to set that exact variable; the script never passed it in, so following the
  advice returned you to the dead end you were already in.
- **The encoder decision was logged before logging existed.** `serve` loaded the settings
  first, and loading them is what runs hardware detection — whose explanation went out
  through `logging.lastResort`, which drops everything below WARNING. Every start threw
  away the lines docs/quickstart.md points at.
- **`.gitignore` covered `.env` and nothing beside it.** `.env.bak` from a `setup --force`,
  `.env.local`, `.env.prod` and any backup taken before an edit were committable, each
  carrying the same API key and webhook token. Now `.env*` with `!.env.example`.
- `hardware` left `THREADS` out of its container calibration command, so `calibrate.sh`
  fell back to 2 and the sweep measured against half the threads the encoder really gets.
- `quickstart.sh` printed a second copy of the three commands `setup` had just printed, so
  a successful install ended looking like an error.
- The compose override template still advised matching the CPU preset's `-threads` to the
  container's `cpus`. The container reads its own cgroup limit; the advice contradicted two
  other files and stood in the one everybody edits.

### Known

- The repository was private when this was tagged, and the documented quickstart begins with
  `git clone` — so the five-minute path was not open to anybody outside it. The published
  **image** was a separate matter and was pullable anonymously throughout.

  **Resolved the same day.** The repository was made public on 2026-08-22, and the
  documented path was then walked from an empty directory on this machine: `git clone` with
  credentials refused, `docker pull ghcr.io/navilois/immich-compressor:1.2.0`, and
  `--version` answering `1.2.0`.

## [1.1.1] - 2026-08-21

### Added

- **A surge breaker** (`behavior.surge_threshold` / `surge_window_seconds`, 200 per 10 min).
  Backstop behind the gate below, for a bulk influx it does not model — an unfamiliar
  trigger, a re-uploaded library, a misdirected workflow. More than the threshold in new
  webhook-queued assets inside the window latches the service paused: workers claim nothing,
  the sweeper finalises no deletes, further webhooks are refused. The latch is stored in the
  database, because restarting the container is the first thing an operator reaches for and
  it must not be the thing that clears a pause. Cleared with `immich-compressor resume
  --apply` or a token-protected `POST /resume`, and visible in `report`, `/healthz` and
  `/stats`. A large phone backup will trip it; the breaker only pauses, and that is the right
  way round for a service that deletes originals.
- **A bulk-trigger gate** (`behavior.max_asset_age_hours`, 24 h by default). Immich's
  `AssetMetadataExtraction` trigger is a maintenance operation: one click on
  **Administration → Jobs → Extract Metadata** re-fires the workflow for every asset in the
  library. Assets already recorded were immune; assets never seen were not, which was the
  whole library until it had been worked through. Every webhook carries `createdAt`, which
  dates the *upload* rather than the exposure, so a re-trigger is now refused at ingest
  while a legitimate import of a thousand old photos still passes — something a rate limit
  could not distinguish. A refusal writes no job, deliberately: `backfill` enqueues through
  the same `ON CONFLICT DO NOTHING`, and a row recorded here would put the asset permanently
  out of its reach. `max_asset_age_hours: null` turns the gate off and is refused at startup
  together with `delete_mode: permanent`.
- **JPEG stills are compressed too.** `enabled_types: [VIDEO, IMAGE]` and `IMAGE` in the
  workflow's type filter are what `setup` now writes. The encoder path already existed;
  what was missing was the decision logic around it.
- **Format allowlist** (`Preset.match.extensions`). Immich files RAW, PNG, GIF, TIFF, WebP
  and HEIC under type `IMAGE` exactly like JPEG, and ImageMagick reads DNG/CR2/CR3/NEF/ARW
  through libraw — without the list a raw file would be developed into an 8-bit JPEG, pass
  every sanity check, and have its original deleted. Anything not on the list is skipped as
  `unsupported_format`, which is deliberately a different reason from `no_preset`.
- **A metadata gate.** After the encode, source and output are compared with
  `exiftool -G -EXIF:all -GPS:all -XMP:all -IPTC:all`; any tag that is missing or changed is
  a finding. `behavior.metadata_verify` decides whether that fails the job (`strict`, the
  default) or only logs (`warn`), and `warn` is refused at startup together with
  `delete_mode: permanent` — a warning cannot undo a force-deleted original.
- **Motion photos are detected and skipped** as `embedded_media`. A Samsung or Google motion
  photo is a JPEG with an MP4 behind the end-of-image marker; a re-encode drops the video
  while every other check reports success. Two independent signals: the XMP markers, and
  payload after the EOI marker found by walking the JPEG's segment structure rather than
  searching for the last `FFD9`.
- `Preset.min_source_quality` — skip a still that is already at or below the preset's own
  quality target, since quantisation error is cumulative and a re-encode usually produces a
  *larger* file (measured 158 368 -> 190 488 bytes for a q60 source through the q82 preset).
  Skipped as `source_quality`.
- Per-preset overrides of `max_ratio`, `min_savings_bytes` and `require_date_time_original`,
  because video and stills have opposite economics.
- **One worker lane per enabled asset type**, backed by a new `asset_type` column on the job
  store (migrated automatically). Without it a single clip with `timeout_s: 7200` holds the
  only worker for two hours while every one-second image job queues up behind it. Rows
  written before the column existed carry `NULL` and stay claimable from every lane.
- `immich-compressor encode` additionally reports `source_quality`, `embedded_media` and
  `metadata_differences`, so every still-specific decision is visible before the pipeline
  makes it — without touching the server.
- `MAGICK_THREAD_LIMIT`, `MAGICK_MEMORY_LIMIT` and `MAGICK_MAP_LIMIT` in the image.
  ImageMagick is built with OpenMP and sizes its thread pool from the host core count,
  ignoring the container's cgroup limit — the same trap the video preset defuses with
  `pools=2 -threads 2`.

### Changed

- **Breaking: `behavior.min_size_bytes` is replaced by `behavior.min_savings_bytes`**
  (default 1 MiB, was 20 MiB). The old threshold guessed from the input size whether the
  work was worth doing; the new one measures whether it *was*. It also serves as the
  pre-download filter, and that half needs no calibration: a file cannot save more bytes
  than it has. A config that still carries the old key is refused at startup with the
  replacement named in the error. See [docs/upgrading.md](docs/upgrading.md).
- The generated stills preset is now
  `magick {input} -auto-orient -quality 82 -interlace Plane {output}`. `magick` because
  `convert` is a deprecated alias in ImageMagick 7; `-interlace Plane` because it is free
  (the same DCT coefficients reordered — `compare -metric AE` returns 0 — for 3-8 % less
  size); and no `-sampling-factor`, because ImageMagick then inherits the source's chroma
  subsampling instead of halving it on every 4:4:4 source, which no sanity check would
  notice.
- `immich-compressor hardware` lists the extensions a preset accepts, and `--json` carries
  them.

### Fixed

- **The metadata gate rejected every geotagged camera JPEG.** EXIF stores rationals, and
  copying a tag re-approximates the fraction, so a carry-over that loses nothing still moves
  the float: measured on a phone JPEG through the shipped preset, `ExposureTime` went
  `2497831/250000000` -> `1/100` and the GPS latitude seconds `16316639/1000000` ->
  `39421/2416`. Both print identically. Values are now compared as exiftool *presents* them,
  and the offset tags (`ThumbnailOffset`, `PreviewImageStart`, `OtherImageStart`,
  `StripOffsets`) are ignored because they are file positions, not content — the matching
  `*Length` tags stay compared, since a thumbnail length that moves is a truncated thumbnail.
- **The `BEHAVIOR__` flags in `.env` reached nothing.** `.env.example` documents four of
  them as the way to go live, but `.env` is compose's substitution file, not an `env_file`:
  `docker-compose.yaml` had no `env_file:` and named only three `${...}` values in its
  `environment:` block, and the service sets `env_file=None` in `config.py` and never had
  `.env` mounted. Measured on a running container: with `BEHAVIOR__DRY_RUN=false` and
  `BEHAVIOR__TRASH_ORIGINAL=true` in `.env`, the container environment held neither, so a
  deployment that went live this way stayed in dry run and said nothing. The compose file
  now lists the four by name, in the list form — a bare name is passed on only when it is
  set, where `BEHAVIOR__DRY_RUN: ${BEHAVIOR__DRY_RUN:-}` would have handed every other
  deployment an empty string to parse. Verified against the 1.1.0 image end to end: unset,
  `config.yaml` still decides and the defaults stay inert; set, the service resolves
  `dry_run=False` and `trash_original=True`. A setting in `docker-compose.override.yaml`
  still wins over `.env`, and a test now holds the two files to the same list of flags.
- **The compose override template broke on its first edit.** It ended in a `{}` that kept
  the file valid while every block in it was a comment — but a flow mapping cannot hold
  block keys, so uncommenting anything made compose stop with a YAML parse error until that
  line was deleted too. It now carries one real setting instead, `restart: unless-stopped`,
  which only restates what `docker-compose.yaml` already sets: the file stays valid and
  inert, and every block can be uncommented on its own. Verified by turning all of them on
  at once against real compose. The second `environment:` block went the same way — a
  service takes one, and uncommenting both produced a duplicate key.
- **`setup` unloaded `docker-compose.override.yaml`.** The `COMPOSE_FILE` line it writes for
  a detected GPU replaces compose's *default* file list, and the override is only ever in
  that default list — so naming an overlay there dropped the override entirely, taking the
  go-live flags (`BEHAVIOR__DRY_RUN`, `BEHAVIOR__TRASH_ORIGINAL`, `BEHAVIOR__DELETE_MODE`),
  the resource limits and any local image pin with it, in exact contradiction of the docs
  telling people to keep all of that there. Measured with `docker compose config`:
  `BEHAVIOR__DRY_RUN` resolved to nothing and the image fell back to
  `ghcr.io/navilois/immich-compressor:1`. The override is now named last on that line, where
  it wins. Compose exits 1 on a file it cannot stat, so `setup` creates the override first —
  copied verbatim from `docker-compose.override.example.yaml`, every block in it still a
  comment — rather than naming a file that is not there yet. Without that it would only have
  helped people who wrote their override before running `setup`, and `docs/safety.md` has
  you write it afterwards, at go-live. An existing override is never touched, `--force` or
  not: regenerating it would put a live deployment back into dry run silently.
- `.dockerignore` matched `__pycache__/` and `*.pyc` at the context root only, so
  `src/immich_compressor/__pycache__/` was copied into the image — 12 stale `.pyc` files on
  a measured rebuild, three of them orphans from a branch that was not even checked out.
  Harmless at runtime, but it made the image depend on which branch was last built.
- **`setup` left the shared webhook token where `git add` could reach it.** When Immich
  refuses the workflow — the API key deliberately lacks `workflow.create` — `setup` writes
  `immich-workflow.json` so it can be posted by hand, and that file carries
  `COMPRESSOR_TOKEN` in clear text as its `headerValue`. `.gitignore` covered `.env` but not
  it, so a fresh checkout offered the live token to `git add` as an untracked file, and it
  was written with plain `write_text`, taking its mode from the umask while the `.env` beside
  it was 0600 — measured at 0644 under the usual umask 022, and 0666 under umask 0. It is
  now gitignored, written through `write_secret_file`, and both `setup` and the docs say to
  delete it once the workflow exists. It has never been committed to this repository.
- **`write_secret_file` set the mode after writing the secret, not before.** The docstring
  promised 0600 "from the start", but the body called `write_text` and only then `chmod`, so
  the file held the secret at whatever the umask allowed for the length of the write — 0666
  under umask 0, measured. It now creates the file with `O_CREAT` at 0600 and chmods while
  the file is still empty, which also covers the one case `O_CREAT`'s mode does not: a file
  that already exists, such as a 0644 `.env` left by an earlier version. Verified at umask
  022, 0 and 077, for a new file and for a rewritten 0644 one — 0600 in every case.

## [1.1.0] - 2026-08-19

The "someone else can install this" release. The pipeline is unchanged; everything
around it — hardware selection, setup, packaging, docs — was rebuilt for people who did
not write it.

### Added

- **Automatic hardware detection** (`hardware.py`). Render nodes are enumerated from
  `/dev/dri`, vendor and device ids are read from sysfs, `ffmpeg -encoders` and `vainfo`
  are asked what they support, and every candidate is confirmed with a real one-frame
  encode before it is chosen. Intel Gen9–11 versus Gen12+ (VAAPI versus QSV) now resolves
  itself instead of being a documentation step.
- `immich-compressor hardware [--json]` — prints the detected devices, the preset chosen
  per asset type, every rejected candidate with the reason it was rejected, the CPU budget
  derived from the container's cgroup, and the YAML to paste if you want to pin the choice.
- **Built-in preset catalog** for `hevc_qsv`, `hevc_vaapi`, `hevc_nvenc`, CPU `libx265`
  and the ImageMagick stills preset. Presets no longer have to be written by hand.
- `hardware.mode` (`auto` | `cpu` | `qsv` | `vaapi` | `nvenc`) and `hardware.render_node`
  for pinning the choice, and `behavior.quality` (`balanced` | `higher` | `smaller`) for
  tuning quality without knowing ffmpeg's per-encoder quality flags.
- **CPU budget from cgroup v2.** `/sys/fs/cgroup/cpu.max` decides the x265 thread pool and
  the worker concurrency, which fixes x265 sizing its pool from the host core count and
  ignoring the container limit.
- `immich-compressor setup [--non-interactive]` — validates the API key against the
  server, names the permissions it is missing, runs hardware detection, writes a tuned
  `config.yaml`, generates a webhook token, writes `.env` with mode 0600, and creates the
  Immich workflow when the credentials allow it (otherwise prints the exact JSON and curl).
- `/metrics` in Prometheus text format: jobs by state, skip reasons, bytes saved, session
  counters and an encode-duration histogram, plus three `config_*` gauges so a deployment
  that quietly went live — or quietly did not — is visible on a dashboard. Hand-rolled;
  no new dependency.
- Published multi-arch image (`linux/amd64`, `linux/arm64`) at
  `ghcr.io/navilois/immich-compressor`, with OCI labels, provenance and an SBOM.
- `docker-compose.build.yaml`, `docker-compose.gpu-nvidia.yaml` and
  `docker-compose.override.example.yaml` overlays; `.env.example`; `scripts/quickstart.sh`.
- `scripts/check-links.py`, which verifies every internal link and heading anchor offline,
  and `scripts/check-language.sh`, an English-only guard — both wired into `make lint` and CI.
- A social preview image in `docs/assets/`, and `docs/maintainers/launch-checklist.md`.
- `docs/` tree: quickstart, installation, configuration (generated from the settings
  model), hardware, workflow setup, safety, operations, troubleshooting, architecture,
  the verified Immich API notes, FAQ, upgrading and a comparison with the alternatives.
- `docs/config.schema.json` plus a `yaml-language-server` modeline in
  `config.example.yaml`, so editors autocomplete and validate the config.
- Project health files: `LICENSE` (MIT), `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue and pull request templates, `CHANGELOG.md`.
- CI on GitHub Actions: ruff, pytest on 3.12 and 3.13, compose validation, a language
  guard, an image build, CodeQL and Dependabot; a tag-triggered release workflow.
- `immich-compressor --version`.

### Fixed

- `setup` aborted against a correctly configured Immich. It validated the API key with
  `GET /users/me`, which needs `user.read` — a permission this service deliberately never
  requests. A live v3.1.0 answers **403** there for a valid key and **401** for a bogus one;
  403 now counts as valid, and the distinction is recorded in `docs/immich-api-notes.md`.
- `scripts/quickstart.sh` ran the setup container on the default bridge network, so the
  documented `http://immich-server:2283/api` could never resolve. It now joins the Immich
  network (`NETWORK=`, default `immich_default`).
- `docker-compose.test.yaml` hard-coded `container_name: immich_server`, `immich_postgres`
  and `immich_redis` — the same names Immich's own compose file uses — so the test stack
  could not start on any host already running Immich. The names are gone and the host port
  is configurable through `COMPOSE_HOST_PORT`.
- `probe_hardware_encoder` reported libva's startup banner instead of the actual failure,
  because the first five lines of ffmpeg's stderr are `libva info:` chatter and the
  component prefix carries a per-run heap address.

### Changed

- `docker-compose.yaml` pulls the published image instead of building locally, and
  deployment-specific settings belong in a gitignored `docker-compose.override.yaml`.
- `check` now delegates its hardware section to the new detection code.
- `README.md` shrank from 712 lines to a front page; nothing was lost, it moved to `docs/`.
- `config.example.yaml` is a minimal working file; the commented GPU presets moved to
  `docs/hardware.md` now that they are selected automatically.
- Every human-readable string in the repository is English.

### Removed

- `PLAN.md`. Its verified API findings live on in `docs/immich-api-notes.md`; the rest was
  superseded by the code.

## [1.0.0] - 2026-08-19

First working release, developed and verified against a live Immich v3.1.0 instance.

### Added

- Webhook-driven service: FastAPI endpoint, SQLite job store (WAL), asyncio worker and a
  trash sweeper. `POST /webhook`, `GET /healthz`, `/stats`, `/jobs`, `/jobs/{id}`,
  `POST /reprocess/{id}`.
- Ten-step pipeline: guards, download, encode, sanity gate, upload, `PUT /assets/copy`,
  explicit field and tag carry-over, versioned markers on both assets, deferred removal of
  the original.
- Typed Immich v3 client covering assets, metadata KV, tags, copy, trash and restore, with
  retries on transport errors and 5xx.
- Preset system with shell-free execution: commands are `shlex.split` at load time and
  rejected if they contain shell control operators, redirections or command substitution.
- Sanity gate: size ratio, decodability, rotation-aware display size, bit depth, HDR
  transfer, duration drift, audio stream count and capture date.
- Four-step verification chain in front of every delete: replacement present and not
  trashed, checksum equal to the uploaded bytes, `dateTimeOriginal` set, marker written.
- `delete_mode: permanent` for reclaiming space immediately, rejected at startup unless
  `trash_original: true` and `dry_run: false`.
- CLI: `serve`, `check`, `encode`, `report`, `reprocess`, `requeue`, `backfill`, `restore`.
- Configuration through `config.yaml` plus `__`-nested environment overrides, with secrets
  read from the environment only and rejected in the file.
- GPU encoding through an optional `docker-compose.gpu.yaml` overlay, with a one-frame
  hardware probe at startup and in `check`.
- Marker v2: a v1 marker without a `replacedBy` field is retried once, because the v1
  sanity gate compared stored frame sizes and rejected every rotated video.
- Test suite: unit tests with mocked HTTP plus a `live`-marked end-to-end suite against a
  full Immich v3.1.0 stack (`docker-compose.test.yaml`).

[Unreleased]: https://github.com/Navilois/immich-compressor/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/Navilois/immich-compressor/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/Navilois/immich-compressor/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/Navilois/immich-compressor/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Navilois/immich-compressor/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/Navilois/immich-compressor/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/Navilois/immich-compressor/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/Navilois/immich-compressor/releases/tag/v1.0.0
