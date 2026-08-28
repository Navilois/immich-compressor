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

### The window between the upload and the job

`re_uploaded` is a verdict the pipeline reaches, and it reaches it when a worker gets to
that job — not when the copy arrives. Immich answers `POST /assets` with 201 and the asset
is live immediately; its job sits in `queued`, carrying no checksum at all, until step 2
reads the asset and writes one. Behind a backlog of video encodes that is minutes to hours,
and a device syncing inside it was handed exactly the collision above. Measured on
2026-08-28: 23 jobs of one re-upload burst were still queued when the batch failed.

So the shim does not wait for the job row. **A sync line carrying a checksum the shim is
armed to hand to a different asset is itself the answer** — the checksum is taken, by the
asset that line is about. The translation stands down from that line onwards, and the shim
remembers the claim for later requests: the copy's line is a delta and goes past once,
while the replacement's line comes back every time anything touches it.

Two things follow from where that evidence comes from.

**It works forwards only.** The response is rewritten line by line and never held whole, so
a claim on one line governs the lines after it. Nothing orders a run of asset lines so that
the copy always precedes the replacement it took the checksum from; when it lands the other
way round, that one batch still fails on the unique index. What changes is what happens
next — Immich re-sends the batch, and the re-send is served from maps that already know, so
the retry applies instead of the client wedging in a loop.

**It lives in memory.** A restart forgets every sighting and the shim is back to asking the
job store. That is usually the same answer by then: the window is the length of the queue,
and a restart restarts the workers too.

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

### What has to be true first

The shim only ever sees traffic a reverse proxy hands it, and **a stock Immich has no
reverse proxy.** The official compose file publishes `immich-server` on `2283` and serves
the web app and the API from that container directly — `docker-compose.test.yaml` in this
repository is derived from it and has no proxy either. If that is your deployment, the shim
is not two `location` blocks away: there is nothing yet to put them in. That is the
condition [motivation.md](motivation.md#r10-and-the-one-thing-that-is-not-solved) records
as the one this project does not meet.

So there are two situations, and only the first is a small change.

**You already run a proxy in front of Immich** — nginx, Caddy, Traefik, Nginx Proxy
Manager. Add the two routes to it. Nothing else about your deployment moves.

**You do not.** Then adding one *is* the change, and it moves your front door:

- The proxy becomes the address every client uses. Immich's published port stops being the
  way in — bind it to `127.0.0.1` or drop it. A client still pointed at `:2283` never passes
  through the shim, which is the coverage limit [below](#limits) and looks exactly like a
  broken install: everything configured, `shim_requests_total` at zero.
- Every client is repointed at the new address, the mobile app included.
- TLS, if Immich was terminating it, terminates at the proxy now.

That is real work, and it is worth doing and testing *before* `shim.enabled: true` — a proxy
that already carries your traffic correctly is one variable, and the shim is then the only
new one.

### The proxy has to be able to reach this service

`proxy_pass http://immich-compressor:8080` resolves only from inside the docker network the
service is on. nginx resolves a literal name like that **when it parses the configuration**,
not per request, so a proxy that cannot see this service does not fail at request time — it
refuses to start at all:

```
nginx: [emerg] host not found in upstream "immich-compressor"
```

A proxy living in its own stack — Nginx Proxy Manager, a standalone Caddy — has to join the
network named by `IMMICH_NETWORK`, the same one this service and Immich already share
([installation.md](installation.md#networking)).

### The service side

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

### The proxy side

Two paths must reach this service, and on both of them the only method the shim answers is
**POST** — which is also the only method Immich offers there. Everything else, every other
path and every other method, goes straight to Immich.

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name photos.example.com;

    # Server level on purpose: these are inherited by all four locations below. Put them
    # inside `location /` instead — which is where a lot of existing configurations keep
    # them — and the two shim routes silently lose them.
    client_max_body_size 50000M;
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade           $http_upgrade;
    proxy_set_header Connection        $connection_upgrade;

    location = /api/sync/stream {
        proxy_pass http://immich-compressor:8080;
        proxy_buffering off;                     # this one is a stream
        proxy_read_timeout 1h;
        proxy_intercept_errors on;
        error_page 500 502 503 504 = @immich;    # fail open
    }

    location = /api/assets/bulk-upload-check {
        proxy_pass http://immich-compressor:8080;
        proxy_intercept_errors on;
        error_page 500 502 503 504 = @immich;
    }

    location @immich { proxy_pass http://immich-server:2283; }

    location / { proxy_pass http://immich-server:2283; }
}
```

Three things in there are easy to get wrong.

**No trailing slash on `proxy_pass`.** `http://immich-compressor:8080` forwards the client's
URI unchanged, which is what the shim expects — it mounts the two paths at the root of its
own app and reads `request.url.path` to build the upstream call. A trailing `/` would make
nginx rewrite the URI instead, and the shim would forward `/` to Immich.

**The header block is at server level.** Whatever your existing configuration sets for
Immich, the two new locations need the same, and inheriting it is less fragile than copying
it twice. The `Upgrade`/`Connection` pair is there for Immich's own real-time updates rather
than for the shim; it is not something this repository has measured.

**`error_page` covers `500`, not just the gateway codes.** The shim catches a network error
to Immich and answers `502` itself, which is the intended fail-open path — but a bug in this
service would be a `500`, and a `500` that is not intercepted is a sync failure with no
fallback. Intercepting it costs nothing: a genuine `500` from Immich is simply fetched from
Immich again.

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

### If you have no proxy yet

The smallest one that does the job, as a compose service on the network Immich and this
service already share. It can live in this project's `docker-compose.override.yaml`, in
Immich's compose file, or in one of its own — the only requirement is the network.

```yaml
services:
  proxy:
    image: nginx:1.27-alpine
    restart: unless-stopped
    volumes:
      - ./immich-proxy.conf:/etc/nginx/conf.d/default.conf:ro
    ports:
      - '80:80'
    networks:
      - immich

networks:
  immich:
    external: true
    name: ${IMMICH_NETWORK:-immich_default}
```

`immich-proxy.conf` is the block above. This is HTTP on port 80 and terminates no TLS: it is
the shape of the thing, not a deployment. Put your certificates on it, or put it behind
whatever already holds them, before it carries anything but LAN traffic.

### Caddy

The same two routes, ordered the way Caddy orders handlers rather than the way nginx matches
locations:

```caddyfile
photos.example.com {
    @shim path /api/sync/stream /api/assets/bulk-upload-check
    handle @shim {
        reverse_proxy immich-compressor:8080 immich-server:2283 {
            lb_policy first          # this service while it is up
            lb_try_duration 5s       # Immich when it is not
            fail_duration 10s
            flush_interval -1        # Caddy's `proxy_buffering off`
        }
    }
    handle {
        reverse_proxy immich-server:2283
    }
}
```

Caddy fails open by listing Immich as a second upstream rather than by catching a status
code, and the three load-balancer settings are what make that work rather than decoration.
`lb_policy first` sends everything to this service while it is reachable; `fail_duration`
marks it down when it is not. **`lb_try_duration` is the one to keep**: without it the first
request after this service stops is answered `502` and only the ones after that fail over —
measured here, `502` then `200` then `200`. With it, the first request fails over too.

What Caddy will *not* do for these two routes is fall back on a bad status code the way the
nginx `error_page` line does. The obvious spelling — `handle_response` matching `status 500`
with a nested `reverse_proxy` — cannot replay a request that has a body, and both of these
routes are POST:

```
readfrom ...: http: invalid Read on closed Body
```

so the client gets `502` instead of the fallback. A GET through the same block falls back
fine, which is what makes this easy to miss in a quick test. The consequence is narrow: a
Caddy deployment fails open when this service is **down**, but not if it is up and answering
`500`. Weigh that before routing production traffic through it — swapping the `handle` block
back to plain `reverse_proxy immich-server:2283` is a one-line, seconds-long mitigation.

Traefik has the routers and the priority rules to express the routing; the fail-open half
would need the same kind of check the Caddy block needed, and neither has been run here.

### What of this was verified

Both proxy blocks above were run on **2026-08-28** — nginx 1.27.5 and Caddy 2 — against this
service and a stub standing in for Immich:

- **Routing.** Both documented paths reach the shim and nothing else does:
  `shim_requests_total` advances by exactly the number of requests sent to those two paths,
  while other paths are served by the upstream without moving it.
- **Fail open, nginx.** With this service stopped, both routes still answer `200` from
  Immich with body and path intact — nginx generates the `502` itself and `error_page`
  catches it regardless.
- **`error_page 500`, nginx.** A `500` from the proxied service is intercepted and re-served
  from Immich as `200`. That is what puts `500` in the list.
- **Fail open, Caddy.** Verified only in the upstream-down case, and only with
  `lb_try_duration` set; the status-code case is the limitation described above.
- **Name resolution.** A proxy that cannot resolve `immich-compressor` fails to start, with
  the message quoted earlier.

What that rig does **not** cover is the translation itself against a real library, or a
first sync of a large one. Those are verified separately — [the counters](#reading-the-counters)
and the field test in the [CHANGELOG](../CHANGELOG.md).

## Rolling it out

Do these in order. Each step is readable in `immich-compressor report` and at `/metrics`.

1. **Baseline.** Note the current `re_uploaded` count. That is the size of your problem. If
   it is zero, nothing is re-uploading and you do not need the shim.
2. **`log_only: true`.** Route the two paths here. `shim_requests_total` should start
   climbing. If it stays at zero, your reverse proxy is not sending anything here — fix that
   before changing bytes.
3. **`log_only: false`, `rewrite_sync_stream: false`, `rewrite_upload_check: false`.**
   Gates start opening. Watch `shim_gates_opened_total`. Nothing is being rewritten yet, so
   a mistake at this point costs nothing.

   Both rewrite flags default to `true`, so both have to be named here. Leaving
   `rewrite_upload_check` at its default would put the upload-check translation live at
   this step — `shim_hashes_translated_total` climbing is how you would find out.

   Whether `shim_touches_total` moves with it depends on `delete_mode`, and both answers
   are correct. On `trash` the no-op updates fire alongside the gates, because the shim
   makes one whenever it sees a purge. On `permanent` they are held back until step 4: the
   touch exists to have a line re-sent for rewriting, and nothing is being rewritten yet.

   On `trash`, expect this step to be quiet for a while regardless. A gate opens when
   Immich purges an original, which is up to 30 days after this service trashed it.

4. **`rewrite_sync_stream: true`**, and `rewrite_upload_check: true` with it unless you
   have a reason not to. The translation goes live. Watch your phone's backup
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
A second line names claims the shim learned from the stream before the pipeline had
classified them — those assets are still `queued` in the job store, so looking for them
there finds nothing.

That first line is **not live state.** It is written when the maps are rebuilt, which
happens on a request and no more often than `shim.ledger_refresh_seconds`; with no client
syncing it goes stale and stays stale. On 2026-08-28 it read `29` for 28 minutes while a
check against the library found 138 of the 8,021 ledger checksums live on another asset.
Use the script, not the log, to answer "how many right now".

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
- **A returned copy suppresses a translation only if this service saw it, one way or the
  other.** There are two ways: a `re_uploaded` job row, and the copy's own line going past
  on the sync stream. A copy that arrived while the service was stopped, or one the ingest
  guards refused before any row was written — a disabled asset type, for instance — leaves
  no job row, so until its line is offered to some client through the shim it holds the
  checksum without leaving a record that says so, and the translation is made while it is
  live. That is the collision above. [upgrading.md](upgrading.md) has a read-only script
  that lists any of these against your own library.
- **A sighting is not durable.** Claims learned from the stream are held in memory, so a
  restart drops them and any that the pipeline has not classified yet stop suppressing
  until their line is seen again. The job store is the durable half and needs no help.
- **The job store becomes load-bearing.** The translation is rebuilt from it every minute.
  Lose the database and the next update to a replaced asset sends its real checksum, the
  phone's mirror corrects itself, and the re-upload happens. Back it up.
- **It fails open, always.** A parse error, an unreachable Immich, a ledger it cannot read —
  every one of them forwards what Immich said, unchanged. A shim that breaks sync would be
  worse than the problem it solves.
