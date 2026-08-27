# The checksum-translation shim

**Off by default.** Nothing on this page happens until you set `shim.enabled: true` *and*
point a reverse proxy at two paths. Read [safety.md](safety.md) first if you have not.

## What it is for

Your phone decides what to back up without asking the server. It hashes each local file
with SHA-1 and looks that hash up in the list of remote assets it has mirrored locally; if
nothing matches, the file is a backup candidate. That decision is entirely offline.

So when this service replaces an asset and the original is eventually deleted for good, the
original's checksum stops existing anywhere in Immich. The phone still holds the file, finds
no match, and uploads it again. You get the original back, at full size, and the space the
compression reclaimed is given away.

Since 1.4.0 the pipeline *recognises* this: the returning bytes are skipped as
`re_uploaded` rather than compressed a second time (see [faq.md](faq.md)). But recognising
it does not prevent it — the upload still crosses the network, still costs a full round of
thumbnails and machine learning on the server, and still leaves a duplicate in the library.

The shim prevents it. Where the sync stream hands your phone the compressed replacement, it
substitutes the **original's** checksum into that one field. The phone finds a match for
the file it is holding and never queues it.

## What it is not

It does not change anything in Immich. The database keeps the real checksum, the web app
shows the real checksum, and every other client sees the real checksum. The substitution
happens in two HTTP responses on their way to a client and nowhere else.

It is still, plainly, telling a client something untrue. That is a deliberate trade and you
should make it knowingly: the phone is told a file it does not have is a file it does have,
because that is the only signal Immich exposes that can stop the upload. Upstream has
declined to keep a registry of deleted hashes on the server side, so there is no honest
version of this available today.

## The one rule that makes it work

The phone's mirror allows **exactly one row per (owner, checksum)** — it enforces a unique
index. So the rule the shim has to keep is this one:

> A replacement may be handed the original's checksum only while **no other asset of the
> same owner holds that checksum**.

Get it wrong and the phone's database either silently drops the other row or refuses the
write and aborts the whole sync batch — not the one line, the batch, and the sync stops
making progress because the failure comes before the checkpoint is acked.

Two different assets can be that other holder, and the shim waits for both.

**The original itself**, until it is really gone — even while it sits in the trash, its row
is still there holding the checksum. That is what the **gate** is: every replacement in the
job store carries an `original_freed_at` timestamp, empty until the original stops
existing, and until it is set the shim passes that asset's lines through untouched.

**A copy the device uploaded again**, after the gate opened. That one is below.

## How the gate opens

It depends on `delete_mode`, because the service only witnesses one of the two cases.

**`delete_mode: permanent`.** The service deletes the original itself, so it knows the
moment. It records the gate there and then — whether or not the shim is running, because
that is a fact about the server — and makes the no-op update if the shim is on and
rewriting.

**`delete_mode: trash` (the default).** The original goes to the trash and Immich deletes
it for real when its retention window expires — up to 30 days later, inside Immich, with
nothing reported back here. The shim watches for it instead: the purge produces a delete
event on the sync stream, and the shim is in that stream. When it sees a delete for an
original it replaced, it opens that gate and asks for the same no-op update.

Both paths count the same two things — `shim_gates_opened_total` when the gate opens,
`shim_touches_total` when the no-op update lands. Which of the two saw it is not in the
counters and does not need to be. A deployment that switched `delete_mode` can have both
observe the same original; the gate is first-write-wins, so it is still counted once.

## When the checksum comes back

An open gate says the original is gone. It does not say the checksum is free for ever, and
those are not the same claim.

A device that still holds the file uploads it again — that is the whole situation this page
exists for, and it keeps happening for every file the shim was not yet in front of. Immich
accepts the upload, because at that moment nothing holds the hash. The result is a **new
asset carrying the original's checksum**. This service recognises it and stops there:
`re_uploaded` deletes nothing and changes nothing, by design. So the checksum is live
again, under a new id, while the gate for the replacement is still open.

Translating now breaks the rule exactly as badly as translating too early. It has been seen
on a real device:

```
Error: updateAssetsV2 - user
SqliteException(2067): UNIQUE constraint failed:
  remote_asset_entity.owner_id, remote_asset_entity.checksum
```

and the sync then stopped advancing at all, because the batch dies before its ack and the
server re-sends the same batch.

So the shim holds that translation back for as long as the returned copy exists. **This
costs the user nothing**: while the copy is there it is itself the match the phone is
looking for, so the local file is not a backup candidate anyway. The prevention the shim
provides is already being provided by the duplicate.

When the copy is deleted in turn, its delete goes past on the same sync stream. The shim
records it against that copy's own job row, makes the same no-op update on the replacement,
and the translation is armed again — otherwise removing the duplicate would simply start
the upload cycle over.

Nothing here is counted, because nothing happened. It is logged instead, on the line that
says how many translations are currently held back, and it changes only when the number
changes.

### Why a no-op update is needed at all

The sync stream only offers a client assets that changed since its last checkpoint. Nothing
has changed about the replacement since it was created, so it would never be sent again —
and the shim would never get a line to rewrite.

Writing any field of an asset regenerates its sync cursor, even when the value written is
the value that was already there. So the service reads the replacement's `isFavorite` and
writes the same value straight back. Nothing a user can see changes; the asset simply
becomes something the server offers again.

This is the one write the shim's machinery makes against your library. It is counted as
`shim_touches_total`.

## Setting it up

Two paths must reach this service. Everything else goes straight to Immich.

```yaml
shim:
  enabled: true
  upstream_url: "http://immich-server:2283"   # the ORIGIN — no /api suffix
  log_only: true                              # start here
```

`upstream_url` is not `immich.base_url`. That one ends in `/api`; this one must not,
because the shim forwards the client's whole path and that path already begins with `/api`.
A value ending in `/api` is refused at startup rather than producing 404s from a server that
is obviously running.

nginx:

```nginx
location = /api/sync/stream {
    proxy_pass http://immich-compressor:8080;
    proxy_http_version 1.1;
    proxy_buffering off;                 # required: this is a stream
    proxy_read_timeout 1h;
    proxy_intercept_errors on;
    error_page 502 503 504 = @immich;    # fail open
}
location = /api/assets/bulk-upload-check {
    proxy_pass http://immich-compressor:8080;
    proxy_intercept_errors on;
    error_page 502 503 504 = @immich;
}
location @immich { proxy_pass http://immich-server:2283; }
location / { proxy_pass http://immich-server:2283; }
```

`proxy_buffering off` is the setting this was tested with, and the one to use. The reason is
narrower than this page used to claim.

It said the app stalls on a response that never finishes arriving. That is not what happens.
Measured on v3.1.0 against a 423-line, 265 KB sync response, buffering on and off were
indistinguishable — first byte inside 20 ms either way, byte-identical output, no stall —
with a fast client and again with one reading at roughly 40 KB/s. Immich's sync stream is a
*finite* response that completes and closes, so there is nothing for nginx to hold open.

What remains true is that this is a streaming endpoint and `proxy_buffering off` is the
setting that matches it: the shim rewrites and yields one line at a time, and the client
applies batches as they arrive rather than at the end. Buffering also lets nginx spool a
response larger than `proxy_buffers` to a temporary file — documented nginx behaviour, but
**not observed here**: no temp file appeared at 265 KB even with the slow client, and the
size a first sync of a large library reaches has not been tested. Treat the large-library
case as unverified in both directions.

If you use Caddy or Traefik, find their equivalent before you route anything.

## Rolling it out

Do these in order. Each step is readable in `immich-compressor report` and at `/metrics`.

1. **Baseline.** Note the current `re_uploaded` count. That is the size of your problem. If
   it is zero, nothing is re-uploading and you do not need the shim.
2. **`log_only: true`.** Route the two paths here. `shim_requests_total` should start
   climbing. If it stays at zero, your reverse proxy is not sending anything here — fix that
   before changing bytes.
3. **`log_only: false`, `rewrite_sync_stream: false`.** Gates start opening. Watch
   `shim_gates_opened_total`. Nothing is being rewritten yet, so a mistake at this point
   costs nothing.

   Whether `shim_touches_total` moves with it depends on `delete_mode`, and both answers
   are correct. On `trash` the no-op updates fire alongside the gates, because the shim
   makes one whenever it sees a purge. On `permanent` they are held back until step 4: the
   touch exists to have a line re-sent for rewriting, and nothing is being rewritten yet.

   On `trash`, expect this step to be quiet for a while regardless. A gate opens when
   Immich purges an original, which is up to 30 days after this service trashed it.

4. **`rewrite_sync_stream: true`.** The translation goes live. Watch your phone's backup
   counter after a permanent delete: it must not grow. The `re_uploaded` count must stop
   rising.

## Reading the counters

| Metric | What a number means |
|---|---|
| `shim_requests_total` | Requests proxied. Zero while enabled means the reverse proxy is not routing here. |
| `shim_lines_rewritten_total` | Sync lines whose checksum was translated. |
| `shim_hashes_translated_total` | Checksums translated in either direction. |
| `shim_gates_opened_total` | Originals observed to be gone for good, counted in both delete modes: the `permanent` delete this service performs, and the purge the shim sees go past. It follows the record, not the shim, so on `permanent` it climbs even with the shim off. |
| `shim_touches_total` | No-op updates made so a replacement is re-sent. Zero while gates are opening means the translation is armed but may never reach a device — except at step 3 above on `delete_mode: permanent`, where it is held back on purpose. |
| `shim_passthrough_errors_total` | Times Immich was unreachable and clients got a 502. Anything above zero means somebody saw a sync failure. |

`re_uploaded` is the one to watch overall. It counts re-uploads that already happened, so
after the shim is live it should stop rising. Whatever still arrives names a client the
shim is not in front of.

There is no counter for a translation held back by a returned copy, because nothing
happens when one is: the log line naming the current number is the place to look, and the
script in [upgrading.md](upgrading.md) is how to check the library rather than the record.

## Limits

- **Coverage follows routing.** A client that reaches Immich directly — over the LAN, over
  a VPN, on a second hostname — never passes through the shim and is unaffected.
- **`bulk-upload-check` does not help phones.** The mobile app does not use that route. It
  covers `immich-go`, the CLI and the web uploader.
- **`bulk-upload-check` needs a credential that resolves an owner.** Its ledger lookup is
  scoped by owner, so the shim asks `GET /users/me` first. A session token, or an API key
  holding `user.read`, answers 200; a key scoped to only the permissions this service needs
  answers 403, the owner comes back unresolved, and the translation then does nothing at
  all. It fails open and stays silent, so the symptom is a re-upload that the shim looked
  like it should have caught. Grant `user.read` to any key whose uploads you want covered.
- **No retroactive fix.** Replacements made before the ledger shipped in 1.4.0 carry no
  record of what their original hashed to, and for an original that is already permanently
  deleted that value cannot be recovered. Those replacements are never translated.
- **A returned copy suppresses a translation only if this service saw it arrive.** The
  recognition is a `re_uploaded` job row, so a copy that came in while the service was
  stopped — or one the ingest guards refused before any row was written, a disabled asset
  type for instance — holds the checksum without leaving a record that says so. The
  translation for that checksum is then made while the copy is live, which is the collision
  above. [upgrading.md](upgrading.md) has a read-only script that lists any of these against
  your own library.
- **The job store becomes load-bearing.** The translation is rebuilt from it every minute.
  Lose the database and the next update to a replaced asset sends its real checksum, the
  phone's mirror corrects itself, and the re-upload happens. Back it up.
- **It fails open, always.** A parse error, an unreachable Immich, a ledger it cannot read —
  every one of them forwards what Immich said, unchanged. A shim that breaks sync would be
  worse than the problem it solves.
