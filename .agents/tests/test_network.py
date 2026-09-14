"""Real slow/error servers prove probe deadlines and credential-safe reports."""
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

import vaws_network as network


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/proxy-auth":
                self.send_error(407)
                return
            self.send_response(200)
            self.end_headers()
            try:
                if self.path == "/drip":
                    for _ in range(30):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(.1)
                else:
                    self.wfile.write(b"x" * 32768)
            except (BrokenPipeError, ConnectionResetError):
                pass
    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{instance.server_port}"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join()


def test_real_direct_probe_ignores_broken_inherited_proxy(server):
    result = network.probe(server, network.Route("direct", ""),
                           environment={**os.environ, "HTTP_PROXY": "http://127.0.0.1:1"})
    assert result["status"] == "ok" and result["bytes"] == 32768


def test_drip_response_cannot_extend_whole_probe_deadline(server):
    started = time.monotonic()
    result = network.probe(server + "/drip", network.Route("direct", ""), deadline=.7)
    assert result["category"] == "deadline" and result["child_reaped"] is True
    assert time.monotonic() - started < 3


def test_dns_like_worker_stall_is_reaped_at_deadline(tmp_path):
    worker = tmp_path / "blocked.py"
    worker.write_text("import time; time.sleep(60)", encoding="utf-8")
    result = network.probe("https://example.invalid", network.Route("direct", ""), deadline=.5, worker=worker)
    assert result["category"] == "deadline" and result["child_reaped"] is True


def test_proxy_auth_has_specific_remedy_and_no_response_body(server):
    result = network.probe(server + "/proxy-auth", network.Route("direct", ""))
    assert result["category"] == "proxy_auth"
    assert result["http_status"] == 407 and "body" not in result


def test_discovery_never_serializes_proxy_credentials(tmp_path, monkeypatch):
    fixture_value = "fixture" + "_private_credential"
    proxy = "http://" + "operator:" + fixture_value + "@proxy.example:8080"
    monkeypatch.setattr(network, "_config", lambda *args: proxy)
    monkeypatch.setattr(network, "windows_winhttp", lambda: "")
    routes, report = network.discover(tmp_path, {"HTTPS_PROXY": proxy})
    assert routes[2].proxy == proxy
    assert fixture_value not in repr(routes) + json.dumps(report)
    assert "proxy.example" not in json.dumps(report)
    assert report["routes"][2]["authenticated"] is True


def test_environment_reuses_reference_without_probes_and_preserves_explicit_settings(tmp_path, monkeypatch):
    network.atomic(tmp_path / network.PROFILE, {"schema": 1, "proxy_source": "direct", "no_proxy": ["mirror.example"], "transport": network.DEFAULTS})
    monkeypatch.setattr(network, "discover", lambda *args: ([network.Route("direct", "")], {}))
    monkeypatch.setattr(network, "probe", lambda *args, **kwargs: pytest.fail("warm reuse must not probe"))
    original = {"HTTPS_PROXY": "http://proxy.example:8080", "NO_PROXY": "existing.example", "UV_HTTP_TIMEOUT": "120"}
    result = network.environment_for(tmp_path, original)
    assert "HTTPS_PROXY" not in result
    assert result["UV_HTTP_TIMEOUT"] == "120" and result["UV_SYSTEM_CERTS"] == "true"
    assert {"existing.example", "mirror.example", "localhost", "127.0.0.1", "::1"} <= set(result["NO_PROXY"].split(","))
    assert original["HTTPS_PROXY"] == "http://proxy.example:8080"


def test_marginal_timing_does_not_replace_working_route():
    inherited = {"source": "inherited", "status": "ok", "seconds": 1.1}
    faster = {"source": "direct", "status": "ok", "seconds": 1.0}
    assert network.select([inherited, faster]) == inherited
    assert network.select([{**inherited, "status": "failed"}, faster]) == faster


def test_failed_check_keeps_credentials_out_of_report(tmp_path, monkeypatch):
    fixture_value = "fixture" + "_proxy_secret"
    monkeypatch.setattr(network, "discover", lambda root: ([network.Route("git:https", "http://user:" + fixture_value + "@proxy.example:8080")], {}))
    monkeypatch.setattr(network, "endpoints", lambda *args: {"github": "https://api.github.com/meta"})
    monkeypatch.setattr(network, "probe", lambda *args, **kwargs: {"source": "git:https", "status": "failed", "category": "dns"})
    result = network.check(tmp_path)
    assert result["status"] == "partial"
    assert "remedy" in result["observations"]["github"][0]
    assert fixture_value not in (tmp_path / network.REPORT).read_text()


def test_scope_restores_environment_after_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "original")
    monkeypatch.setattr(network, "environment_for", lambda root, env, **kwargs: {**env, "HTTPS_PROXY": "selected"})
    with pytest.raises(RuntimeError), network.network_scope(tmp_path):
        assert os.environ["HTTPS_PROXY"] == "selected"
        raise RuntimeError("failed setup")
    assert os.environ["HTTPS_PROXY"] == "original"


def test_target_routes_override_git_config_only_for_github(tmp_path):
    network.atomic(tmp_path / network.PROFILE, {"schema": 1, "routes": {"github": "direct", "models": "env:HTTPS_PROXY"}})
    original = {"HTTPS_PROXY": "http://proxy.example:8080", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.longpaths", "GIT_CONFIG_VALUE_0": "true"}
    result = network.environment_for(tmp_path, original, target="models")
    assert result["HTTPS_PROXY"] == original["HTTPS_PROXY"]
    assert result["GIT_CONFIG_KEY_1"] == "http.https://github.com.proxy" and result["GIT_CONFIG_VALUE_1"] == ""
    assert result["GIT_CONFIG_VALUE_0"] == "true"


def test_warm_selection_resolves_only_its_source(tmp_path, monkeypatch):
    network.atomic(tmp_path / network.PROFILE, {"schema": 1, "routes": {"github": "env:HTTPS_PROXY"}})
    monkeypatch.setattr(network, "discover", lambda *args: pytest.fail("warm reuse rediscovered unrelated settings"))
    monkeypatch.setattr(network, "_config", lambda *args: pytest.fail("warm env selection invoked Git"))
    assert network.environment_for(tmp_path, {"HTTPS_PROXY": "http://proxy.example:8080"})["HTTPS_PROXY"]
    with pytest.raises(ValueError, match="no longer available"):
        network.environment_for(tmp_path, {})


def test_all_failed_checks_preserve_previous_profile(tmp_path, monkeypatch):
    previous = {"schema": 1, "routes": {"github": "direct"}}
    network.atomic(tmp_path / network.PROFILE, previous)
    monkeypatch.setattr(network, "discover", lambda root: ([network.Route("direct", "")], {}))
    monkeypatch.setattr(network, "endpoints", lambda *args: {"github": "https://api.github.com/meta"})
    monkeypatch.setattr(network, "probe", lambda *args, **kwargs: {"source": "direct", "status": "failed", "category": "dns"})
    result = network.check(tmp_path, apply=True)
    assert result["applied"] is False and network.read_profile(tmp_path) == previous


def test_metadata_read_has_hard_deadline_too(server):
    with pytest.raises(ValueError, match="deadline"):
        network.fetch_bytes(server + "/drip", deadline=.5)


def test_inherited_http_route_is_materialized_for_git(tmp_path, monkeypatch):
    from urllib import request
    proxy = "http://proxy.example:8080"
    monkeypatch.setattr(request, "getproxies", lambda: {"https": proxy})
    monkeypatch.setattr(request, "proxy_bypass", lambda host: False)
    routes = [network.Route("inherited"), network.Route("env:HTTPS_PROXY", proxy), network.Route("git:https", "http://broken.example:8080")]
    monkeypatch.setattr(network, "discover", lambda root: (routes, {}))
    monkeypatch.setattr(network, "endpoints", lambda *args: {"github": "https://api.github.com/meta"})
    monkeypatch.setattr(network, "probe", lambda url, route, **kwargs: {"source": route.source, "status": "ok", "category": "ok", "seconds": 1})
    result = network.check(tmp_path, apply=True)
    assert result["selected"]["github"] == "inherited"
    assert result["applied_routes"]["github"] == "env:HTTPS_PROXY"
    env = network.environment_for(tmp_path, {"HTTPS_PROXY": proxy})
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com.proxy"
    assert env["GIT_CONFIG_VALUE_0"] == proxy
