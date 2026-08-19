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
| **Assets with manually named faces** | Faces are re-detected for the replacement, so a name you typed could be lost. `skip_if_named_people: true` is the default. |
| **Anything below `min_size_bytes`** | 20 MiB by default. |
| **Anything it has already processed** | The `compressor` metadata marker on the asset is a hard, versioned loop guard. |

It never calls `POST /trash/empty`, ever — that endpoint drops your *entire* trash,
including assets you deleted by hand and may still want back. Originals are removed one
asset id at a time.

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

- it is at most `max_ratio` (0.6) of the original — otherwise there was no point;
- it decodes, and has a video or image stream;
- its **display** size matches the source's. Not the stored size: a portrait phone clip is
  coded 1920x1080 with a 90° display matrix, and an encoder may legitimately keep the matrix
  or bake the rotation into the pixels. What the gate rejects is the third case — pixels left
  unrotated *and* the matrix lost, which is the one that actually damages the picture;
- the bit depth did not drop;
- an HDR transfer function was not lost, so a 10-bit HDR source can never be silently
  flattened to washed-out SDR;
- the duration is within 0.5 s of the source's, and the audio stream count is unchanged;
- the output carries a capture date.

A failure here marks the *original* so the same CPU is not burned on it again, and records
the asset as `skipped: no_gain`. Nothing is uploaded and nothing is deleted.

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
