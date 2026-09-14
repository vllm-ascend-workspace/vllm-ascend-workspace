"""Bridge already trusted system roots and explicitly supplied IT CA bundles.

Never harvest trust from an unverified endpoint or modify the OS trust store.
This bootstrap module uses only the standard library.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import ssl

CA_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")
BUNDLE = ".vaws-local/certificates/ca-bundle.pem"


def system_bundle_candidates():
    # Standalone Python distributions can retain build-machine OpenSSL paths.
    # These are OS-maintained bundles, not certificates harvested from a server.
    return (Path("/etc/ssl/certs/ca-certificates.crt"), Path("/etc/pki/tls/certs/ca-bundle.crt"),
            Path("/etc/ssl/cert.pem")) if os.name != "nt" else ()


def system_roots():
    context = ssl.create_default_context()
    roots = set(context.get_ca_certs(binary_form=True))
    if not roots and not os.environ.get("SSL_CERT_FILE"):
        for path in system_bundle_candidates():
            if path.is_file():
                context.load_verify_locations(cafile=str(path))
                roots.update(context.get_ca_certs(binary_form=True))
    return roots


def inspect(environment=None):
    env = os.environ if environment is None else environment
    result = []
    for name in CA_ENV:
        if env.get(name):
            try:
                context = ssl.create_default_context(cafile=env[name])
                result.append({"source": name, "status": "valid", "certificates": len(context.get_ca_certs())})
            except (OSError, ssl.SSLError):
                result.append({"source": name, "status": "invalid", "remedy": "Repair this explicit CA file; system fallback must not hide it."})
    return result


def configure(root: Path, ca_bundle: Path | None = None) -> dict:
    """An explicit ca_bundle argument authorizes that local trust input.

    The system's effective trusted roots are copied, not downloaded. Supplied
    PEM CA files augment them for VAWS processes only. Validate before publish.
    """
    from vaws_network import atomic, owner, read_profile, PROFILE, DEFAULTS
    root = owner(root)
    roots = system_roots()
    supplied_hash = None
    if ca_bundle is not None:
        data = ca_bundle.read_bytes()
        if len(data) > 8 * 1024 * 1024 or b"PRIVATE KEY" in data:
            raise ValueError("Expected a PEM CA bundle of at most 8 MiB without private keys")
        supplied = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        supplied.load_verify_locations(cadata=data.decode("ascii"))
        certificates = supplied.get_ca_certs(binary_form=True)
        if not certificates:
            raise ValueError("The supplied PEM file contains no CA certificates")
        roots.update(certificates)
        supplied_hash = hashlib.sha256(data).hexdigest()
    if not roots:
        raise ValueError("No trusted roots available; select a verified-source PEM CA bundle")
    content = "".join(ssl.DER_cert_to_PEM_cert(cert) for cert in sorted(roots))
    # Check the exact combined output before making it available to children.
    ssl.create_default_context(cadata=content)
    path = root / BUNDLE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_bytes(content.encode("ascii"))
    temporary.replace(path)
    facts = {"source": "system_and_supplied" if ca_bundle else "system", "certificates": len(roots),
             "sha256": hashlib.sha256(content.encode("ascii")).hexdigest()}
    if supplied_hash:
        facts["supplied_sha256"] = supplied_hash
    previous = read_profile(root)
    profile = {"schema": 1, **previous, "certificates": facts,
               "transport": {**DEFAULTS, **previous.get("transport", {})}}
    atomic(root / PROFILE, profile)
    return {"status": "configured", **facts, "os_trust_changed": False,
            "verification": "CA file parsed; run network check to verify endpoint chains"}


def environment_for(root: Path, environment: dict, profile: dict) -> dict:
    env = dict(environment)
    selected = next((env[name] for name in CA_ENV if env.get(name)), None)
    if selected:
        # Preserve independent client overrides, but bridge the primary explicit
        # bundle to clients that otherwise silently use certifi or another store.
        ssl.create_default_context(cafile=selected)
    elif profile.get("certificates"):
        from vaws_network import owner
        path = owner(root) / BUNDLE
        if hashlib.sha256(path.read_bytes()).hexdigest() != profile["certificates"]["sha256"]:
            raise ValueError("Local CA bundle changed; rerun vaws_network.py certificates")
        selected = str(path)
    if selected:
        for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO"):
            env.setdefault(name, selected)
    return env
