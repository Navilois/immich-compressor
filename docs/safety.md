# Is this safe?

This tool deletes the originals of photos and videos you cannot re-take. That is the whole
risk, and it is worth being explicit about how it is contained.

**Out of the box, nothing is deleted, uploaded or even downloaded.** The shipped
configuration is `dry_run: true`, `trash_original: false`, `delete_mode: trash`. You have
to change three separate settings, in that order, before an original can be removed — and
the last of those is refused at startup unless the other two already agree.

## What it never touches

| | Why |
|---|---|
| **External libraries** | The files are yours, not Immich's. `isExternal` or a `libraryId` means skip, always. |
| **Live photos** | The still and the video are one object; recompressing half of it breaks the pair. |
| **Edited assets** | `isEdited` means non-destructive edits are attached, and they do not follow the replacement. |
| **Locked-folder assets** | `visibility: locked` means skip. |
| **Assets already in the trash** | Checked from the payload *and* re-checked live before anything happens. |
| **Assets that were not just uploaded** | `createdAt` dates the upload, not the exposure. Past `max_asset_age_hours` (24 h) the webhook is a re-trigger, not an upload, and it is refused before a job exists — see [the metadata-extraction trap](operations.md#the-metadata-extraction-trap). |
| **Assets with manually named faces** | Faces are re-detected for the replacement, so a name you typed could be lost. `skip_if_named_people: true` is the default. |
| **Anything that cannot save `min_savings_bytes`** | 1 MiB by default. A file cannot save more bytes than it has, so this is decided before the download. |
| **Anything it has already processed** | The `compressor` metadata marker on the asset is a hard, versioned loop guard. |
| **Every still that is not a JPEG** | RAW, HEIC, PNG, GIF, TIFF and WebP are all filed under type `IMAGE` by Immich, and all of them are skipped as `unsupported_format` — see below. |
| **Motion photos** | A JPEG with a video glued on behind it. Detected and skipped as `embedded_media` — see below. |
| **Stills that are already heavily compressed** | At or below the preset's `min_source_quality`, a re-encode adds artefacts and usually *grows* the file. Skipped as `source_quality`. |

It never calls `POST /trash/empty`, ever — that endpoint drops your *entire* trash,
including assets you deleted by hand and may still want back. Originals are removed one
asset id at a time.

### Why only JPEG stills

The `IMAGE` preset carries an extension allowlist (`match.extensions`), not a denylist, and
that is the entry doing the most work in this whole table:

- **RAW (DNG/CR2/CR3/NEF/ARW)** — ImageMagick reads these through libraw. Without the
  allowlist a raw file would be *developed* into an 8-bit JPEG, pass every sanity check
  looking perfectly healthy, and have its original deleted. 14-bit linear sensor data,
  gone, with nothing anywhere reporting a problem.
- **HEIC** — libheif is read-only in this image, so it could only be written back as JPEG,
  and HEVC-intra beats JPEG by roughly 2x. The "compressed" file would be *larger* than the
  source. HEIC is already the efficient format; there is nothing to win.
- **PNG** — screenshots and text turn into ringing artefacts, and transparency is lost.
- **GIF / TIFF / WebP** — animation is destroyed, and there is no WebP write support.

### Why motion photos are skipped

A Samsung or Google motion photo is a JPEG with an MP4 glued on behind the end-of-image
marker. Re-encoding reads the JPEG and drops the trailer — and *every* other check reports
success: the metadata copy faithfully carries `XMP:MotionPhoto=1` across, the size ratio
looks excellent precisely because the video is gone, and the picture itself is unchanged.
Measured on a 1 935 292 byte source: 389 697 bytes out, no `ftyp` left.

Two independent signals catch it, because neither alone is enough. The XMP markers identify
the format but can be absent on vendor variants; the trailer check is format-agnostic but
cannot tell a video from any other appended payload. The trailer is found by walking the
JPEG's marker structure from the SOI rather than searching for the last `FFD9` — an
embedded thumbnail is itself a complete JPEG and ends in one, and an appended MP4 can
contain the byte pair by chance.

## The verification chain

Before an original is removed — in **both** delete modes — four things are checked against
the live server. If any one of them fails, nothing is deleted: the job stays in
`pending_delete` and retries in an hour.

1. **The replacement exists and is not itself in the trash.**
2. **The server's checksum equals the base64 SHA-1 the encoder computed for the file it
   uploaded.** Proof that the stored bytes are the bytes we made, not a truncated upload.
3. **`exifInfo.dateTimeOriginal` is set on the replacement.** Proof that Immich's metadata
   extraction ran, so the asset sits at the right place in the timeline.
4. **The `compressor` marker is present on the replacement.** Proof that the linking step
   completed and the replacement is traceable back to its source.

Running the chain in `trash` mode too is deliberate: a failing condition then costs a retry
and you find out about it while the delete is still undoable.

## The sanity gate

Before anything is *uploaded*, the encoded file has to pass every one of these:

- it is at most `max_ratio` of the original — 0.6 for video; for stills the preset lowers
  the bar to 0.9, because on a cheap encode the ratio is the wrong axis entirely;
- it saved at least `min_savings_bytes` in absolute terms. Ratio 0.75 on a 12 MB photo
  saves 3 MB, ratio 0.60 on a 371 KB photo saves 147 KB — and 147 KB does not pay for a
  permanent asset lifecycle of thumbnails, an embedding, face detection, OCR and a timeline
  entry;
- it decodes, and has a video or image stream;
- its **display** size matches the source's. Not the stored size: a portrait phone clip is
  coded 1920x1080 with a 90° display matrix, and an encoder may legitimately keep the matrix
  or bake the rotation into the pixels. What the gate rejects is the third case — pixels left
  unrotated *and* the matrix lost, which is the one that actually damages the picture;
- the bit depth did not drop;
- an HDR transfer function was not lost, so a 10-bit HDR source can never be silently
  flattened to washed-out SDR;
- the duration is within 0.5 s of the source's, and the audio stream count is unchanged;
- the output carries a capture date. Off for stills by preset, because a replacement's
  timeline position comes from the `fileCreatedAt` sent at upload and the explicit
  `dateTimeOriginal` write in step 8, not from the file — requiring the tag there would
  reject scans and EXIF-stripped exports after a full download and encode, for no gain.

A failure here marks the *original* so the same CPU is not burned on it again, and records
the asset as `skipped: no_gain`. Nothing is uploaded and nothing is deleted.

## The metadata gate

A still loses **all** of its metadata on a re-encode; `exiftool -TagsFromFile … -all:all`
puts it back. That mechanism was measured rather than assumed — a 39-tag source (Make,
Model, LensModel, DateTimeOriginal, Artist, Copyright, UserComment, ISO, FNumber, GPS
including altitude, XMP Rating/Description/Subject/Label, IPTC Keywords/City/Caption/
By-line) came through with 0 tags lost and 0 unexpected additions, and the embedded EXIF
thumbnail survived byte-identical.

It is still verified on every single job, because a mechanism working on a test file is not
the same as it working on your camera's files. After the copy, both files are read back
with `exiftool -G -EXIF:all -GPS:all -XMP:all -IPTC:all` and compared tag by tag. Anything
missing or changed is a finding. Tags the encode *added* are not reported: gaining a tag is
not losing one.

Values are compared as exiftool **presents** them, not as `-n` floats. EXIF stores
rationals, and copying a tag re-approximates the fraction: measured on a phone JPEG through
the shipped preset, `ExposureTime` moved `2497831/250000000` -> `1/100` and the GPS latitude
seconds `16316639/1000000` -> `39421/2416`. Both print identically, and comparing the raw
floats rejected every geotagged photo — with `metadata_verify: strict` and
`delete_mode: permanent` that is a gate no image can pass. The cost is that a change too
small to alter the printed value cannot be seen here, which is the intended reading of "the
metadata survived": the value a viewer is shown is the value that has to survive.

Four kinds of tag are on the ignore list, and each one is earned rather than convenient:

| Tag | Why |
|---|---|
| `EXIF:Orientation` | `normalize_orientation` pins it to 1 by design, and writes it even when the source had none |
| `XMP:XMPToolkit` | the version stamp of whatever last wrote the XMP packet, so exiftool stamps its own on every copy. It names the writing tool, not the picture |
| `EXIF:ThumbnailOffset`, `EXIF:PreviewImageStart`, `EXIF:OtherImageStart`, `EXIF:StripOffsets` | byte positions inside the file, not content. Rewriting the EXIF block moves them by definition — measured 1008 -> 1026. The matching `*Length` tags stay compared, because a thumbnail length that changes is a truncated thumbnail |

`behavior.metadata_verify` decides what a finding costs:

- **`strict`** (the default) — the job fails, the original is never touched, and the asset
  shows up in `report` as `failed`.
- **`warn`** — logged only. For the first days on unfamiliar camera material, where an
  unlisted MakerNotes quirk would otherwise block every image. **The config refuses this
  together with `delete_mode: permanent`**, because the two failure directions are not
  symmetric: a gate that fires wrongly costs a failed job, while a gate that stays silent
  wrongly costs the metadata *and* the original, with no rollback but a Postgres backup.

To check the gate against your own photos before it starts failing jobs for real:

```bash
docker compose exec immich-compressor immich-compressor encode /path/to/photo.jpg --type IMAGE
```

It runs the whole thing locally and prints `metadata_differences`, `source_quality` and
`embedded_media` without touching the server.

## Going live, in four stages

Do not skip a stage. Each one is reversible; the last one is not.

### Stage 1 — dry run (the default)

Leave everything as it is. Upload a few assets, wait for `initial_delay_seconds` (5
minutes), then:

```bash
docker compose exec immich-compressor immich-compressor report
```

Every asset shows up as `skipped: dry_run`. Nothing was created, changed or deleted on the
server — there is an automated test that asserts exactly that.

### Stage 2 — compress, keep the originals

```yaml
# docker-compose.override.yaml
services:
  immich-compressor:
    environment:
      BEHAVIOR__DRY_RUN: "false"
```

`setup` leaves that file ready to edit, so uncommenting the flag is the whole change. Two
things to check if the stage appears to do nothing: delete the `{}` line at the bottom of
the template once you uncomment anything, and — if `.env` carries a `COMPOSE_FILE` line —
make sure it ends with `:docker-compose.override.yaml`, because that line replaces the list
compose loads by default. `docker compose config` prints what actually applies.

Both versions now exist side by side. Spend a while here. On the replacement, check: album
membership, tags, rating, description, GPS, position in the timeline, stack, shared links,
and that the picture actually looks right at full size.

### Stage 3 — move originals to the trash

Grant `asset.delete` on the API key, then:

```yaml
      BEHAVIOR__TRASH_ORIGINAL: "true"
      BEHAVIOR__RETENTION_DAYS: "7"
```

Originals move to Immich's trash a week after they are replaced, and stay recoverable until
the trash is emptied. `immich-compressor restore --all-pending` brings back everything this
service trashed.

**Disk space does not go down in this stage.** Until the trash is emptied you are using
*more* space, not less. That is the price of the undo.

### Stage 4 — reclaim the space (irreversible)

```yaml
      BEHAVIOR__DELETE_MODE: "permanent"
      BEHAVIOR__RETENTION_DAYS: "0"
```

The original is deleted with `force: true` the moment the verification chain passes. It
never enters the trash: the database row is gone and the files are unlinked. `restore` and
**Utilities → Trash → Restore** both have nothing to work with.

**Take a backup of Postgres and the upload directory before enabling this**, and do not
enable it until stage 3 has run long enough that you trust the replacements. The service
logs a loud warning at every startup while it is on.

## Rolling back

**1. Stop the flow.** Disable the workflow in Immich (Utilities → Workflows → toggle), or:

```bash
curl -X PUT "$IMMICH_URL/api/workflows/$WORKFLOW_ID" \
  -H "Authorization: Bearer $SESSION_TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled": false}'

docker compose stop immich-compressor
```

**2. Restore the trashed originals** (`delete_mode: trash` only):

```bash
docker compose run --rm immich-compressor restore --all-pending
docker compose run --rm immich-compressor restore <assetId> <assetId>
```

Equivalent to `POST /trash/restore/assets`, or Utilities → Trash → Restore in the UI.
Verified: the asset comes back with `isTrashed: false`.

**3. Remove the replacements** (optional). They are identifiable three ways: the filename
ends in `.cmp.<ext>`, the `compressor` metadata key is set, and its value carries
`sourceId`. Delete them normally — the restored originals keep their albums and tags.

**4. Clear the service's own state** for a clean slate:

```bash
docker compose down
docker volume rm immich-compressor_compressor-state
```

> **If the trash was already emptied, or `delete_mode: permanent` was in effect, the
> original is gone.** There is no undo. `immich-compressor restore` answers
> `HTTP 400 Not found` for the whole batch rather than silently doing nothing, and says why.
> The only way back is a backup of Postgres *and* the upload directory from before the run.

## Threat model

See [SECURITY.md](../SECURITY.md). In short: preset commands never go through a shell, the
webhook secret is compared in constant time, secrets are environment-only and rejected in
`config.yaml`, the container runs as a non-root user with no capabilities, and there is no
telemetry of any kind — the service talks to your Immich server and to nothing else.
