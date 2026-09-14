import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import time

import pytest

from vaws_public_download import download


@pytest.fixture
def artifact_server():
    body = b"public-release-fixture" * 10000
    ranges = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            value = self.headers.get("Range", "")
            ranges.append(value)
            offset = int(value[6:-1]) if value else 0
            if self.path == "/ignore":
                offset = 0
            self.send_response(206 if offset else 200)
            if offset:
                self.send_header("Content-Range", f"bytes {offset}-{len(body)-1}/{len(body)}")
            self.send_header("Content-Length", str(len(body)-offset))
            self.end_headers()
            try:
                if self.path == "/slow":
                    for _ in range(30):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.1)
                else:
                    self.wfile.write(body[offset:])
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", body, ranges
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("endpoint", ["/resume", "/ignore"])
def test_resume_or_restart_preserves_exact_digest(artifact_server, tmp_path, endpoint, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, body, ranges = artifact_server
    target = tmp_path / "archive.zip"
    target.with_suffix(".zip.part").write_bytes(body[:20])
    expected = hashlib.sha256(body).hexdigest()
    assert download(url + endpoint, target, expected)["sha256_verified"] is True
    assert target.read_bytes() == body and ranges == ["bytes=20-"]
    assert download(url + endpoint, target, expected)["status"] == "cached"
    assert len(ranges) == 1


def test_corrupt_release_is_not_published(artifact_server, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, _, _ = artifact_server
    target = tmp_path / "archive.zip"
    with pytest.raises(ValueError):
        download(url, target, "0" * 64)
    assert not target.exists() and not target.with_suffix(".zip.part").exists()


def test_download_deadline_retains_partial_but_never_publishes(artifact_server, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, _, _ = artifact_server
    target = tmp_path / "archive.zip"
    with pytest.raises(subprocess.TimeoutExpired):
        download(url + "/slow", target, "0" * 64, timeout=.7)
    assert not target.exists() and target.with_suffix(".zip.part").exists()
