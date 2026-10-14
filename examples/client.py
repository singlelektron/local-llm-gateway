#!/usr/bin/env python3
"""Independent, standard-library-only client for the local gateway's text API."""

import argparse
import http.client
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def gateway_url(value):
    """Accept literal loopback addresses, avoiding DNS and redirect ambiguity."""
    try:
        parsed = urllib.parse.urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if (parsed.scheme != "http" or not address.is_loopback
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or port == 0):
            raise ValueError
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Use an HTTP root URL with a literal loopback IP, such as "
            "http://127.0.0.1:8787"
        ) from error
    return value.rstrip("/")


def error_label(payload):
    """Do not echo arbitrary backend errors or request content to diagnostics."""
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else "gateway_error"


def show_usage(usage):
    if not isinstance(usage, dict):
        print("Usage: unavailable", file=sys.stderr)
        return
    fields = []
    for label, key in (("input", "prompt_tokens"), ("output", "completion_tokens"),
                       ("total", "total_tokens")):
        count = usage.get(key)
        fields.append(f"{label}={count if type(count) is int and count >= 0 else 'unavailable'}")
    print("Usage: " + ", ".join(fields), file=sys.stderr)


def stream_reply(response):
    """Render SSE as it arrives; EOF without [DONE] is a failed/partial reply."""
    data_lines = []
    event_bytes = 0
    usage = None
    while True:
        raw = response.readline(1_048_577)
        if not raw:
            raise ValueError("Stream ended without [DONE]; the reply may be partial")
        event_bytes += len(raw)
        if event_bytes > 1_048_576:
            raise ValueError("Gateway stream event exceeded the client limit")
        line = raw.decode("utf-8").rstrip("\r\n")
        if line:
            if line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
            continue
        event_bytes = 0
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        data_lines.clear()
        if data == "[DONE]":
            print()
            show_usage(usage)
            return
        packet = json.loads(data)
        if not isinstance(packet, dict):
            raise ValueError("Invalid gateway stream event")
        if "error" in packet:
            raise ValueError("Gateway stream failed: " + error_label(packet))
        if packet.get("usage") is not None:
            usage = packet["usage"]
        choices = packet.get("choices", [])
        if not isinstance(choices, list):
            raise ValueError("Invalid gateway stream choices")
        for choice in choices:
            if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                raise ValueError("Invalid gateway stream delta")
            content = choice["delta"].get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("Invalid gateway text content")
                print(content, end="", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=gateway_url, default="http://127.0.0.1:8787")
    parser.add_argument("--client", default="demo", help="Configured accounting identifier (default: demo)")
    parser.add_argument("--model", default="local-chat", help="Configured gateway model alias")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--prompt", default="Reply with exactly: local gateway ready.")
    parser.add_argument("--max-tokens", type=int, default=48)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.client):
        parser.error("--client must contain 1–64 letters, digits, dots, underscores or hyphens")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be positive")
    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "max_tokens": args.max_tokens,
        "stream": args.stream,
    }
    if args.stream:
        payload["stream_options"] = {"include_usage": True}
    request = urllib.request.Request(
        args.url + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Client-ID": args.client},
        method="POST",
    )
    # Neither environment proxies nor HTTP redirects may move this request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=180) as response:
            if args.stream:
                if response.headers.get_content_type() != "text/event-stream":
                    raise ValueError("Gateway did not return an SSE stream")
                stream_reply(response)
            else:
                data = response.read(1_048_577)
                if len(data) > 1_048_576:
                    raise ValueError("Gateway response exceeded the client limit")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("Invalid gateway response")
                if "error" in result:
                    raise ValueError("Gateway failed: " + error_label(result))
                try:
                    content = result["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as error:
                    raise ValueError("Invalid gateway completion") from error
                if not isinstance(content, str):
                    raise ValueError("Invalid gateway text content")
                print(content)
                show_usage(result.get("usage"))
    except urllib.error.HTTPError as error:
        try:
            label = error_label(json.loads(error.read(4096)))
        except (ValueError, OSError):
            label = "gateway_error"
        finally:
            error.close()
        print(f"Gateway returned HTTP {error.code}: {label}. No retry was made.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        print("Gateway connection failed; any displayed reply may be partial. No retry was made.", file=sys.stderr)
        return 1
    except (ValueError, UnicodeError) as error:
        # JSON decoder errors can include fragments of untrusted input.
        message = "Invalid gateway JSON" if isinstance(error, json.JSONDecodeError) else str(error)
        print(message + ". No retry was made.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nClient cancelled; backend completion is not confirmed.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
