"""Native Ollama protocol tests against an actual local HTTP test server."""

import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import aiohttp
from aiohttp import web

from local_llm_gateway.backends import Event, GatewayError, OllamaBackend


LOCAL_METADATA = {
    "details": {"format": "gguf"},
    "model_info": {"general.architecture": "llama"},
}


class OllamaBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.handler = None
        self.payloads = {
            "/api/show": LOCAL_METADATA,
            "/api/tags": {"models": []},
            "/api/ps": {"models": []},
            "/api/chat": {
                "message": {"role": "assistant", "content": "hello"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 7,
                "eval_count": 4,
                "eval_duration": 200_000_000,
            },
        }
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.session = aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=2)
        )
        self.backend = OllamaBackend(self.session, self.url)

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()

    async def handle(self, request):
        body = await request.json() if request.can_read_body else None
        self.requests.append((request.path, body))
        if self.handler:
            return await self.handler(request)
        return web.json_response(self.payloads[request.path])

    async def collect(self, stream=False, backend=None):
        return [
            event
            async for event in (backend or self.backend).chat(
                "small:latest",
                [{"role": "user", "content": "sensitive test prompt"}],
                {"num_predict": 4},
                stream,
            )
        ]

    async def test_nonstream_native_usage_and_options(self):
        result = await self.collect()
        self.assertEqual(result, [Event("hello", True, 7, 4, 20.0, "stop")])
        path, body = self.requests[0]
        self.assertEqual(path, "/api/chat")
        self.assertEqual(body["options"], {"num_predict": 4})
        self.assertFalse(body["stream"])
        self.assertIs(body["think"], False)

    async def test_missing_metrics_remain_unavailable(self):
        self.payloads["/api/chat"] = {"message": {"content": "hi"}, "done": True}
        result = (await self.collect())[0]
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        self.assertIsNone(result.generation_tps)

    async def test_zero_counts_are_real_and_zero_duration_is_unavailable(self):
        self.payloads["/api/chat"].update(
            prompt_eval_count=0, eval_count=0, eval_duration=0
        )
        result = (await self.collect())[0]
        self.assertEqual(result.input_tokens, 0)
        self.assertEqual(result.output_tokens, 0)
        self.assertIsNone(result.generation_tps)

    async def test_invalid_metrics_are_not_coerced_to_counts(self):
        for invalid in (True, -1, 2.5, "4", None, 2**80):
            with self.subTest(invalid=invalid):
                self.payloads["/api/chat"].update(
                    prompt_eval_count=invalid, eval_count=invalid, eval_duration=invalid
                )
                event = (await self.collect())[0]
                self.assertIsNone(event.input_tokens)
                self.assertIsNone(event.output_tokens)
                self.assertIsNone(event.generation_tps)

    async def test_maximum_token_counts_have_a_sqlite_safe_combined_total(self):
        maximum = 2**31 - 1
        self.payloads["/api/chat"].update(prompt_eval_count=maximum, eval_count=maximum)
        event = (await self.collect())[0]
        self.assertEqual(event.input_tokens, maximum)
        self.assertEqual(event.output_tokens, maximum)
        total = event.input_tokens + event.output_tokens
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute("CREATE TABLE counts (input INTEGER, output INTEGER, total INTEGER)")
            database.execute("INSERT INTO counts VALUES (?, ?, ?)", (maximum, maximum, total))
            self.assertEqual(
                database.execute("SELECT input, output, total FROM counts").fetchone(),
                (maximum, maximum, 2**32 - 2),
            )

    async def test_token_counts_above_cap_are_unavailable(self):
        self.payloads["/api/chat"].update(prompt_eval_count=2**31, eval_count=2**31)
        event = (await self.collect())[0]
        self.assertIsNone(event.input_tokens)
        self.assertIsNone(event.output_tokens)
        self.assertIsNone(event.generation_tps)

    async def test_duration_retains_signed_64_bit_nanosecond_range(self):
        self.payloads["/api/chat"]["eval_duration"] = 2**63 - 1
        event = (await self.collect())[0]
        self.assertEqual(event.generation_tps, 4 / (2**63 - 1) * 1_000_000_000)
        self.payloads["/api/chat"]["eval_duration"] = 2**63
        event = (await self.collect())[0]
        self.assertEqual(event.output_tokens, 4)
        self.assertIsNone(event.generation_tps)

    async def test_stream_delivers_before_generation_finishes(self):
        release = asyncio.Event()

        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
            await response.prepare(request)
            # Counters on intermediate frames must not be accumulated.
            await response.write(
                b'{"message":{"content":"first"},"done":false,"eval_count":900}\n'
            )
            await release.wait()
            await response.write(json.dumps(self.payloads["/api/chat"]).encode() + b"\n")
            await response.write_eof()
            return response

        self.handler = stream
        iterator = self.backend.chat("small", [], {}, True)
        try:
            event = await asyncio.wait_for(anext(iterator), timeout=1)
            self.assertEqual(event, Event("first"))
            self.assertFalse(release.is_set())
            release.set()
            terminal = await anext(iterator)
            self.assertEqual(terminal.output_tokens, 4)
            self.assertEqual(terminal.input_tokens, 7)
            with self.assertRaises(StopAsyncIteration):
                await anext(iterator)
        finally:
            release.set()
            await iterator.aclose()

    async def test_stream_fragmented_utf8_and_unterminated_final_line(self):
        raw = (
            json.dumps({"message": {"content": "你好"}, "done": False}, ensure_ascii=False)
            + "\n\n"
            + json.dumps(self.payloads["/api/chat"])
        ).encode()

        async def fragmented(request):
            response = web.StreamResponse()
            await response.prepare(request)
            for offset in range(0, len(raw), 3):
                await response.write(raw[offset : offset + 3])
            return response

        self.handler = fragmented
        result = await self.collect(stream=True)
        self.assertEqual([e.content for e in result], ["你好", "hello"])
        self.assertEqual(sum(e.output_tokens or 0 for e in result), 4)

    async def test_duplicate_terminal_frame_is_not_counted_twice(self):
        raw = (json.dumps(self.payloads["/api/chat"]) + "\n") * 2

        async def duplicate(request):
            return web.Response(text=raw)

        self.handler = duplicate
        self.assertEqual(len(await self.collect(stream=True)), 1)

    async def test_missing_stream_terminal_is_protocol_failure(self):
        async def truncated(request):
            return web.Response(text='{"message":{"content":"partial"},"done":false}\n')

        self.handler = truncated
        iterator = self.backend.chat("small", [], {}, True)
        self.assertEqual((await anext(iterator)).content, "partial")
        with self.assertRaises(GatewayError) as caught:
            await anext(iterator)
        self.assertEqual(caught.exception.code, "backend_protocol_error")

    async def test_nonstream_requires_terminal_response(self):
        self.payloads["/api/chat"]["done"] = False
        with self.assertRaises(GatewayError) as caught:
            await self.collect()
        self.assertEqual(caught.exception.code, "backend_protocol_error")

    async def test_malformed_frames_are_sanitized(self):
        for raw in (
            b"not json secret",
            b"[]",
            b'{"done":1,"message":{"content":"secret"}}',
            b'{"done":true,"message":{"content":["secret"]}}',
            b'{"done":false}',
            b'{"done":true,"message":{"tool_calls":[{}]}}',
        ):
            with self.subTest(raw=raw):
                async def malformed(request):
                    return web.Response(body=raw)
                self.handler = malformed
                with self.assertRaises(GatewayError) as caught:
                    await self.collect()
                self.assertEqual(caught.exception.code, "backend_protocol_error")
                self.assertNotIn("secret", str(caught.exception))

    async def test_upstream_errors_never_echo_private_response(self):
        for status in (200, 401, 404, 500):
            with self.subTest(status=status):
                async def failure(request):
                    return web.json_response(
                        {"error": "secret prompt, Authorization: Bearer private-key"}, status=status
                    )
                self.handler = failure
                with self.assertRaises(GatewayError) as caught:
                    await self.collect()
                self.assertEqual(caught.exception.status, 404 if status == 404 else 502)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("private-key", str(caught.exception))

    async def test_timeout_is_classified(self):
        async def slow(request):
            await asyncio.sleep(0.08)
            return web.json_response(self.payloads["/api/chat"])
        self.handler = slow
        async with aiohttp.ClientSession(
            trust_env=False, timeout=aiohttp.ClientTimeout(total=0.01)
        ) as session:
            with self.assertRaises(GatewayError) as caught:
                await self.collect(backend=OllamaBackend(session, self.url))
        self.assertEqual(caught.exception.code, "backend_timeout")
        self.assertEqual(caught.exception.status, 504)

    async def test_connection_failure_is_classified(self):
        await self.site.stop()
        with self.assertRaises(GatewayError) as caught:
            await self.collect()
        self.assertEqual(caught.exception.code, "backend_unavailable")

    async def test_redirects_are_never_followed(self):
        async def redirect(request):
            return web.Response(status=307, headers={"Location": self.url + "/redirect-target"})
        self.handler = redirect
        with self.assertRaises(GatewayError):
            await self.collect()
        with self.assertRaises(GatewayError):
            await self.backend.verify_local("small")
        with self.assertRaises(GatewayError):
            await self.backend.inventory()
        self.assertNotIn("/redirect-target", [path for path, _ in self.requests])

    async def test_local_metadata_required_before_inference(self):
        await self.backend.verify_local("small")
        self.assertEqual(self.requests, [("/api/show", {"model": "small"})])

    async def test_cloud_names_rejected_without_network(self):
        for model in ("foo-cloud", "foo:cloud", "foo:cloud-tag", "foo-cloud:latest", "cloud"):
            with self.subTest(model=model), self.assertRaises(GatewayError) as caught:
                await self.backend.verify_local(model)
            self.assertEqual(caught.exception.code, "route_not_local")
        self.assertEqual(self.requests, [])

    async def test_renamed_cloud_and_unknown_metadata_fail_closed(self):
        for metadata in (
            {},
            {"details": {"format": "gguf"}},
            {"model_info": {"general.architecture": "llama"}},
            {**LOCAL_METADATA, "remote_host": "https://cloud.invalid"},
            {**LOCAL_METADATA, "remote_model": "remote"},
            {**LOCAL_METADATA, "cloud": True},
            {**LOCAL_METADATA, "model_info": {"general.architecture": ""}},
            {**LOCAL_METADATA, "model_info": {"general.architecture": "cloud"}},
        ):
            with self.subTest(metadata=metadata):
                self.payloads["/api/show"] = metadata
                with self.assertRaises(GatewayError) as caught:
                    await self.backend.verify_local("innocent-alias")
                self.assertEqual(caught.exception.code, "route_not_local")
        self.assertNotIn("/api/chat", [path for path, _ in self.requests])

    async def test_remote_response_metadata_is_rejected_before_content(self):
        self.payloads["/api/chat"]["remote_host"] = "https://remote.invalid"
        with self.assertRaises(GatewayError) as caught:
            await self.collect()
        self.assertEqual(caught.exception.code, "route_not_local")

    async def test_inventory_selects_only_safe_fields(self):
        self.payloads["/api/tags"]["models"] = [
            {"name": "small", "details": {"format": "gguf"}, "template": "private"},
            {"name": "renamed", "remote_host": "https://remote.invalid", "details": {"format": "gguf"}},
            {"name": "large:cloud", "details": {"format": "gguf"}},
            {"name": "unverified"},
        ]
        self.payloads["/api/ps"]["models"] = [{"name": "small", "irrelevant": "private"}]
        self.assertEqual(
            await self.backend.inventory(),
            {
                "installed": [
                    {"name": "small", "local": True},
                    {"name": "renamed", "local": False},
                    {"name": "large:cloud", "local": False},
                    {"name": "unverified", "local": False},
                ],
                "loaded": ["small"],
            },
        )

    async def test_json_and_line_response_limits(self):
        async def oversized(request):
            return web.Response(body=b"x" * 1025)
        self.handler = oversized
        with patch("local_llm_gateway.backends.MAX_JSON_BYTES", 1024):
            for operation in (self.collect, lambda: self.backend.verify_local("small")):
                with self.assertRaises(GatewayError) as caught:
                    await operation()
                self.assertEqual(caught.exception.code, "backend_response_too_large")
        with patch("local_llm_gateway.backends.MAX_LINE_BYTES", 1024):
            with self.assertRaises(GatewayError) as caught:
                await self.collect(stream=True)
            self.assertEqual(caught.exception.code, "backend_response_too_large")

    async def test_chunked_json_limit_without_content_length(self):
        async def oversized(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"x" * 1025)
            return response
        self.handler = oversized
        with patch("local_llm_gateway.backends.MAX_JSON_BYTES", 1024):
            with self.assertRaises(GatewayError) as caught:
                await self.collect()
            self.assertEqual(caught.exception.code, "backend_response_too_large")

    async def test_closing_stream_releases_upstream_connection(self):
        release, closed = asyncio.Event(), asyncio.Event()

        async def stream(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'{"done":false,"message":{"content":"first"}}\n')
            await release.wait()
            for _ in range(100):
                if request.transport is None or request.transport.is_closing():
                    closed.set()
                    break
                await asyncio.sleep(0.001)
            return response

        self.handler = stream
        iterator = self.backend.chat("small", [], {}, True)
        try:
            await anext(iterator)
            await iterator.aclose()
            release.set()
            await asyncio.wait_for(closed.wait(), timeout=1)
            self.assertEqual(len(self.session.connector._acquired), 0)
        finally:
            release.set()
            await iterator.aclose()

    async def test_concurrent_requests_keep_separate_totals(self):
        results = await asyncio.gather(*(self.collect() for _ in range(8)))
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result[0].input_tokens == 7 for result in results))
        self.assertEqual(sum(result[0].output_tokens for result in results), 32)


if __name__ == "__main__":
    unittest.main()
