"""HTTP acceptance checks with real loopback sockets and a simulated Ollama."""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from local_llm_gateway.app import ACTIVE, STORE, create_app
from local_llm_gateway.config import Config
from local_llm_gateway.storage import Store


class GatewayHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mode = "normal"
        self.show_metadata = {"details": {"format": "gguf"}, "model_info": {"general.architecture": "llama"}}
        self.chat_calls = []
        self.show_calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.running = 0
        self.high_water = 0
        upstream = web.Application()
        upstream.add_routes([
            web.post("/api/show", self.show), web.post("/api/chat", self.ollama_chat),
            web.get("/api/tags", self.tags), web.get("/api/ps", self.loaded),
        ])
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.config_dict = {
            "server": {"database": str(Path(self.directory.name) / "usage.sqlite3"),
                       "timeout_seconds": 3, "max_concurrent": 4},
            "backends": {"ollama": {"type": "ollama", "url": str(self.upstream.make_url("/"))}},
            "models": {"local-chat": {"backend": "ollama", "model": "tiny:local"},
                       "other-chat": {"backend": "ollama", "model": "other:local"}},
            "clients": {"demo": {"default_model": "local-chat"},
                        "second-app": {"default_model": "other-chat"}},
        }
        self.config = Config.from_dict(self.config_dict)
        self.app = create_app(self.config)
        self.gateway = TestServer(self.app)
        await self.gateway.start_server()
        self.client = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        await self.gateway.close()
        await self.upstream.close()

    async def show(self, request):
        self.show_calls.append(await request.json())
        return web.json_response(self.show_metadata)

    async def tags(self, request):
        return web.json_response({"models": [
            {"name": "tiny:local", "details": {"format": "gguf"}},
            {"name": "remote:cloud", "remote_host": "https://example.invalid"},
        ]})

    async def loaded(self, request):
        return web.json_response({"models": [{"name": "tiny:local"}]})

    async def ollama_chat(self, request):
        body = await request.json()
        self.chat_calls.append(body)
        self.running += 1
        self.high_water = max(self.high_water, self.running)
        self.entered.set()
        try:
            if self.mode in ("hold", "hold_stream_before_headers"):
                await self.release.wait()
            if self.mode == "concurrent":
                await asyncio.sleep(.03)
            if self.mode == "failure":
                return web.Response(status=500, text="PRIVATE PROMPT Authorization: SECRET_KEY")
            final = {"model": body["model"], "done": True, "message": {"content": "hello 世界"},
                     "prompt_eval_count": 7, "eval_count": 3, "eval_duration": 600_000_000}
            if self.mode == "missing_metrics":
                for key in ("prompt_eval_count", "eval_count", "eval_duration"):
                    del final[key]
            elif self.mode == "partial_metrics":
                final["eval_count"] = -2
            if not body["stream"]:
                return web.json_response(final)
            response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
            await response.prepare(request)
            first = {"model": body["model"], "done": False, "message": {"content": "hello "},
                     "prompt_eval_count": 700, "eval_count": 300}
            await response.write(json.dumps(first).encode() + b"\n")
            if self.mode == "hold_stream":
                await self.release.wait()
            if self.mode != "truncated":
                final["message"]["content"] = "世界"
                await response.write(json.dumps(final).encode() + b"\n")
            await response.write_eof()
            return response
        except (ConnectionResetError, BrokenPipeError):
            return web.Response(status=499)
        finally:
            self.running -= 1

    async def post(self, body=None, *, client="demo", headers=None):
        if body is None:
            body = {"model": "local-chat", "messages": [{"role": "user", "content": "Test 世界"}]}
        raw = json.dumps(body, ensure_ascii=False).encode()
        request_headers = {"Content-Type": "application/json", "X-Client-ID": client, **(headers or {})}
        async with self.client.post(self.gateway.make_url("/v1/chat/completions"), data=raw, headers=request_headers) as response:
            return response.status, await response.read(), dict(response.headers), raw

    async def get_json(self, path):
        async with self.client.get(self.gateway.make_url(path)) as response:
            return response.status, await response.json()

    async def wait_rows(self, count=1, *, finished=True):
        for _ in range(200):
            rows = self.app[STORE].recent(limit=1000)
            if len(rows) == count and (not finished or all(row["status"] != "in_progress" for row in rows)):
                return rows
            await asyncio.sleep(.01)
        self.fail(f"Expected {count} finalized rows; got {rows}")

    async def test_nonstream_exact_bytes_usage_defaults_and_manual_route(self):
        status, raw, headers, sent = await self.post()
        self.assertEqual(status, 200)
        result = json.loads(raw)
        self.assertEqual(result["choices"][0]["message"]["content"], "hello 世界")
        self.assertEqual(result["usage"], {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        row = (await self.wait_rows())[0]
        self.assertEqual(row["id"], headers["X-Request-ID"])
        self.assertEqual(row["request_bytes"], len(sent))
        self.assertEqual(row["response_bytes"], len(raw))
        self.assertEqual(row["generation_tps"], 5)
        self.assertGreater(row["duration_ms"], 0)
        self.assertIsNone(row["ttft_ms"])
        self.assertEqual(row["token_source"], "measured")
        self.assertEqual((row["backend"], row["requested_model"], row["actual_model"]), ("ollama", "local-chat", "tiny:local"))
        status, _, _, _ = await self.post({"messages": [{"role": "user", "content": "default"}]}, client="second-app")
        self.assertEqual(status, 200)
        self.assertEqual(self.chat_calls[-1]["model"], "other:local")
        status, _, _, _ = await self.post({"backend": "ollama", "model": "tiny:local", "messages": [{"role": "user", "content": "manual"}]})
        self.assertEqual(status, 200)
        self.assertEqual(self.chat_calls[-1]["model"], "tiny:local")

    async def test_stream_terminal_usage_not_chunk_totals_and_exact_body_bytes(self):
        status, raw, _, sent = await self.post({"model": "local-chat", "stream": True,
            "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": "test"}]})
        self.assertEqual(status, 200)
        self.assertTrue(raw.endswith(b"data: [DONE]\n\n"))
        packets = [json.loads(part[6:]) for part in raw.decode().split("\n\n") if part.startswith("data: {")]
        self.assertEqual("".join(choice["delta"].get("content", "") for packet in packets for choice in packet["choices"]), "hello 世界")
        usages = [packet["usage"] for packet in packets if "usage" in packet]
        self.assertEqual(usages, [{"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}])
        row = (await self.wait_rows())[0]
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["input_tokens"], 7)
        self.assertEqual(row["output_tokens"], 3)
        self.assertEqual(row["total_tokens"], 10)
        self.assertEqual(row["request_bytes"], len(sent))
        self.assertEqual(row["response_bytes"], len(raw))
        self.assertGreater(row["ttft_ms"], 0)
        _, stats = await self.get_json("/admin/stats")
        self.assertEqual(stats["groups"][0]["total_tokens"], 10)
        self.assertEqual(stats["groups"][0]["total_token_samples"], 1)

    async def test_missing_and_partial_measurements_stay_null(self):
        self.mode = "missing_metrics"
        status, raw, _, _ = await self.post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["usage"], {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None})
        row = (await self.wait_rows())[0]
        self.assertEqual(row["token_source"], "unavailable")
        self.assertIsNone(row["generation_tps"])
        self.mode = "partial_metrics"
        await self.post()
        row = (await self.wait_rows(2))[0]
        self.assertEqual(row["token_source"], "partial")
        self.assertEqual(row["input_tokens"], 7)
        self.assertIsNone(row["output_tokens"])
        self.assertIsNone(row["total_tokens"])
        _, stats = await self.get_json("/admin/stats")
        group = stats["groups"][0]
        self.assertIsNone(group["total_tokens"])
        self.assertEqual(group["total_token_samples"], 0)
        self.assertEqual(group["input_tokens"], 7)

    async def test_backend_failure_and_partial_stream_are_sanitized(self):
        self.mode = "failure"
        status, raw, _, _ = await self.post(headers={"Authorization": "Bearer SECRET_KEY"})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(raw)["error"]["code"], "backend_unavailable")
        self.assertNotIn(b"PRIVATE", raw)
        self.assertNotIn(b"SECRET", raw)
        self.mode = "truncated"
        status, raw, _, _ = await self.post({"stream": True, "messages": [{"role": "user", "content": "test"}]})
        self.assertEqual(status, 200)
        self.assertIn(b"hello ", raw)
        self.assertIn(b'"error"', raw)
        self.assertNotIn(b"[DONE]", raw)
        rows = await self.wait_rows(2)
        self.assertEqual(rows[0]["status"], "partial")
        self.assertIsNone(rows[0]["total_tokens"])
        self.assertEqual(rows[1]["status"], "failed")

    async def test_stream_is_forwarded_before_backend_finishes_and_disconnect_recorded(self):
        self.mode = "hold_stream"
        response = await self.client.post(self.gateway.make_url("/v1/chat/completions"),
            json={"stream": True, "messages": [{"role": "user", "content": "cancel"}]},
            headers={"X-Client-ID": "demo"})
        try:
            prefix = await asyncio.wait_for(response.content.readuntil(b"hello "), 1)
            self.assertIn(b"hello ", prefix)
            self.assertFalse(self.release.is_set())
        finally:
            response.close()
        row = (await self.wait_rows())[0]
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["error_category"], "client_disconnected")
        self.assertIsNone(row["total_tokens"])
        self.assertGreater(row["response_bytes"], 0)
        self.assertEqual(self.app[ACTIVE]["count"], 0)

    async def test_disconnect_before_headers_in_nonstream_and_stream(self):
        for index, stream in enumerate((False, True), 1):
            with self.subTest(stream=stream):
                self.mode = "hold"
                self.entered.clear()
                raw = json.dumps({"stream": stream, "messages": [{"role": "user", "content": "disconnect"}]}).encode()
                reader, writer = await asyncio.open_connection(self.gateway.host, self.gateway.port)
                writer.write((f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{self.gateway.port}\r\n"
                    f"Content-Type: application/json\r\nX-Client-ID: demo\r\nContent-Length: {len(raw)}\r\n\r\n").encode() + raw)
                await writer.drain()
                await asyncio.wait_for(self.entered.wait(), 1)
                writer.close()
                await writer.wait_closed()
                row = (await self.wait_rows(index))[0]
                self.assertEqual(row["status"], "cancelled")
                self.assertEqual(row["response_bytes"], 0)
                self.assertIsNone(row["total_tokens"])
                self.assertEqual(self.app[ACTIVE]["count"], 0)

    async def test_shutdown_cancels_active_handler_as_gateway_interruption(self):
        # Exercise real aiohttp shutdown timeout cancellation, distinct from a
        # downstream peer disconnect. Reopen SQLite only after cleanup releases it.
        await self.gateway.close()
        self.app = create_app(self.config)
        self.gateway = TestServer(self.app)
        await self.gateway.start_server(shutdown_timeout=.01)
        self.mode = "hold"
        task = asyncio.create_task(self.post())
        try:
            await asyncio.wait_for(self.entered.wait(), 1)
            await self.wait_rows(finished=False)
            await asyncio.wait_for(self.gateway.close(), 1)
            self.assertTrue(self.app[ACTIVE]["shutdown"])
            self.assertEqual(self.app[ACTIVE]["count"], 0)
            with Store(self.config.database) as reopened:
                row = reopened.recent()[0]
                self.assertEqual(row["status"], "interrupted")
                self.assertEqual(row["error_category"], "gateway_shutdown")
                self.assertIsNone(row["total_tokens"])
                self.assertEqual(row["response_bytes"], 0)
                self.assertGreater(row["duration_ms"], 0)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_concurrent_requests_remain_separate(self):
        self.mode = "concurrent"
        results = await asyncio.gather(*(self.post(client="demo" if i % 2 else "second-app") for i in range(4)))
        self.assertTrue(all(result[0] == 200 for result in results))
        rows = await self.wait_rows(4)
        self.assertEqual(len({row["id"] for row in rows}), 4)
        self.assertTrue(all(row["total_tokens"] == 10 for row in rows))
        self.assertGreaterEqual(self.high_water, 2)
        _, stats = await self.get_json("/admin/stats?group_by=client_id")
        self.assertEqual([group["request_count"] for group in stats["groups"]], [2, 2])
        self.assertEqual([group["total_tokens"] for group in stats["groups"]], [20, 20])

    async def test_max_concurrency_rejects_without_forwarding(self):
        self.mode = "hold"
        tasks = [asyncio.create_task(self.post()) for _ in range(4)]
        try:
            for _ in range(100):
                if len(self.chat_calls) == 4:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(len(self.chat_calls), 4)
            status, raw, _, _ = await self.post()
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(raw)["error"]["code"], "gateway_busy")
            self.assertEqual(len(self.chat_calls), 4)
        finally:
            self.release.set()
            await asyncio.gather(*tasks)
        rows = await self.wait_rows(5)
        self.assertEqual(sum(row["status"] == "success" for row in rows), 4)

    async def test_cloud_show_metadata_stops_prompt_forwarding(self):
        self.show_metadata["remote_host"] = "https://remote.invalid"
        status, raw, _, _ = await self.post()
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"]["code"], "route_not_local")
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.show_calls, [{"model": "tiny:local"}])
        row = (await self.wait_rows())[0]
        self.assertEqual(row["error_category"], "route_not_local")

    async def test_unconfigured_fields_clients_and_arbitrary_routes_rejected(self):
        messages = [{"role": "user", "content": "SECRET_PROMPT"}]
        cases = [
            ({"messages": messages, "tools": []}, "demo", 400),
            ({"messages": messages, "url": "https://remote.invalid"}, "demo", 400),
            ({"messages": messages, "model": "not-configured"}, "demo", 404),
            ({"messages": messages, "backend": "http://remote.invalid", "model": "tiny:local"}, "demo", 404),
            ({"messages": messages, "backend": "ollama", "model": "arbitrary-model"}, "demo", 404),
            ({"messages": messages}, "unknown-secret-client", 401),
            ({"messages": [{"role": "user", "content": [{"type": "image_url"}]}]}, "demo", 400),
            ({"messages": messages, "stream": "true"}, "demo", 400),
        ]
        for body, client, expected in cases:
            with self.subTest(body=body):
                status, raw, _, _ = await self.post(body, client=client)
                self.assertEqual(status, expected)
                self.assertNotIn(b"SECRET_PROMPT", raw)
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.show_calls, [])
        rows = await self.wait_rows(len(cases))
        self.assertNotIn("unknown-secret-client", json.dumps(rows))

    async def test_huge_numeric_options_return_client_error_before_backend_request(self):
        for name in ("temperature", "top_p"):
            with self.subTest(name=name):
                status, raw, _, _ = await self.post({name: 10**400,
                    "messages": [{"role": "user", "content": "not forwarded"}]})
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(raw)["error"]["code"], "invalid_request")
        self.assertEqual(self.show_calls, [])
        self.assertEqual(self.chat_calls, [])
        rows = await self.wait_rows(2)
        self.assertTrue(all(row["error_category"] == "invalid_request" for row in rows))

    async def test_browser_can_navigate_read_only_pages_but_cannot_infer_or_fetch(self):
        navigation = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for site in ("none", "same-origin", "cross-site"):
            for path in ("/healthz", "/v1/models", "/admin/backends", "/admin/stats?group_by=client_id,model", "/admin/requests"):
                with self.subTest(site=site, path=path):
                    async with self.client.get(self.gateway.make_url(path), headers={**navigation, "Sec-Fetch-Site": site}) as response:
                        self.assertEqual(response.status, 200)
                        self.assertNotIn("error", await response.json())
                        self.assertNotIn("Access-Control-Allow-Origin", response.headers)
        denied = (
            {**navigation, "Origin": "https://example.invalid"},
            {**navigation, "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
            {**navigation, "Sec-Fetch-Dest": "iframe"},
            {"Sec-Fetch-Mode": "cors"},
            {**navigation, "Host": "rebinding.example"},
        )
        for headers in denied:
            async with self.client.get(self.gateway.make_url("/admin/stats"), headers=headers) as response:
                self.assertEqual(response.status, 403)
        for path in ("/v1/chat/completions", "/unknown"):
            async with self.client.get(self.gateway.make_url(path), headers=navigation) as response:
                self.assertEqual(response.status, 403)
        for headers in (navigation, {"Origin": "http://127.0.0.1"},
                        {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}):
            self.assertEqual((await self.post(headers=headers))[0], 403)
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.show_calls, [])
        self.assertEqual(self.app[STORE].recent(), [])

    async def test_health_discovery_filters_and_browser_boundary(self):
        status, health = await self.get_json("/healthz")
        self.assertEqual((status, health["status"]), (200, "ok"))
        _, models = await self.get_json("/v1/models")
        self.assertEqual({entry["id"] for entry in models["data"]}, {"local-chat", "other-chat"})
        _, inventory = await self.get_json("/admin/backends")
        backend = inventory["backends"][0]
        self.assertTrue(backend["healthy"])
        self.assertEqual(backend["loaded"], ["tiny:local"])
        self.assertEqual([item["local"] for item in backend["installed"]], [True, False])
        await self.post()
        await self.post({"messages": [{"role": "user", "content": "other"}]}, client="second-app")
        day = datetime.now(timezone.utc).date().isoformat()
        _, recent = await self.get_json("/admin/requests?client_id=second-app&actual_model=other:local&backend=ollama&since=" + day + "&until=" + day)
        self.assertEqual(len(recent["requests"]), 1)
        _, grouped = await self.get_json("/admin/stats?group_by=client_id,model,backend,date")
        self.assertEqual(len(grouped["groups"]), 2)
        self.assertEqual({row["model"] for row in grouped["groups"]}, {"tiny:local", "other:local"})
        for path in ("/admin/stats?group_by=unknown", "/admin/stats?client_id=demo&client_id=demo",
                     "/admin/requests?limit=0", "/admin/requests?since=not-a-date", "/admin/requests?prompt=secret"):
            self.assertEqual((await self.get_json(path))[0], 400)
        for headers in ({"Origin": "https://example.invalid"}, {"Host": "rebinding.example"}, {"Sec-Fetch-Site": "same-origin"}):
            async with self.client.get(self.gateway.make_url("/healthz"), headers=headers) as response:
                self.assertEqual(response.status, 403)

    async def test_content_headers_and_model_reply_not_persisted_and_restart_retains_usage(self):
        await self.post({"messages": [{"role": "user", "content": "PRIVATE_PROMPT_SENTINEL"}]}, headers={"Authorization": "Bearer SECRET_KEY_SENTINEL"})
        row = (await self.wait_rows())[0]
        self.assertNotIn("PRIVATE_PROMPT_SENTINEL", json.dumps(row))
        self.assertNotIn("SECRET_KEY_SENTINEL", json.dumps(row))
        self.assertNotIn("hello", json.dumps(row))
        await self.gateway.close()
        for path in Path(self.directory.name).glob("usage.sqlite3*"):
            raw = path.read_bytes()
            for secret in (b"PRIVATE_PROMPT_SENTINEL", b"SECRET_KEY_SENTINEL", "hello 世界".encode()):
                self.assertNotIn(secret, raw)
        self.app = create_app(self.config)
        self.gateway = TestServer(self.app)
        await self.gateway.start_server()
        _, recent = await self.get_json("/admin/requests")
        self.assertEqual(recent["requests"][0]["id"], row["id"])
        self.assertEqual(recent["requests"][0]["total_tokens"], 10)
        self.assertEqual(recent["requests"][0]["status"], "success")

    def test_config_rejects_remote_backends_and_nonloopback_listener(self):
        for value in ("https://api.example.invalid", "http://192.168.1.2:11434", "http://0.0.0.0:11434",
                      "http://user:secret@127.0.0.1:11434", "http://127.0.0.1:11434/path", "http://127.0.0.1:11434?url=remote",
                      "http://127.0.0.1:0"):
            config = json.loads(json.dumps(self.config_dict))
            config["backends"]["ollama"]["url"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.from_dict(config)
        for host in ("0.0.0.0", "::", "192.168.1.2", "remote.example"):
            config = json.loads(json.dumps(self.config_dict))
            config["server"]["host"] = host
            with self.subTest(host=host), self.assertRaises(ValueError):
                Config.from_dict(config)


if __name__ == "__main__":
    unittest.main()
