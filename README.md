# Local LLM Gateway

A small, independent local inference gateway for multiple applications. It exposes
an OpenAI-compatible **text chat subset**, routes administrator-defined model
aliases to Ollama, and records durable, content-free usage in SQLite.

Python 3.12+; one runtime dependency (`aiohttp`, with its transitive dependencies).
No model engine, agent framework, frontend build, cloud fallback, or telemetry.

## Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed.
From this repository:

```sh
uv sync --frozen
cp -n gateway.example.toml gateway.local.toml
curl --noproxy '*' http://127.0.0.1:11434/api/tags
```

Edit `gateway.local.toml`: set `models.local-chat.model` to an **already installed
local model** from the response. The example matches the original Arch machine's
`qwen2.5-coder:14b-instruct-q6_K`; another computer must select its own model.
Nothing is downloaded automatically.

```sh
uv run --frozen llm-gateway --config gateway.local.toml serve
```

Expect a foreground listener at `http://127.0.0.1:8787`. Open that address in your
browser for the usage dashboard: filter by client, model, backend and UTC date,
inspect recent failures/performance, and optionally refresh every 15 seconds.
It uses only real SQLite records, with no external assets or frontend build.
In another terminal:

```sh
curl --noproxy '*' http://127.0.0.1:8787/healthz
python3 examples/client.py --client demo --model local-chat
python3 examples/client.py --client second-app --model local-chat --stream
curl --noproxy '*' 'http://127.0.0.1:8787/admin/stats?group_by=client_id,model'
curl --noproxy '*' 'http://127.0.0.1:8787/admin/requests?limit=5'
```

Expect `status: ok`, real model responses, two client groups, and backend-reported
token counts. Stop with **Ctrl+C**, run the server again, and repeat the statistics
query: the records remain in `.data/usage.sqlite3`. Model generation may be slow on
the first request while Ollama loads the model.

## Scope

Implemented: models/aliases/client defaults, explicit configured backend/model
selection, native Ollama model discovery and local checks, nonstream and SSE chat,
cancellation/error accounting, request/response body byte measurements, real token
and generation-rate parsing, a local usage dashboard, filtered statistics,
restart recovery and backups.

Unsupported: tools/function calls, images/audio, embeddings, Responses API, remote
backends, authentication between local users, cloud providers/costs, automatic
model downloads/loading controls, and MLX/llama.cpp adapters.
Unsupported request fields fail explicitly. The backend protocol is ready for
small independently tested adapters; compatibility with other engines is not claimed.

- [Operation, verification and troubleshooting](docs/OWNER_GUIDE.md)
- [API, accounting semantics and adapter contract](docs/api.md)
- [desktop-companion integration boundary](docs/integration.md)
- [Product contract](docs/PRODUCT.md) · [Development instructions](AGENTS.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

Run offline model-free tests with `uv run --frozen python -m unittest discover -s tests -v`.
Tests use local ephemeral HTTP servers; they do not download models.
