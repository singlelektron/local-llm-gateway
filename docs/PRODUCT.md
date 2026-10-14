# Product contract

## Purpose and approved scope

Provide lightweight, reusable local model infrastructure on a personal computer,
independent of desktop-companion. Initial target: Arch Linux, an existing Ollama
service, NVIDIA RTX 5080 16 GB and 64 GB system RAM. macOS/M1 Max and alternative
local inference engines are future targets, not verified compatibility claims.

The MVP must provide model discovery/health, configured aliases and client defaults,
explicit route selection, OpenAI-compatible models and text chat endpoints with
streaming, and persistent usage grouped by client/model/backend/UTC date.
Every inference attempt should retain outcome, available backend token counts,
body byte counts, duration, measurable streaming first-content latency and reliable
generation rate. Unknown counts remain null; cancelled and partial requests are
not counted as successful or assigned fabricated usage. No complete stream buffering.

Privacy boundaries: loopback listener and explicit loopback backend allowlist;
clients cannot supply URLs; no cloud fallback even with credentials; verify Ollama
model locality before sending content. Never retain prompts, responses, screenshots,
context, credentials or arbitrary backend errors in statistics/logs. Local model
metadata checks depend on a trusted local Ollama process; the gateway is not an
OS network sandbox for that process. Application consent remains the client's duty.

Non-goals: inference implementation, model weight/driver management, scheduler,
agent platform, memory/personality/tool permissions, remote deployment, cloud billing,
or changes to companion's experimental isolation. No automated download/delete.

## Acceptance

The owner can start the gateway, call an already-installed Ollama model from an
independent client, inspect real usage attributed to that client and model, stop
and restart the gateway, and see the same persisted history. Automated tests cover
ordinary/stream requests, actual Ollama metric field shapes, failures, disconnects,
concurrency, persistence, local/remote boundaries and content exclusion. Tests do
not require model downloads. Documentation distinguishes implemented and future work.
A feature branch and reviewable regular PR are the publication target; no automatic
merge, remote repository creation or GitHub settings changes without authorization.

## Implementation decisions

Python + aiohttp + standard-library SQLite keeps the service small and asynchronous.
FastAPI is viable but adds a validation/server stack unnecessary for this small API;
Go offers a standalone binary but adds implementation/toolchain work for this MVP.
LiteLLM is unnecessary when only native Ollama is supported. SQLite writes are short,
synchronous transactions to ensure cancellation cannot interrupt accounting; this
tradeoff targets personal usage, not high-throughput shared hosting. One server
process owns one database. Default concurrency is four with immediate 429 overload.

CLI and HTTP statistics satisfy the viewing requirement without a frontend.
Client IDs are configured attribution labels, not an authentication mechanism.
Future adapter work must retain local routing and explicit missing-usage semantics.
