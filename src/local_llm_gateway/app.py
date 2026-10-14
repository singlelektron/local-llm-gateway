"""Small OpenAI-shaped text API; backend protocols live in backends.py."""
import asyncio
from contextlib import aclosing
from datetime import datetime, timezone
import json
import math
import re
import time
import uuid

import aiohttp
from aiohttp import web

from .backends import GatewayError, OllamaBackend
from .config import Config, loopback_host
from .dashboard import render_dashboard
from .storage import Store

CONFIG = web.AppKey("config", Config)
STORE = web.AppKey("store", Store)
BACKENDS = web.AppKey("backends", dict)
ACTIVE = web.AppKey("active", dict)
MAX_BODY = 1024 * 1024
READ_ONLY_PATHS = frozenset({"/", "/healthz", "/v1/models", "/admin/backends", "/admin/stats", "/admin/requests"})


def encode(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def error_body(code):
    return {"error": {"message": code.replace("_", " "), "type": "gateway_error", "code": code}}


def fail(code="invalid_request", status=400):
    raise GatewayError(status, code)


def validate(body, config, client):
    if not isinstance(body, dict) or set(body) - {"model", "messages", "stream", "stream_options", "temperature", "top_p", "max_tokens", "stop", "seed", "backend", "num_ctx", "keep_alive", "model_digest"}:
        fail("unsupported_request_fields")
    if type(body.get("stream", False)) is not bool:
        fail()
    stream = body.get("stream", False)
    stream_options = body.get("stream_options", {})
    if not isinstance(stream_options, dict) or set(stream_options) - {"include_usage"} or type(stream_options.get("include_usage", False)) is not bool or (stream_options and not stream):
        fail()
    requested = body.get("model", config.clients[client]["default_model"])
    backend = body.get("backend")
    if not isinstance(requested, str) or (backend is not None and not isinstance(backend, str)):
        fail()
    if backend is None:
        if requested not in config.models:
            fail("model_not_configured", 404)
        route = config.models[requested]
    else:
        # Explicit selection is limited to administrator configured model pairs.
        route = next((r for r in config.models.values() if r["backend"] == backend and r["model"] == requested), None)
        if route is None:
            fail("model_not_configured", 404)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 256:
        fail()
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] not in ("system", "user", "assistant") or not isinstance(message["content"], str):
            fail("text_messages_required")
    options = {}
    for key, lower, upper in (("num_ctx", 512, 131072), ("keep_alive", 0, 3600)):
        if key in body:
            if type(body[key]) is not int or not lower <= body[key] <= upper:
                fail()
            options[key] = body[key]
    if "model_digest" in body:
        if not isinstance(body["model_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", body["model_digest"]):
            fail()
        options["model_digest"] = body["model_digest"]
    for key, upper in (("temperature", 2), ("top_p", 1)):
        if key in body:
            val = body[key]
            if type(val) not in (float, int) or not 0 <= val <= upper or not math.isfinite(val):
                fail()
            options[key] = val
    if "max_tokens" in body:
        if type(body["max_tokens"]) is not int or not 1 <= body["max_tokens"] <= 32768:
            fail()
        options["num_predict"] = body["max_tokens"]
    if "seed" in body:
        if type(body["seed"]) is not int or not 0 <= body["seed"] < 2**31:
            fail()
        options["seed"] = body["seed"]
    if "stop" in body:
        stop = body["stop"]
        stop = [stop] if isinstance(stop, str) else stop
        if not isinstance(stop, list) or not 1 <= len(stop) <= 4 or not all(isinstance(s, str) and 1 <= len(s) <= 256 for s in stop):
            fail()
        options["stop"] = stop
    return requested, route, messages, options, stream, stream_options.get("include_usage", False)


@web.middleware
async def boundary(request, handler):
    try:
        # Permit direct, top-level viewing of known read-only endpoints. Browser
        # fetches, embedded pages and inference submissions remain unsupported.
        browser_navigation = (
            request.method == "GET"
            and request.path in READ_ONLY_PATHS
            and request.headers.get("Sec-Fetch-Mode") == "navigate"
            and request.headers.get("Sec-Fetch-Dest") == "document"
        )
        fetch_metadata = any(name.lower().startswith("sec-fetch-") for name in request.headers)
        if "Origin" in request.headers or (fetch_metadata and not browser_navigation):
            fail("browser_access_denied", 403)
        try:
            loopback_host(request.url.host)
        except (ValueError, TypeError):
            fail("host_not_local", 403)
        return await handler(request)
    except GatewayError as exc:
        return web.json_response(error_body(exc.code), status=exc.status)
    except web.HTTPException as exc:
        return web.json_response(error_body("http_error"), status=exc.status)
    except (ValueError, TypeError):
        return web.json_response(error_body("invalid_request"), status=400)
    except Exception:
        # Never log upstream exception text, request bodies, URLs or headers.
        return web.json_response(error_body("internal_error"), status=500)


async def chat(request):
    config, store = request.app[CONFIG], request.app[STORE]
    req_id = "chatcmpl-" + uuid.uuid4().hex
    started = time.perf_counter()
    rec = {"id": req_id, "started_at": datetime.now(timezone.utc).isoformat(),
           "status": "in_progress", "request_bytes": 0, "response_bytes": 0, "stream": False}
    store.begin(rec)
    updates = {"status": "failed", "error_category": "internal_error", "request_bytes": 0,
               "response_bytes": 0, "input_tokens": None, "output_tokens": None,
               "total_tokens": None, "token_source": "unavailable"}
    response = None
    sent_content = False
    active = request.app[ACTIVE]
    admitted = False
    created = int(time.time())

    async def write(data):
        await response.write(data)
        updates["response_bytes"] += len(data)

    async def send_json(payload, status=200):
        nonlocal response
        data = encode(payload)
        response = web.StreamResponse(status=status, headers={"Content-Type": "application/json", "Content-Length": str(len(data)), "X-Request-ID": req_id})
        await response.prepare(request)
        await write(data)
        await response.write_eof()
        return response

    def chunk(model, delta, finish=None, usage=None):
        result = {"id": req_id, "object": "chat.completion.chunk", "created": created, "model": model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            result["choices"] = []
            result["usage"] = usage
        return b"data: " + encode(result) + b"\n\n"

    try:
        client = request.headers.get("X-Client-ID")
        if client not in config.clients:
            fail("client_not_configured", 401)
        updates["client_id"] = client
        if request.content_type != "application/json" or request.headers.get("Content-Encoding", "identity") != "identity":
            fail("json_body_required", 415)
        raw = bytearray()
        async for piece in request.content.iter_chunked(65536):
            updates["request_bytes"] += len(piece)
            if len(raw) + len(piece) > MAX_BODY:
                fail("request_too_large", 413)
            raw.extend(piece)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            fail()
        requested, route, messages, options, stream, include_usage = validate(body, config, client)
        updates.update(requested_model=requested, actual_model=route["model"], backend=route["backend"], stream=stream)
        if active["count"] >= config.max_concurrent:
            fail("gateway_busy", 429)
        # Save trusted routing and observed input size before any backend work.
        store.finish(req_id, {k: updates[k] for k in ("client_id", "requested_model", "actual_model", "backend", "stream", "request_bytes")})
        active["count"] += 1
        admitted = True
        backend = request.app[BACKENDS][route["backend"]]
        await backend.verify_local(route["model"], digest=options.pop("model_digest", None))
        content = []
        done = False
        finish_reason = "stop"
        async with aclosing(backend.chat(route["model"], messages, options, stream)) as events:
            async for event in events:
                if event.content and stream and updates.get("ttft_ms") is None:
                    updates["ttft_ms"] = (time.perf_counter() - started) * 1000
                if stream and response is None:
                    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": req_id})
                    await response.prepare(request)
                    await write(chunk(requested, {"role": "assistant"}))
                if event.done:
                    done = True
                    finish_reason = event.finish_reason
                    inp, out = event.input_tokens, event.output_tokens
                    updates.update(input_tokens=inp, output_tokens=out,
                                   total_tokens=inp + out if inp is not None and out is not None else None,
                                   token_source="measured" if inp is not None and out is not None else "partial" if inp is not None or out is not None else "unavailable",
                                   generation_tps=event.generation_tps)
                if event.content:
                    if stream:
                        await write(chunk(requested, {"content": event.content}))
                        sent_content = True
                    else:
                        content.append(event.content)
        if not done:
            fail("upstream_incomplete", 502)
        usage = {"prompt_tokens": updates["input_tokens"], "completion_tokens": updates["output_tokens"], "total_tokens": updates["total_tokens"]}
        if stream:
            await write(chunk(requested, {}, finish_reason))
            if include_usage:
                await write(chunk(requested, {}, usage=usage))
            await write(b"data: [DONE]\n\n")
            await response.write_eof()
        else:
            await send_json({"id": req_id, "object": "chat.completion", "created": created, "model": requested,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(content)}, "finish_reason": finish_reason}], "usage": usage})
        updates.update(status="success", error_category=None)
        return response
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        updates.update(status="interrupted" if active["shutdown"] else "cancelled",
                       error_category="gateway_shutdown" if active["shutdown"] else "client_disconnected")
        raise
    except Exception as exc:
        if isinstance(exc, GatewayError):
            code, status = exc.code, exc.status
        else:
            code, status = "internal_error", 500
        updates.update(status="partial" if sent_content else "failed", error_category=code)
        try:
            if response is not None and response.prepared:
                await write(b"data: " + encode(error_body(code)) + b"\n\n")
                await response.write_eof()
                return response
            return await send_json(error_body(code), status)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            updates.update(status="interrupted" if active["shutdown"] else "cancelled",
                       error_category="gateway_shutdown" if active["shutdown"] else "client_disconnected")
            raise
    finally:
        if admitted:
            active["count"] -= 1
        updates["duration_ms"] = (time.perf_counter() - started) * 1000
        # Short synchronous SQLite transaction cannot be interrupted by cancellation.
        store.finish(req_id, updates)


async def health(request):
    return web.json_response({"status": "ok", "scope": "gateway", "active_requests": request.app[ACTIVE]["count"],
                              "chat_extensions": ["num_ctx", "keep_alive", "model_digest"]})


async def models(request):
    return web.json_response({"object": "list", "data": [{"id": name, "object": "model", "created": 0, "owned_by": "local-gateway"} for name in request.app[CONFIG].models]})


async def backends(request):
    async def inspect(name, adapter):
        try:
            inventory = await adapter.inventory()
            return {"id": name, "healthy": True, **inventory}
        except GatewayError as exc:
            return {"id": name, "healthy": False, "error_category": exc.code}
    return web.json_response({"backends": await asyncio.gather(*(inspect(n, b) for n, b in request.app[BACKENDS].items()))})


def query_filters(request, extra):
    allowed = {"client_id", "requested_model", "actual_model", "backend", "since", "until"}
    if set(request.query) - allowed - extra or any(len(request.query.getall(k)) != 1 for k in request.query):
        fail("invalid_filter")
    return {k: v for k, v in request.query.items() if k in allowed}


async def stats(request):
    filters = query_filters(request, {"group_by"})
    groups = request.query.get("group_by", "").split(",") if request.query.get("group_by") else []
    return web.json_response({"groups": request.app[STORE].aggregate(filters, groups), "timezone": "UTC"})


async def recent(request):
    filters = query_filters(request, {"limit"})
    return web.json_response({"requests": request.app[STORE].recent(filters, int(request.query.get("limit", "50")))})


async def dashboard(request):
    store, config = request.app[STORE], request.app[CONFIG]
    fields = {"client_id", "actual_model", "backend", "since", "until"}
    filters, refresh, error = {}, False, None
    try:
        if set(request.query) - fields - {"refresh"} or any(len(request.query.getall(k)) != 1 for k in request.query):
            raise ValueError
        refresh = request.query.get("refresh") == "1"
        if "refresh" in request.query and not refresh:
            raise ValueError
        filters = {k: v for k, v in request.query.items() if k in fields and v}
        totals = store.aggregate(filters, [])[0]
    except ValueError:
        # Don't reflect arbitrary query/error text into the page or its logs.
        filters, refresh = {}, False
        error = "筛选条件无效。请检查日期范围，并使用页面中的筛选选项重试。"
        totals = store.aggregate({}, [])[0]
    client_history = store.aggregate({}, ["client_id"])
    model_history = store.aggregate({}, ["model", "backend"])
    choices = {
        "client_id": sorted(set(config.clients) | {r["client_id"] for r in client_history if r["client_id"]}),
        "actual_model": sorted({r["model"] for r in config.models.values()} | {r["model"] for r in model_history if r["model"]}),
        "backend": sorted(set(config.backends) | {r["backend"] for r in model_history if r["backend"]}),
    }
    page = render_dashboard(
        totals=totals, clients=store.aggregate(filters, ["client_id"]),
        models=store.aggregate(filters, ["model", "backend"]), daily=store.aggregate(filters, ["date"]),
        recent=store.recent(filters, 30), filters=filters, choices=choices,
        active_requests=request.app[ACTIVE]["count"], now=datetime.now(timezone.utc),
        refresh=refresh, error=error,
    )
    return web.Response(text=page, content_type="text/html", status=400 if error else 200, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    })


def create_app(config):
    app = web.Application(middlewares=[boundary], client_max_size=MAX_BODY, handler_args={"handler_cancellation": True})
    app[CONFIG] = config
    app[ACTIVE] = {"count": 0, "shutdown": False}

    async def shutdown(app):
        app[ACTIVE]["shutdown"] = True

    app.on_shutdown.append(shutdown)

    async def resources(app):
        store = Store(config.database)
        app[STORE] = store
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=config.timeout_seconds, connect=5), auto_decompress=False) as session:
                app[BACKENDS] = {name: OllamaBackend(session, b["url"]) for name, b in config.backends.items()}
                yield
        finally:
            store.close()

    app.cleanup_ctx.append(resources)
    app.add_routes([web.get("/", dashboard), web.get("/healthz", health), web.get("/v1/models", models),
                    web.post("/v1/chat/completions", chat), web.get("/admin/backends", backends),
                    web.get("/admin/stats", stats), web.get("/admin/requests", recent)])
    return app
