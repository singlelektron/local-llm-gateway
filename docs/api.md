# API and accounting reference

Base URL: `http://127.0.0.1:8787`. This is a documented subset of OpenAI chat,
not full OpenAI SDK/API parity. Errors use `{"error":{"message":...,"type":"gateway_error","code":...}}`.
Error messages are fixed categories, never upstream text. A chat response includes
`X-Request-ID`, also stored as `id`. Authorization headers are neither used nor
forwarded. No client-controlled upstream headers or URLs are accepted.

Browsers may navigate directly to the read-only GET endpoints below. Browser
fetch/XHR, iframe navigation, Origin-bearing requests and inference submissions
remain refused; no CORS access is enabled. Native HTTP clients remain supported.

## Endpoints

- `GET /`: local HTML usage dashboard, with optional `client_id`, `actual_model`,
  `backend`, `since`, `until`, and `refresh=1` (15-second page reload). Empty filter
  values are ignored. Invalid/duplicate/unknown query parameters return HTML 400
  with a validation message. No model call or statistics write occurs.
- `GET /healthz`: gateway liveness, active request count and supported `chat_extensions`.
- `GET /v1/models`: configured model aliases (`object: list`). Does not trigger inference.
- `POST /v1/chat/completions`: requires JSON and configured `X-Client-ID`.
- `GET /admin/backends`: configured backend IDs, health, installed names/local hints and
  SHA-256 digests (null when absent or invalid),
  loaded model names. Health/discovery calls are not inference usage records.
- `GET /admin/stats`: totals or `group_by=client_id,model,backend,date` in any combination.
- `GET /admin/requests`: recent records; `limit=1..1000`, default 50.

Stats/recent filters: `client_id`, `requested_model` (alias or explicit native name),
`actual_model`, `backend`, `since`, `until`. Date bounds are inclusive `YYYY-MM-DD`
in UTC. Unknown query fields/duplicate values/groups return 400. `model` grouping
means actual configured native model; add backend grouping to distinguish engines.

## Text chat contract

```sh
curl --noproxy '*' http://127.0.0.1:8787/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-Client-ID: demo' \
  -d '{"model":"local-chat","messages":[{"role":"user","content":"Say hello."}],"max_tokens":32}'
```

`model` may be omitted to use that client's configured default. Supported roles:
`system`, `user`, `assistant`, each with string `content`. Options: `stream` boolean;
`temperature` 0..2; `top_p` 0..1; `max_tokens` 1..32768 (Ollama `num_predict`);
`seed` 0..2147483647; `stop` string or 1..4 strings, each 1..256 characters. Requests are bounded to 1 MiB
and 256 messages. Unknown fields, multimodal content, tools and other features fail.
Ollama thinking is disabled explicitly; hidden reasoning is not an exposed feature.

Optional local extensions preserve a client's bounded inference configuration:
`num_ctx` is an integer context size from 512 to 131072; `keep_alive` is an integer
residency duration in seconds from 0 to 3600. Omission preserves the backend's
defaults. `model_digest` is a lowercase 64-character SHA-256 digest; when supplied,
the selected backend's `/api/tags` must match it immediately before the positive
locality check and inference. A mismatch returns 409 `model_identity_changed`
without sending the messages to inference. This checks a trusted daemon, not an
atomic lock against concurrent model replacement. These fields do not accept
arbitrary Ollama options or authorize downloading/loading an absent model.

For manual route selection, use the extension `"backend":"ollama"` together with
`"model":"qwen2.5-coder:14b-instruct-q6_K"`. That exact pair must occur in the
administrator's model configuration. A discovered model is not automatically authorized.

Nonstream responses contain `chat.completion`, one assistant choice, a `stop` or
`length` finish reason, and `usage`. Stream responses use SSE `data:` JSON chunks
with `chat.completion.chunk`, followed by `[DONE]`. `stream_options.include_usage`
adds one final usage chunk with empty choices; intermediate chunks do not carry
repeated usage. Nullable usage counters are a deliberate extension when unknown.
A midstream failure emits a sanitized error event and ends **without `[DONE]`**;
clients must treat that as incomplete. A disconnect cannot receive an error event.
Before SSE headers, failures retain their ordinary HTTP status.

## Accounting semantics

SQLite stores only enumerated metadata. No prompts, replies, free-form request
fields, unknown client/model labels, URLs, keys, headers or backend error bodies
are retained. Invalid requests have null untrusted labels. IDs are attribution,
not authorization between users. Access/request logging is disabled in the CLI.

| Measurement | Definition |
| --- | --- |
| `input_tokens`, `output_tokens` | Native final `prompt_eval_count`, `eval_count`; never accumulated from intermediate frames. Counts above 2^31-1 are treated as invalid/unavailable. |
| `total_tokens` | Sum only when both real counters are available. |
| `token_source` | `measured`, `partial` (only one counter available), or `unavailable`; no estimator is implemented. |
| `request_bytes` | Client HTTP body bytes read by the gateway, including JSON formatting; does not include headers or upstream metadata traffic. |
| `response_bytes` | JSON/SSE body bytes successfully handed to the client transport, including SSE framing. Does not prove peer receipt or count wire overhead. Unknown after process crash. |
| `duration_ms` | Gateway monotonic elapsed time including request parsing, locality check, inference and response writing; excludes final database commit. |
| `ttft_ms` | Streaming time to first nonempty content observed from the backend, including locality check/queue/load; null for nonstream or no content. |
| `generation_tps` | Final `eval_count / (eval_duration / 1e9)` if valid; backend generation speed, not end-to-end throughput. |
| `status` | `in_progress`, `success`, `failed`, `cancelled`, `partial`, or `interrupted` after restart/server shutdown. |

A completed backend's metrics may exist even if final downstream delivery was
cancelled; retain those measured metrics once, with the delivery outcome. Cancelled
requests lacking final metrics have unknown usage, not an estimate of generated
text. Interrupted rows retain only pre-crash evidence. Normal restart retains
completed history. DB writes fail closed before starting inference if accounting
cannot begin; disk failure after inference may leave an interrupted row.

Aggregations return known sums plus `*_token_samples`, status counts, byte sample
counts and duration coverage. Missing-only sums remain null. This is not billing.

## Implementation and extension

`config.py` owns allowed routes and literal loopback URL validation. `app.py` owns
HTTP validation/formatting and lifecycle accounting. `backends.py` defines a small
`Backend` protocol (`inventory`, `verify_local`, `chat`) and backend-neutral `Event`.
`storage.py` owns the SQLite schema, exclusive process ownership and recovery.
One aiohttp event loop/pool supports bounded concurrent calls; streams are forwarded
incrementally with write backpressure and bounded NDJSON lines.

To add a backend, implement/test that protocol, its locality verification, mapping
and usage evidence, then explicitly register the new configuration type. Future
OpenAI-compatible adapters must cope with absent terminal usage, cancellation and
incomplete SSE; do not claim measurement from estimated chunks. MLX/llama.cpp and
cloud adapters are not present. The trusted Ollama daemon must not be malicious or
concurrently reconfigured to reroute a model between its metadata check and call.
For a stronger guarantee, deny daemon egress at the OS level; the gateway cannot
prove where a deliberately deceptive backend computes.

Sources: [Ollama chat fields](https://docs.ollama.com/api/chat),
[Ollama routing metadata types](https://github.com/ollama/ollama/blob/main/api/types.go),
[aiohttp disconnect lifecycle](https://docs.aiohttp.org/en/stable/web_advanced.html#peer-disconnection).
