# Security

## Supported versions

| Version | Supported |
|---|---|
| 1.3.x | ✅ |
| 1.2.x | security fixes only |

Fixes are released as a new patch version. The compose file pins the major image tag, so a
`docker compose pull` picks them up.

## Reporting a vulnerability

Use GitHub's private reporting: **Security → Report a vulnerability** on
[the repository](https://github.com/Navilois/immich-compressor/security/advisories/new).
That opens a private advisory only the maintainers can see.

Please do not open a public issue for anything exploitable. Include what you did, what
happened, and the version — an expected acknowledgement is within a week.

## Threat model

This service holds an Immich API key with permission to delete assets, and runs external
encoder binaries against files it downloads. Both are treated as such.

### The API key

Read **only** from the environment, as `IMMICH__API_KEY`. Putting it into `config.yaml` is
rejected at startup, so it cannot reach a repository or an image layer by accident. It is
never logged: it is held as a pydantic `SecretStr`, and `setup` writes `.env` at mode 0600.

Grant [the listed permissions](docs/installation.md#api-key-permissions) and nothing more.
`asset.delete` is only needed once `trash_original: true`; leaving it off is a hard
guarantee that nothing can be removed. `workflow.create` is deliberately not requested — the
one call that needs it wants a session token instead.

### The webhook secret

Compared with `hmac.compare_digest`, so a wrong token cannot be found by timing. A rejected
request is logged at WARNING with the peer address — which matters, because **Immich ignores
the response status and reports the workflow as successful either way**.

`/webhook`, `/reprocess` and `/resume` require the secret — `/resume` clears the surge
breaker, which re-arms a service that deletes originals, so it is not an anonymous action.
`/healthz`, `/stats`, `/metrics` and `/jobs` do not: they expose no asset content, only
counts, states and asset ids. No port is published by default, and the compose file tells
you to bind to `127.0.0.1` if you publish one.

### Encoder commands

Preset commands never go through a shell. They are `shlex.split` at load time into an argv
list, executed with `asyncio.create_subprocess_exec`, and rejected outright at startup if a
token is a shell control operator (`|`, `&&`, `;`), a redirection (`>`, `2>&1`) or a command
substitution (`` ` ``, `$(`). A filename can therefore never be interpreted as a command,
however it is spelled.

The distinction is at token level, not substring level: `-vf format=nv12|vaapi,hwupload` is
legitimate ffmpeg syntax and is accepted, because a shell would not have split that `|` off
into a word of its own.

### The container

Runs as uid 10001, non-root, with no added capabilities and no host mounts beyond the
read-only `config.yaml` and its own two volumes. It needs the host's `render` group only
when a GPU is passed through, and that passthrough is opt-in through a separate compose
file.

### Data that leaves the machine

None. There is no telemetry, no analytics and no update check. The only outbound connection
is to `immich.base_url`.

### Supply chain

Images are built by GitHub Actions from a tagged commit, published to ghcr.io with build
provenance and an SBOM attached, and can be verified with `gh attestation verify`. CodeQL
and Dependabot run against the repository.

## Not in scope

- Anything requiring an attacker who already has the API key, `.env`, or shell access on the
  host.
- Immich's own security. Report those to [immich-app/immich](https://github.com/immich-app/immich).
- Data loss from deliberately enabling `delete_mode: permanent` without a backup. That is
  documented in three places, warned about at every startup, and is a configuration
  decision, not a vulnerability.
