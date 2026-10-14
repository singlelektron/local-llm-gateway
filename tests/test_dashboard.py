"""Browser dashboard checks against a real HTTP server and disposable statistics."""

from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp.test_utils import TestServer

from local_llm_gateway.app import BACKENDS, STORE, create_app
from local_llm_gateway.config import Config


class ParsedHTML(HTMLParser):
    """Inspect semantic HTML without a browser dependency or layout snapshots."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.open_elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.open_elements.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.open_elements) - 1, -1, -1):
            if self.open_elements[index]["tag"] == tag:
                del self.open_elements[index:]
                break

    def handle_data(self, data):
        for element in self.open_elements:
            element["text"] += data

    def metric(self, name):
        elements = [element for element in self.elements if element["attrs"].get("data-metric") == name]
        if len(elements) != 1:
            raise AssertionError(f"Expected exactly one {name!r} metric, got {len(elements)}")
        return elements[0]["text"].strip()

    def request_ids(self):
        return [element["attrs"]["data-request-id"] for element in self.elements if "data-request-id" in element["attrs"]]


class NoBackendAccess:
    def __getattr__(self, name):
        raise AssertionError("Reading local statistics must not contact an inference backend")


class DashboardHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Config.from_dict({
            "server": {"database": str(Path(directory.name) / "usage.sqlite3")},
            "backends": {"ollama": {"type": "ollama", "url": "http://127.0.0.1:9"}},
            "models": {"local-chat": {"backend": "ollama", "model": "tiny:local"}},
            "clients": {"demo": {"default_model": "local-chat"}, "unused": {"default_model": "local-chat"}},
        })
        self.app = create_app(self.config)
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.app[BACKENDS]["ollama"] = NoBackendAccess()
        self.client = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=5))

    async def asyncTearDown(self):
        await self.client.close()
        await self.server.close()

    def record(self, request_id, *, started_at="2026-10-14T12:00:00Z", client_id="demo",
               actual_model="tiny:local", backend="ollama", requested_model="local-chat",
               status="success", input_tokens=7, output_tokens=3, total_tokens=10,
               token_source="measured", **updates):
        self.app[STORE].begin({
            "id": request_id, "started_at": started_at, "client_id": client_id,
            "actual_model": actual_model, "backend": backend, "requested_model": requested_model,
            "stream": False, "request_bytes": 80,
        })
        self.app[STORE].finish(request_id, {
            "status": status, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": total_tokens, "token_source": token_source,
            "response_bytes": 120, "duration_ms": 500, **updates,
        })

    async def page(self, path="/", *, params=None, headers=None):
        async with self.client.get(self.server.make_url(path), params=params, headers=headers) as response:
            text = await response.text()
            return response.status, dict(response.headers), text, ParsedHTML(text)

    async def test_empty_dashboard_is_read_only_and_runs_without_backend(self):
        status, headers, text, parsed = await self.page()
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(parsed.metric("request-count"), "0")
        self.assertNotEqual(parsed.metric("total-tokens"), "0")
        self.assertEqual(parsed.request_ids(), [])
        self.assertEqual(self.app[STORE].recent(), [])
        self.assertTrue(any(element["tag"] == "form" and element["attrs"].get("method", "get").lower() == "get" for element in parsed.elements))
        self.assertIn("unused", text)
        self.assertIn("tiny:local", text)

    async def test_real_aggregate_and_recent_rows_follow_all_filters(self):
        self.record("before", started_at="2026-10-13T23:59:59Z")
        self.record("chosen", client_id="historical", actual_model="old:local", backend="retired")
        self.record("other-client", actual_model="old:local", backend="retired")
        self.record("other-model", client_id="historical", backend="retired")
        self.record("other-backend", client_id="historical", actual_model="old:local")
        self.record("after", started_at="2026-10-15T00:00:00Z")
        status, _, _, parsed = await self.page()
        self.assertEqual(status, 200)
        self.assertEqual(parsed.metric("request-count"), "6")
        self.assertEqual(parsed.metric("total-tokens"), "60")
        status, _, text, parsed = await self.page(params={
            "client_id": "historical", "actual_model": "old:local", "backend": "retired",
            "since": "2026-10-14", "until": "2026-10-14",
        })
        self.assertEqual(status, 200)
        self.assertEqual(parsed.metric("request-count"), "1")
        self.assertEqual(parsed.metric("total-tokens"), "10")
        self.assertEqual(parsed.request_ids(), ["chosen"])
        for value in ("historical", "old:local", "retired"):
            self.assertTrue(any(element["tag"] == "option" and element["attrs"].get("value") == value for element in parsed.elements))
        self.assertEqual(len(self.app[STORE].recent()), 6)

    async def test_recent_limit_does_not_truncate_summary_totals(self):
        for index in range(35):
            self.record(f"request-{index:02d}", started_at=f"2026-10-14T12:00:{index:02d}Z")
        status, _, _, parsed = await self.page()
        self.assertEqual(status, 200)
        self.assertEqual(parsed.metric("request-count"), "35")
        self.assertEqual(parsed.metric("total-tokens"), "350")
        self.assertEqual(parsed.request_ids(), [f"request-{index:02d}" for index in range(34, 4, -1)])

    async def test_extreme_valid_dates_and_pending_requests_render_without_errors(self):
        self.record("complete", client_id="other-client")
        self.app[STORE].begin({
            "id": "pending", "started_at": "2026-10-14T12:00:00Z",
            "client_id": "demo", "actual_model": "tiny:local", "backend": "ollama",
        })
        status, _, _, parsed = await self.page(params={"client_id": "demo"})
        self.assertEqual(status, 200)
        self.assertEqual(parsed.metric("request-count"), "1")
        summary = [element for element in parsed.elements
                   if element["tag"] == "article" and "总请求数" in element["text"]]
        self.assertEqual(len(summary), 1)
        self.assertIn("处理中 1", summary[0]["text"])
        self.assertEqual(parsed.request_ids(), ["pending"])
        for params in ({"until": "0001-01-01"}, {"since": "9999-12-31"}):
            with self.subTest(params=params):
                status, _, text, parsed = await self.page(params=params)
                self.assertEqual(status, 200)
                self.assertEqual(parsed.metric("request-count"), "0")
                self.assertEqual(parsed.request_ids(), [])
                self.assertFalse(any(element["attrs"].get("role") == "alert" for element in parsed.elements))
                self.assertNotIn("日期范围无效", text)
                for field, value in params.items():
                    self.assertTrue(any(element["tag"] == "input"
                                        and element["attrs"].get("name") == field
                                        and element["attrs"].get("value") == value
                                        for element in parsed.elements))
        self.assertEqual(self.app[STORE].recent({"client_id": "demo"})[0]["status"], "in_progress")

    async def test_missing_tokens_remain_distinct_from_measured_zero(self):
        self.record("unknown", client_id="unknown-client", input_tokens=None, output_tokens=None,
                    total_tokens=None, token_source="unavailable", status="cancelled")
        self.record("zero", client_id="zero-client", input_tokens=0, output_tokens=0, total_tokens=0)
        status, _, _, unknown = await self.page(params={"client_id": "unknown-client"})
        self.assertEqual(status, 200)
        self.assertNotEqual(unknown.metric("total-tokens"), "0")
        status, _, _, zero = await self.page(params={"client_id": "zero-client"})
        self.assertEqual(status, 200)
        self.assertEqual(zero.metric("total-tokens"), "0")
        self.assertNotEqual(unknown.metric("total-tokens"), zero.metric("total-tokens"))
        self.assertEqual(self.app[STORE].recent({"client_id": "unknown-client"})[0]["total_tokens"], None)

    async def test_metadata_and_filter_values_are_html_escaped(self):
        hostile = '\"><script>alert("sensitive")</script><img src=x onerror=alert(1)>'
        self.record(hostile, client_id=hostile, backend=hostile, actual_model=hostile,
                    requested_model=hostile, status="failed", error_category=hostile)
        status, _, text, parsed = await self.page()
        self.assertEqual(status, 200)
        self.assertNotIn(hostile, text)
        self.assertNotIn("<script", text.lower())
        self.assertNotIn("<img", text.lower())
        self.assertEqual(parsed.request_ids(), [hostile])
        self.assertTrue(any(hostile in element["text"] for element in parsed.elements))
        status, _, text, parsed = await self.page(params={"client_id": hostile, "actual_model": hostile, "backend": hostile})
        self.assertEqual(status, 200)
        self.assertNotIn(hostile, text)
        self.assertEqual(parsed.request_ids(), [hostile])
        self.assertFalse(any(name.lower().startswith("on") for element in parsed.elements for name in element["attrs"]))

    async def test_invalid_queries_return_html_without_echoing_untrusted_input(self):
        secret = "PRIVATE_AUTHORIZATION_SECRET"
        for params in (
            {secret: secret}, {"since": secret}, {"until": "2026-02-30"},
            {"since": "2026-10-15", "until": "2026-10-14"}, {"refresh": secret},
            {"refresh": "0"}, [("client_id", "demo"), ("client_id", secret)],
            [("refresh", "1"), ("refresh", "1")], {"client_id": secret * 30},
        ):
            with self.subTest(params=params):
                status, headers, text, _ = await self.page(params=params)
                self.assertEqual(status, 400)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertNotIn(secret, text)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.app[STORE].recent(), [])

    async def test_empty_form_fields_and_optional_refresh(self):
        self.record("one")
        status, _, _, parsed = await self.page(params={
            "client_id": "", "actual_model": "", "backend": "", "since": "", "until": "",
        })
        self.assertEqual(status, 200)
        self.assertEqual(parsed.metric("request-count"), "1")
        self.assertFalse(any(element["tag"] == "meta" and element["attrs"].get("http-equiv", "").lower() == "refresh" for element in parsed.elements))
        status, _, _, parsed = await self.page(params={"refresh": "1", "client_id": "demo"})
        self.assertEqual(status, 200)
        refresh = [element for element in parsed.elements if element["tag"] == "meta" and element["attrs"].get("http-equiv", "").lower() == "refresh"]
        self.assertEqual(len(refresh), 1)
        self.assertEqual(refresh[0]["attrs"]["content"], "15")

    async def test_navigation_is_allowed_but_fetch_embedding_and_inference_are_denied(self):
        navigation = {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document", "Sec-Fetch-Site": "same-origin"}
        status, headers, _, parsed = await self.page(headers=navigation)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        csp = headers["Content-Security-Policy"]
        for directive in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'"):
            self.assertIn(directive, csp)
        self.assertFalse(any(element["tag"] in {"script", "iframe", "link", "img"} for element in parsed.elements))
        for request_headers in (
            {**navigation, "Origin": "http://127.0.0.1"},
            {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
            {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"},
        ):
            with self.subTest(headers=request_headers):
                status, _, text, _ = await self.page(headers=request_headers)
                self.assertEqual(status, 403)
                self.assertIn("browser_access_denied", text)
        async with self.client.post(self.server.make_url("/v1/chat/completions"), json={}, headers=navigation) as response:
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "browser_access_denied")
        self.assertEqual(self.app[STORE].recent(), [])


if __name__ == "__main__":
    unittest.main()
