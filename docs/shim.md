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

Since 1.3.2 the pipeline *recognises* this: the returning bytes are skipped as
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
index. The original's own row holds its checksum for as long as the original exists, even
while it sits in the trash.

So the replacement can only be given that checksum *after* the original is really gone. Do
it earlier and the phone's database either silently drops the original's row or refuses the
write and aborts the whole sync batch.

That is what the **gate** is. Every replacement in the job store carries an
`original_freed_at` timestamp, empty until the original stops existing. Until it is set,
the shim passes that asset's lines through untouched.

## How the gate opens

It depends on `delete_mode`, because the service only witnesses one of the two cases.

**`delete_mode: permanent`.** The service deletes the original itself, so it knows the
moment. It records the gate and immediately makes a no-op update to the replacement.

**`delete_mode: trash` (the default).** The original goes to the trash and Immich deletes
it for real when its retention window expires — up to 30 days later, inside Immich, with
nothing reported back here. The shim watches for it instead: the purge produces a delete
event on the sync stream, and the shim is in that stream. When it sees a delete for an
original it replaced, it opens that gate and asks for the same no-op update.

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

`proxy_buffering off` is not optional. With buffering on, nginx holds the sync stream and
the app stalls waiting for a response that never finishes arriving. If you use Caddy or
Traefik, find their equivalent before you route anything.

## Rolling it out

Do these in order. Each step is readable in `immich-compressor report` and at `/metrics`.

1. **Baseline.** Note the current `re_uploaded` count. That is the size of your problem. If
   it is zero, nothing is re-uploading and you do not need the shim.
2. **`log_only: true`.** Route the two paths here. `shim_requests_total` should start
   climbing. If it stays at zero, your reverse proxy is not sending anything here — fix that
   before changing bytes.
3. **`log_only: false`, `rewrite_sync_stream: false`.** Gates start opening and the no-op
   updates start firing. Watch `shim_gates_opened_total` and `shim_touches_total`. Nothing
   is being rewritten yet, so a mistake at this point costs nothing.
4. **`rewrite_sync_stream: true`.** The translation goes live. Watch your phone's backup
   counter after a permanent delete: it must not grow. The `re_uploaded` count must stop
   rising.

## Reading the counters

| Metric | What a number means |
|---|---|
| `shim_requests_total` | Requests proxied. Zero while enabled means the reverse proxy is not routing here. |
| `shim_lines_rewritten_total` | Sync lines whose checksum was translated. |
| `shim_hashes_translated_total` | Checksums translated in either direction. |
| `shim_gates_opened_total` | Originals observed to be gone for good. |
| `shim_touches_total` | No-op updates made so a replacement is re-sent. Zero while gates are opening means the translation is armed but may never reach a device. |
| `shim_passthrough_errors_total` | Times Immich was unreachable and clients got a 502. Anything above zero means somebody saw a sync failure. |

`re_uploaded` is the one to watch overall. It counts re-uploads that already happened, so
after the shim is live it should stop rising. Whatever still arrives names a client the
shim is not in front of.

## Limits

- **Coverage follows routing.** A client that reaches Immich directly — over the LAN, over
  a VPN, on a second hostname — never passes through the shim and is unaffected.
- **`bulk-upload-check` does not help phones.** The mobile app does not use that route. It
  covers `immich-go`, the CLI and the web uploader.
- **No retroactive fix.** Replacements made before the ledger shipped in 1.3.2 carry no
  record of what their original hashed to, and for an original that is already permanently
  deleted that value cannot be recovered. The shim is complete from 1.3.2 onwards.
- **The job store becomes load-bearing.** The translation is rebuilt from it every minute.
  Lose the database and the next update to a replaced asset sends its real checksum, the
  phone's mirror corrects itself, and the re-upload happens. Back it up.
- **It fails open, always.** A parse error, an unreachable Immich, a ledger it cannot read —
  every one of them forwards what Immich said, unchanged. A shim that breaks sync would be
  worse than the problem it solves.
