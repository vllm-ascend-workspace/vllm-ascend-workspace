"""Real TLS proves that an explicit CA repairs trust without disabling checks."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import threading

import pytest

import vaws_certificates as certificates
import vaws_network as network


@pytest.fixture
def tls_server(tmp_path):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL CLI required to generate ephemeral TLS test credentials")
    certificate, key = tmp_path / "ca.pem", tmp_path / "key.pem"
    config = tmp_path / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=extensions\nprompt=no\n[dn]\nCN=localhost\n[extensions]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,digitalSignature,keyEncipherment\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="ascii")
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-config", str(config),
                    "-keyout", str(key), "-out", str(certificate)], check=True, capture_output=True, timeout=15)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"verified")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", certificate, key
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_explicit_ca_repairs_actual_tls_and_bridges_clients(tls_server, tmp_path):
    url, certificate, _ = tls_server
    base = {name: value for name, value in os.environ.items() if name not in certificates.CA_ENV}
    initial = network.probe(url, network.Route("direct", ""), environment=base)
    assert initial["category"] == "certificate"
    facts = certificates.configure(tmp_path, certificate)
    assert facts["os_trust_changed"] is False
    env = network.environment_for(tmp_path, base)
    assert len({env[name] for name in certificates.CA_ENV}) == 1
    result = network.probe(url, network.Route("direct", ""), environment=env)
    assert result["status"] == "ok" and result["tls_verified"] is True
    assert hashlib.sha256((tmp_path / certificates.BUNDLE).read_bytes()).hexdigest() == facts["sha256"]


def test_private_key_and_invalid_ca_do_not_replace_trust(tls_server, tmp_path):
    _, certificate, key = tls_server
    certificates.configure(tmp_path, certificate)
    previous = (tmp_path / certificates.BUNDLE).read_bytes()
    with pytest.raises(ValueError):
        certificates.configure(tmp_path, key)
    assert (tmp_path / certificates.BUNDLE).read_bytes() == previous
    invalid = tmp_path / "invalid.pem"
    invalid.write_text("not a certificate")
    with pytest.raises((ValueError, ssl.SSLError)):
        certificates.configure(tmp_path, invalid)
    assert (tmp_path / certificates.BUNDLE).read_bytes() == previous


def test_bad_explicit_override_is_visible_and_not_silently_ignored(tmp_path):
    bad = str(tmp_path / "missing.pem")
    assert certificates.inspect({"REQUESTS_CA_BUNDLE": bad})[0]["status"] == "invalid"
    with pytest.raises(OSError):
        certificates.environment_for(tmp_path, {"REQUESTS_CA_BUNDLE": bad}, {})


def test_profile_bundle_changes_require_reconfiguration(tmp_path, tls_server):
    certificates.configure(tmp_path, tls_server[1])
    with (tmp_path / certificates.BUNDLE).open("ab") as output:
        output.write(b"\n")
    with pytest.raises(ValueError, match="changed"):
        network.environment_for(tmp_path, {})


def test_standalone_python_recovers_existing_os_bundle(tls_server, monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(certificates.ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    monkeypatch.setattr(certificates, "system_bundle_candidates", lambda: [tls_server[1]])
    assert certificates.system_roots()
