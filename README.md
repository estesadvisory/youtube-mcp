# YouTube MCP

A Python MCP server for YouTube publishing, built from scratch under
[portfolio-ops #285](https://github.com/estesadvisory/portfolio-ops/issues/285).
MIT licensed. Slice 1 supplies only a transport shell and a `health` tool.
There is no YouTube client, credential provider, or publishing operation yet.

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
