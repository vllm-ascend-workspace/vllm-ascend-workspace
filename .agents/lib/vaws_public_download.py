"""Resumable, SHA256-verified public release download with a whole deadline."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
from urllib import request
from urllib.parse import urlsplit


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def transfer(url: str, target: Path, expected: str, *, max_bytes=256 * 1024 * 1024):
    """Only public HTTPS (or loopback tests); never accept unverified archives."""
    parsed = urlsplit(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Expected a public HTTPS artifact URL without credentials")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Expected the published SHA256 digest")
    if target.is_file() and digest(target) == expected:
        return "cached"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset and digest(partial) == expected:
        partial.replace(target)
        return "resumed"
    if offset > max_bytes:
        raise ValueError("Partial download exceeds the artifact limit")
    from vaws_network_probe import Redirects
    opener = request.build_opener(request.HTTPSHandler(context=ssl.create_default_context()), Redirects())
    headers = {"User-Agent": "vaws-bootstrap", "Accept": "application/octet-stream"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with opener.open(request.Request(url, headers=headers), timeout=20) as response:
        length = int(response.headers.get("Content-Length", "0"))
        if response.status == 206:
            if not re.match(rf"bytes {offset}-[0-9]+/(?:[0-9]+|\*)$", response.headers.get("Content-Range", "")):
                raise ValueError("Invalid resume range")
        else:
            offset = 0  # A server may ignore Range; restart instead of appending.
        with partial.open("ab" if offset else "wb") as output:
            count = offset
            while block := response.read1(65536):
                count += len(block)
                if count > max_bytes:
                    raise ValueError("Artifact exceeds download limit")
                output.write(block)
        if length and count - offset != length:
            raise ConnectionError("Incomplete artifact; partial retained for resume")
    if digest(partial) != expected:
        partial.unlink()  # A completed corrupt artifact is not resumable.
        raise ValueError("Artifact SHA256 mismatch")
    partial.replace(target)
    return "resumed" if offset else "downloaded"


def download(url: str, target: Path, expected: str, *, timeout=300) -> dict:
    from vaws_process_wait import run_captured
    env = dict(os.environ)
    # Only public artifact coordinates; no credentials in arguments or logs.
    env["VAWS_PUBLIC_DOWNLOAD"] = json.dumps({"url": url, "target": str(target), "sha256": expected})
    result = run_captured([sys.executable, "-I", str(Path(__file__).resolve())], stage="gh_download", timeout=timeout, env=env)
    facts = json.loads(result.stdout or "{}")
    if result.returncode:
        raise ValueError("Public download failed: " + facts.get("category", "worker"))
    return facts


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from vaws_network_probe import category
    try:
        spec = json.loads(os.environ.pop("VAWS_PUBLIC_DOWNLOAD"))
        state = transfer(spec["url"], Path(spec["target"]), spec["sha256"])
        print(json.dumps({"status": state, "sha256_verified": True}))
    except Exception as exc:
        print(json.dumps({"status": "failed", "category": category(exc), "error_type": type(exc).__name__}))
        raise SystemExit(1)
