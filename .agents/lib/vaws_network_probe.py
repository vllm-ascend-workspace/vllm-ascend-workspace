"""One bounded, credential-free HTTP observation in a disposable process."""
from __future__ import annotations

import json
import base64
import os
import socket
import ssl
import sys
import time
from urllib import error, request
from urllib.parse import urlsplit


def category(exc: BaseException) -> str:
    reason = getattr(exc, "reason", exc)
    if isinstance(exc, error.HTTPError):
        return {407: "proxy_auth", 401: "authentication", 403: "access_denied",
                429: "rate_limit"}.get(exc.code, "http_error")
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "certificate"
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    # urllib wraps CONNECT's status in OSError rather than HTTPError.
    if "Tunnel connection failed: 407" in str(reason):
        return "proxy_auth"
    if isinstance(reason, ValueError):
        return "invalid_response"
    return "connection"


class Redirects(request.HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.username or target.password or target.scheme != urlsplit(req.full_url).scheme:
            raise ValueError("unsafe redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def observe(spec: dict) -> dict:
    started = time.monotonic()
    count = 0
    first_byte = None
    try:
        # Read route credentials only from this child's environment. Neither
        # URLs, headers, response content nor exception strings enter reports.
        proxy = os.environ.get("VAWS_PROBE_PROXY")
        proxies = request.getproxies() if proxy is None else ({"http": proxy, "https": proxy} if proxy else {})
        context = ssl.create_default_context(cafile=os.environ.get("VAWS_PROBE_CA") or None)
        opener = request.build_opener(request.ProxyHandler(proxies), request.HTTPSHandler(context=context), Redirects())
        parsed = urlsplit(spec["url"])
        if parsed.username or parsed.password or parsed.scheme not in {"http", "https"}:
            raise ValueError("invalid URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("HTTPS required")
        fetch = spec.get("operation") == "fetch"
        limit = min(max(int(spec.get("bytes", 32768)), 1), 8 * 1024 * 1024 if fetch else 256 * 1024)
        timeout = min(max(float(spec.get("timeout", 4)), 0.1), 10)
        headers = {"User-Agent": "vaws-network-check", "Accept": "text/html" if fetch else "*/*"}
        if not fetch:
            headers["Range"] = f"bytes=0-{limit - 1}"
        req = request.Request(spec["url"], headers=headers)
        body = bytearray()
        with opener.open(req, timeout=timeout) as response:
            read_limit = limit + 1 if fetch else limit
            while count < read_limit:
                data = response.read1(min(read_limit - count, 16384))
                if not data:
                    break
                if first_byte is None:
                    first_byte = time.monotonic() - started
                count += len(data)
                if fetch:
                    body.extend(data)
            status = response.status
        if fetch and count > limit:
            raise ValueError("response too large")
        elapsed = max(time.monotonic() - started, 0.001)
        return {"status": "ok", "category": "ok", "http_status": status, "bytes": count,
                "seconds": round(elapsed, 3), "first_byte_seconds": first_byte,
                "bytes_per_second": round(count / elapsed), "tls_verified": urlsplit(spec["url"]).scheme == "https",
                **({"data": base64.b64encode(body).decode("ascii")} if fetch else {})}
    except Exception as exc:
        reason = getattr(exc, "reason", exc)
        verify = getattr(reason, "verify_code", None)
        return {"status": "failed", "category": category(exc), "error_type": type(exc).__name__,
                "http_status": getattr(exc, "code", None), "bytes": count,
                "seconds": round(time.monotonic() - started, 3),
                **({"certificate_reason": {9: "not_yet_valid", 10: "expired", 18: "self_signed", 19: "untrusted_chain",
                                           20: "missing_issuer", 21: "unverified_chain", 62: "hostname_mismatch", 64: "hostname_mismatch"}.get(verify, "chain_validation"),
                    "verify_code": verify} if verify is not None else {})}


if __name__ == "__main__":
    try:
        spec = json.loads(sys.stdin.buffer.read(16385))
        print(json.dumps(observe(spec)), flush=True)
    except Exception as exc:
        print(json.dumps({"status": "failed", "category": "probe_input", "error_type": type(exc).__name__}))
