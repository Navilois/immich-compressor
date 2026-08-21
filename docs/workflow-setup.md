# The Immich workflow

The service does nothing until Immich tells it about an asset. That happens through a
workflow with three steps: filter by type, filter out our own uploads, call the webhook.

`immich-compressor setup` creates this for you when the credentials allow it. This page is
for when they do not, and for understanding what the steps are doing.

## The JSON

This is the exact body that was created and fired successfully against a live v3.1.0
instance:

```json
{
  "name": "immich-compressor",
  "description": "Recompress large assets out of band",
  "trigger": "AssetMetadataExtraction",
  "enabled": true,
  "steps": [
    {
      "method": "immich-plugin-core#assetTypeFilter",
      "config": { "allowedTypes": ["VIDEO", "IMAGE"] },
      "enabled": true
    },
    {
      "method": "immich-plugin-core#assetFileFilter",
      "config": { "pattern": "^(?!.*\\.cmp\\.).*$", "matchType": "regex", "usePath": false },
      "enabled": true
    },
    {
      "method": "immich-plugin-core#webhook",
      "config": {
        "url": "http://immich-compressor:8080/webhook",
        "method": "POST",
        "headerName": "X-Compressor-Token",
        "headerValue": "<COMPRESSOR_TOKEN>"
      },
      "enabled": true
    }
  ]
}
```

`setup` writes this to `immich-workflow.json` with your real token substituted in — mode
0600, and gitignored, because that token is the shared webhook secret in clear text. Create
it with:

```bash
curl -X POST "$IMMICH_URL/api/workflows" \
  -H "Authorization: Bearer $SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d @immich-workflow.json
```

`$SESSION_TOKEN` is a **browser session token**, not the API key. Workflow endpoints need
`workflow.create` / `workflow.read`, and those are deliberately not among the permissions
this service asks for — granting them to a long-lived key would widen it well past its job.

Or do it in the UI: **Utilities → Workflows → New**.

Either way, delete `immich-workflow.json` once the workflow exists. It has no further use,
and the running service reads its token from `.env`.

## Why each step is what it is

**The trigger is `AssetMetadataExtraction`, not `AssetCreate`.** Only afterwards is
`exifInfo` populated, and GPS, tags, rating and description are exactly what has to be
carried over to the replacement.

**The filename filter is a negative lookahead** because `assetFileFilter` has no `inverse`
option. Immich's regex engine supports lookaheads — verified. Without this filter the
compressed upload re-triggers the workflow, which was confirmed to happen. It is only the
first line of defence; the hard loop guard is the `compressor` metadata marker on the asset.

If you change `behavior.compressed_marker`, change this pattern to match.

**Only one custom header can be configured**, which is exactly the shared secret. It must
equal `WEBHOOK__TOKEN` (that is `COMPRESSOR_TOKEN` in `.env`). A mismatch is a 401 that
Immich reports as success — see below.

## Three things that will confuse you

> **`POST /workflows` answers with `"steps": []`** even though the steps *were* saved.
> Confirm with `GET /workflows/{id}` — they are there.

> **Immich ignores the webhook's response status.** A 401 (wrong `headerValue`), a 422
> (payload the service could not parse) or a 500 is still logged as *"Workflow … executed
> successfully"*. Never diagnose from the Immich side alone; the compressor logs every
> rejection at WARNING or ERROR.

> **If a workflow stops firing, restart `immich-server`.** Creating or editing a workflow
> normally takes effect immediately (verified), but execution was observed going quiet after
> a workflow run threw `NoResultError` — triggered by an asset being hard-deleted while its
> workflow was executing. A restart cleared it.

## Video only

The type filter and `behavior.enabled_types` have to agree, and both ship with photos
enabled. To leave stills alone, narrow both:

```json
{ "method": "immich-plugin-core#assetTypeFilter",
  "config": { "allowedTypes": ["VIDEO"] }, "enabled": true }
```

```yaml
behavior:
  enabled_types: [VIDEO]
```

Narrowing only one of the two is not harmful, just wasteful in one direction and useless in
the other: the workflow would keep sending webhooks that the service skips as `wrong_type`,
or the service would be ready for stills that never arrive.

The stills preset re-encodes to JPEG with ImageMagick and carries metadata across with
exiftool, which is verified tag by tag on every job. It only touches JPEG — RAW, HEIC, PNG,
GIF, TIFF, WebP and motion photos are all refused, and a source that is already heavily
compressed is left alone. A JPEG re-encode *is* generationally lossy in a way an
H.264 → HEVC video re-encode largely is not, so read
[safety.md](safety.md#why-only-jpeg-stills) before pointing it at a library you care about.

## Turning it off

Toggle the workflow in **Utilities → Workflows**, or:

```bash
curl -X PUT "$IMMICH_URL/api/workflows/$WORKFLOW_ID" \
  -H "Authorization: Bearer $SESSION_TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled": false}'
```

That stops new work reaching the service. Jobs already queued still run — stop the container
too if you want everything to stop.
