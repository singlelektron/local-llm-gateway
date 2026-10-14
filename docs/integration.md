# Client integration

The gateway is independent infrastructure. A client sends text messages and a
configured model alias to `POST /v1/chat/completions`, with its configured
`X-Client-ID` header. The identifier labels accounting; it is not authentication
or authority to send private data elsewhere. Clients never supply backend URLs.
See the [API contract](api.md) for supported fields and errors.

The [independent example](../examples/client.py) uses only Python's standard
library. With the gateway running from the [README](../README.md):

```sh
python3 examples/client.py --client demo --model local-chat
python3 examples/client.py --client second-app --model local-chat --stream
python3 examples/client.py --client desktop-companion --model local-chat
```

Each command prints the actual model reply and reported token usage. Separate
client identifiers produce separate accounting groups. The reply is generated,
so its wording may differ from the instruction. There is no fabricated fallback.
The example accepts only a literal loopback IP, disables environment proxies and
redirects, and makes no retries. Streaming displays text incrementally and fails
if the gateway reports an error or the stream ends without `[DONE]`. Ctrl-C
closes the client connection; it does not prove that the inference engine stopped.

Applications should keep their interface responsive on connection failure, a
non-2xx error, or a partial stream. Do not turn these failures into an implicit
cloud request or an automatic retry of a possibly completed request. Treat
missing token usage as unavailable. The gateway does not own conversation
history, memory, personality, tool permissions, or long-term application state.

## Desktop Companion boundary

Source inspection on 2026-10-14 covered `singlelektron/desktop-companion` at
[`abe9dba613da6db6499f5aedce6ad98a8bfcd476`](https://github.com/singlelektron/desktop-companion/tree/abe9dba613da6db6499f5aedce6ad98a8bfcd476).
Its [README](https://github.com/singlelektron/desktop-companion/blob/abe9dba613da6db6499f5aedce6ad98a8bfcd476/README.md)
now describes a bounded GTK prototype using the YuriOS experiment. It is not a
production application or a permanent foundation decision. Its
[PRODUCT.md](https://github.com/singlelektron/desktop-companion/blob/abe9dba613da6db6499f5aedce6ad98a8bfcd476/PRODUCT.md)
requires owner agreement for external inference, and its
[AGENTS.md](https://github.com/singlelektron/desktop-companion/blob/abe9dba613da6db6499f5aedce6ad98a8bfcd476/AGENTS.md)
keeps implementation evidence, acceptance, and authorization distinct.

The experiment's
[sandbox](https://github.com/singlelektron/desktop-companion/blob/abe9dba613da6db6499f5aedce6ad98a8bfcd476/experiments/yurios_trial/sandbox.py)
removes direct network access and exposes a private Unix socket. Its
[broker](https://github.com/singlelektron/desktop-companion/blob/abe9dba613da6db6499f5aedce6ad98a8bfcd476/experiments/yurios_trial/broker.py)
owns fixed model settings, model identity verification, retained allowances, and
completion uncertainty. The
[prototype runtime](https://github.com/singlelektron/desktop-companion/blob/abe9dba613da6db6499f5aedce6ad98a8bfcd476/prototype/runtime.py)
drains submitted inference during shutdown before releasing that broker. That
broker also mediates permitted public reads, so it is not a generic inference
gateway and cannot simply be replaced with this HTTP service.

This MVP provides a runnable client contract without modifying those safeguards.
A separate companion integration should retain its sandbox and private socket,
and adapt only the trusted host broker's inference transport. Before making that
change, test model identity and fixed generation settings, retained budgets,
failure receipts, cancellation, shutdown drain, and recovery when backend
completion is unknown. A closed HTTP connection is not evidence of a completed
backend operation. The current API is not a drop-in replacement for the broker's
native Ollama lifecycle checks.

Companion remains responsible for approving the context it sends. Gateway route
configuration and an API key cannot satisfy companion's external-inference
consent requirement. This gateway MVP has no cloud route or cloud fallback;
future provider support would need an explicit separate authorization design.
