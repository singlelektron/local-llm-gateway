"""Small backend interface and a native Ollama implementation.

Only configured backends construct this adapter. No client-provided URL, cloud
fallback, model download, or request-content logging belongs in this layer.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_TOKEN_COUNT = 2**31 - 1
MAX_DURATION_NS = 2**63 - 1

_MESSAGES = {
    "route_not_local": "The configured model could not be verified as local.",
    "model_not_found": "The configured model is not installed on the backend.",
    "backend_unavailable": "The configured inference backend is unavailable.",
    "backend_timeout": "The inference backend timed out.",
    "backend_protocol_error": "The inference backend returned an invalid response.",
    "backend_response_too_large": "The inference backend exceeded a response limit.",
}


class GatewayError(Exception):
    """An intentionally sanitized error suitable for HTTP and usage records."""

    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        self.message = _MESSAGES.get(code, "The gateway could not complete the request.")
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class Event:
    content: str = ""
    done: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    generation_tps: float | None = None
    finish_reason: str = "stop"


class Backend(Protocol):
    async def inventory(self) -> dict[str, Any]: ...

    async def verify_local(self, model: str) -> None: ...

    def chat(
        self, model: str, messages: list, options: dict, stream: bool
    ) -> AsyncIterator[Event]: ...


def _cloud_name(name: str) -> bool:
    name = name.lower()
    return name.endswith("cloud") or ":cloud" in name or "-cloud" in name


def _remote_metadata(value: dict) -> bool:
    # Inspect routing metadata, never prompts/templates or arbitrary text values.
    for metadata in (value, value.get("details"), value.get("model_info")):
        if not isinstance(metadata, dict):
            continue
        if any(metadata.get(key) for key in ("remote_model", "remote_host", "cloud")):
            return True
        if any(
            metadata.get(key) in ("cloud", "remote")
            for key in ("format", "general.architecture", "location", "provider")
        ):
            return True
    return False


def _nonnegative_integer(value: Any, maximum: int = MAX_TOKEN_COUNT) -> int | None:
    # Token counts above this generous per-request cap are malformed/unavailable,
    # not zero. Their combined total must fit SQLite's signed 64-bit integer.
    # Nanosecond durations retain their wider range; bool is always excluded.
    return value if type(value) is int and 0 <= value <= maximum else None


def _decode_json(raw: bytes | bytearray) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise GatewayError(502, "backend_protocol_error") from None
    if not isinstance(value, dict):
        raise GatewayError(502, "backend_protocol_error")
    return value


def _event(value: dict) -> Event:
    if "error" in value:
        raise GatewayError(502, "backend_unavailable")
    if _remote_metadata(value):
        raise GatewayError(403, "route_not_local")
    done = value.get("done")
    if type(done) is not bool:
        raise GatewayError(502, "backend_protocol_error")
    message = value.get("message", {} if done else None)
    if not isinstance(message, dict):
        raise GatewayError(502, "backend_protocol_error")
    content = message.get("content", "")
    if not isinstance(content, str) or message.get("tool_calls"):
        raise GatewayError(502, "backend_protocol_error")
    if not done:
        # Some servers repeat counters on chunks. Only the terminal counters
        # represent authoritative request totals.
        return Event(content=content)
    input_tokens = _nonnegative_integer(value.get("prompt_eval_count"))
    output_tokens = _nonnegative_integer(value.get("eval_count"))
    duration = _nonnegative_integer(value.get("eval_duration"), MAX_DURATION_NS)
    speed = None
    if output_tokens is not None and duration is not None and duration > 0:
        speed = output_tokens / duration * 1_000_000_000
        if not math.isfinite(speed):
            speed = None
    reason = value.get("done_reason", "stop")
    if reason not in ("stop", "length"):
        raise GatewayError(502, "backend_protocol_error")
    return Event(content, True, input_tokens, output_tokens, speed, reason)


class OllamaBackend:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        self.session = session
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _check_status(response: aiohttp.ClientResponse) -> None:
        if response.status == 404:
            raise GatewayError(404, "model_not_found")
        if response.status != 200:
            # Do not read or relay backend errors: they may echo private input
            # or include credentials/URLs. Redirects are never followed.
            raise GatewayError(502, "backend_unavailable")

    @staticmethod
    async def _json_response(response: aiohttp.ClientResponse) -> dict:
        if response.content_length is not None and response.content_length > MAX_JSON_BYTES:
            raise GatewayError(502, "backend_response_too_large")
        raw = bytearray()
        async for chunk in response.content.iter_chunked(64 * 1024):
            raw.extend(chunk)
            if len(raw) > MAX_JSON_BYTES:
                raise GatewayError(502, "backend_response_too_large")
        return _decode_json(raw)

    async def _request_json(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            async with self.session.request(
                method, self.base_url + path, json=body, allow_redirects=False
            ) as response:
                self._check_status(response)
                value = await self._json_response(response)
                if "error" in value:
                    raise GatewayError(502, "backend_unavailable")
                return value
        except asyncio.TimeoutError:
            raise GatewayError(504, "backend_timeout") from None
        except aiohttp.ClientError:
            raise GatewayError(502, "backend_unavailable") from None

    async def verify_local(self, model: str) -> None:
        if _cloud_name(model):
            raise GatewayError(403, "route_not_local")
        metadata = await self._request_json("POST", "/api/show", {"model": model})
        details, info = metadata.get("details"), metadata.get("model_info")
        if (
            _remote_metadata(metadata)
            or not isinstance(details, dict)
            or details.get("format") != "gguf"
            or not isinstance(info, dict)
            or not isinstance(info.get("general.architecture"), str)
            or not info["general.architecture"].strip()
        ):
            raise GatewayError(403, "route_not_local")

    async def inventory(self) -> dict[str, Any]:
        tags = await self._request_json("GET", "/api/tags")
        running = await self._request_json("GET", "/api/ps")
        installed, loaded = [], []
        for data, destination in ((tags, installed), (running, loaded)):
            models = data.get("models")
            if not isinstance(models, list):
                raise GatewayError(502, "backend_protocol_error")
            for model in models:
                if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                    raise GatewayError(502, "backend_protocol_error")
                name = model["name"]
                if destination is loaded:
                    destination.append(name)
                else:
                    details = model.get("details")
                    destination.append(
                        {
                            "name": name,
                            # Informational discovery only. Requests must still
                            # pass /api/show immediately before inference.
                            "local": not _cloud_name(name)
                            and not _remote_metadata(model)
                            and isinstance(details, dict)
                            and details.get("format") == "gguf",
                        }
                    )
        return {"installed": installed, "loaded": loaded}

    async def chat(
        self, model: str, messages: list, options: dict, stream: bool
    ) -> AsyncIterator[Event]:
        body = {
            "model": model,
            "messages": messages,
            "options": options,
            "stream": stream,
            "think": False,
        }
        try:
            async with self.session.post(
                self.base_url + "/api/chat", json=body, allow_redirects=False
            ) as response:
                self._check_status(response)
                if not stream:
                    event = _event(await self._json_response(response))
                    if not event.done:
                        raise GatewayError(502, "backend_protocol_error")
                    yield event
                    return
                pending = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    pending.extend(chunk)
                    while (newline := pending.find(b"\n")) >= 0:
                        if newline > MAX_LINE_BYTES:
                            raise GatewayError(502, "backend_response_too_large")
                        line = pending[:newline]
                        del pending[: newline + 1]
                        if not line.strip():
                            continue
                        event = _event(_decode_json(line))
                        yield event
                        if event.done:
                            return
                    if len(pending) > MAX_LINE_BYTES:
                        raise GatewayError(502, "backend_response_too_large")
                if pending.strip():
                    event = _event(_decode_json(pending))
                    yield event
                    if event.done:
                        return
                raise GatewayError(502, "backend_protocol_error")
        except asyncio.TimeoutError:
            raise GatewayError(504, "backend_timeout") from None
        except aiohttp.ClientError:
            raise GatewayError(502, "backend_unavailable") from None
