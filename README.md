# YouTube MCP

A Python MCP server for YouTube publishing, built from scratch under
[portfolio-ops #285](https://github.com/estesadvisory/portfolio-ops/issues/285).
MIT licensed. Slice 1 supplies a transport shell and a `health` tool; Slice 2
adds an offline profile contract and credential-provider interface. There is no
YouTube client or publishing operation yet.

Requires Python 3.11 or newer.

```sh
pip install .
youtube-mcp                         # stdio (default)
python -m youtube_mcp               # equivalent
youtube-mcp --transport http        # http://127.0.0.1:8000/mcp
```

Both modes construct the same FastMCP registry. `health()` returns
`{"status": "ok"}` without contacting Google. Startup needs no credentials.
Logs go to stderr; stdout is reserved for the stdio MCP protocol.
`http` means Streamable HTTP, not the legacy SSE transport.

HTTP accepts only literal loopback addresses (`127.0.0.1` by default; `::1`
is also supported). Wildcards, hostnames, and non-loopback addresses are rejected
before startup. Slice 1 has no caller-auth policy implementation or bypass flag,
so remote binding is always refused. Loopback has no caller authentication: use
only on a trusted local machine. Google OAuth will authorize Google operations;
it will not authenticate MCP callers. Future remote hosting needs a separate,
reviewed caller-auth policy. Do not expose this process through a proxy or tunnel.
The supported entry points enforce the bind guard; importing `create_server()`
is an embedding API, and an embedder owns its listener and access policy.

Development:

```sh
uv sync --locked --group dev
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

CI repeats lint and subprocess transport tests on Python 3.11 and 3.13 and runs
Gitleaks against full git history. Tests start only local processes/listeners;
no live Google service or credentials are used. Private hosting, IaC, Google
setup, OAuth, and secret-store wiring are outside this repository slice.

Slice 2 adds the offline core contract in `youtube_mcp.profiles`. Construct a
`Profile` with `profile_id`, pinned `channel_id`, display `label`,
`granted_scopes` (full OAuth scope URLs), and an opaque `credential_ref`.
`ProfileStore.resolve(profile_id)` requires an exact explicit id and the
`https://www.googleapis.com/auth/youtube.force-ssl` grant. Labels/channel titles
never select a profile; a missing id never selects a default, even with one
configured profile. Duplicate ids are rejected. Scope sets are immutable snapshots.

The record contains no token fields. Keep token bytes outside profiles behind the
`CredentialProvider` protocol; `InMemoryCredentialProvider` is an offline fake
returning a redacted-repr `Credential`. Profile resolution returns only the pinned
record and does not call the provider. Adapters must use safe fixed-message errors
and omit credentials, references, and upstream error chains from diagnostics.
Redacted repr is defensive only: never serialize/log credential values or profile
records wholesale. References are opaque lookup keys, not credential material.
This slice adds no MCP tools, persistence, OAuth, 1Password, or Google calls.
