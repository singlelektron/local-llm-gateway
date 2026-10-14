# Owner guide

Use [README](../README.md#start) for installation and the first real call.
Use a local model that fits your machine; the gateway does not allocate VRAM or
manage weights. Commands below run from the repository root.

## Inspect routes and usage

```sh
curl --noproxy '*' http://127.0.0.1:8787/v1/models
curl --noproxy '*' http://127.0.0.1:8787/admin/backends
curl --noproxy '*' 'http://127.0.0.1:8787/admin/stats?group_by=client_id,model,backend,date'
curl --noproxy '*' 'http://127.0.0.1:8787/admin/stats?client_id=demo&requested_model=local-chat'
curl --noproxy '*' 'http://127.0.0.1:8787/admin/requests?limit=10'
```

Models lists configured aliases; backend inventory shows installed/loaded names and
backend health. `local` in discovery is a metadata hint, with a stricter `/api/show`
check on every inference request. `/healthz` checks the gateway, not model readiness.
Add aliases/backends/client defaults in the TOML and restart. Every model must map
to an explicitly configured backend. Requests may select an allowed native model
pair using `backend` and `model`; see [API](api.md).

Stats include sample counts: a sum of known tokens is not the complete total when
some requests have no usage. A failed/cancelled request with null usage must remain
unknown. Byte counts are HTTP body bytes at the gateway application boundary,
not Ethernet/TCP/header overhead. `ttft_ms` measures first nonempty visible content,
not hidden reasoning. Dates are UTC, including on computers in other time zones.

## Stop and persistence

Ctrl+C stops a foreground gateway, allowing up to ten seconds for active handlers.
Calls interrupted by forced handler shutdown record `interrupted` / `gateway_shutdown`. No background service is installed automatically.
The database path is relative to the configuration file; default `.data/usage.sqlite3`.
The database contains timing, labels, model names and counts, which can still reveal
usage patterns. The CLI uses private creation permissions. Keep the database and
local config out of Git. There is no automatic retention deletion; inspect disk
usage and archive old databases manually after shutdown.

A dropped client is `cancelled`; a stream that emitted content then lost its backend
is `partial`. An unclean gateway exit leaves a durable initial row; reopening marks
it `interrupted` with `gateway_restart`, unknown final usage/latency and unknown
response bytes. Already persisted input bytes are the last observed count. No retry
is performed. Closing an HTTP connection cannot prove Ollama stopped computing.

## Backup and restore

Stop the gateway first. Use a new backup filename:

```sh
uv run --frozen llm-gateway --config gateway.local.toml backup .data/usage-backup.sqlite3
```

Expect `Backup created`. Existing destinations are refused. This uses SQLite's
consistent backup API; do not copy only an active WAL database's main file.
To inspect a backup without replacing live data:

```sh
cp -n .data/usage-backup.sqlite3 .data/usage-restored.sqlite3
cp -n gateway.local.toml gateway.restore.toml
```

Set `server.database = ".data/usage-restored.sqlite3"` in `gateway.restore.toml`,
then run `uv run --frozen llm-gateway --config gateway.restore.toml serve` and query
stats. Use a fresh restored filename on repeated checks. Keep the original database;
never replace a running database or remove its WAL/SHM files. Automated tests rehearse
backup/reopen and actual process-crash recovery on disposable data.

## Troubleshooting

| Symptom | Check/action |
| --- | --- |
| Startup fails | Validate TOML, installed dependency versions, write permissions, free disk space and port 8787; only one process may own a database. |
| `client_not_configured` | Send an `X-Client-ID` that exists under `[clients]`. |
| `model_not_configured` | Use `/v1/models` alias or an exact configured backend/model pair. |
| `model_not_found` | Inspect Ollama `/api/tags`; configure an existing model. No auto-pull occurs. |
| `route_not_local` | Cloud model, remote metadata or unsupported/unknown local metadata; choose a verified local GGUF model. Do not bypass the check. |
| `backend_unavailable` | Check Ollama service/port and `/admin/backends`; backend redirects/errors are deliberately not relayed. |
| `backend_timeout` | Loading/queueing may exceed `server.timeout_seconds`; inspect Ollama and adjust explicitly. Do not assume timed-out inference stopped. |
| `gateway_busy` | Four active calls by default; wait or explicitly raise `max_concurrent`. |
| `unsupported_request_fields` | Only documented text chat features are implemented. |
| null usage | Backend did not report final counters, or the request did not finish; do not interpret as zero. |

For defense in depth, Ollama supports `OLLAMA_NO_CLOUD=1`; see the
[official FAQ](https://docs.ollama.com/faq#how-do-i-disable-ollamas-cloud-features).
Configure/restart Ollama deliberately if desired; this project does not change the
existing service. Do not expose this unauthenticated MVP through a reverse proxy,
port forwarding or remote bind. Native local clients are supported; browser Origin
requests are refused. Local processes can spoof attribution IDs.
