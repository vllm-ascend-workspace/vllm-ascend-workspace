"""Discover existing enterprise routes, observe them, and reuse safe selections.

Bootstrap-only standard library support: this module also runs on Python 3.10.
No credentials, private hostnames or server response text enter saved reports.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

PROXIES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
PROFILE = ".vaws-local/network.json"
REPORT = ".vaws-local/network-check.json"
DEFAULTS = {"UV_SYSTEM_CERTS": "true", "UV_NATIVE_TLS": "true", "UV_HTTP_TIMEOUT": "60", "UV_CONCURRENT_DOWNLOADS": "4",
            "GIT_HTTP_LOW_SPEED_LIMIT": "1024", "GIT_HTTP_LOW_SPEED_TIME": "30"}
REMEDIES = {
    "dns": "Check the configured proxy hostname or DNS; compare another existing route.",
    "certificate": "Use the installed system trust store or an explicit enterprise CA bundle; never disable TLS verification.",
    "proxy_auth": "The proxy requires authentication; repair its existing credential source.",
    "authentication": "The endpoint is reachable but requires its own authentication.",
    "access_denied": "Inspect endpoint permissions or the proxy policy; a 403 is not a DNS failure.",
    "rate_limit": "Respect the server retry window or configure authorized service authentication.",
    "deadline": "The whole probe exceeded its deadline, including DNS and response reads; compare another route.",
    "timeout": "The connection or read timed out; compare an existing route before retrying.",
    "connection": "Check proxy reachability and the endpoint route.",
    "connection_refused": "The configured proxy or endpoint refused the connection.",
}


@dataclass
class Route:
    source: str
    proxy: str | None = field(default=None, repr=False)


def owner(root: Path) -> Path:
    marker = root / ".vaws-local/native-workspace.json"
    if marker.is_file():
        facts = json.loads(marker.read_text(encoding="utf-8"))
        if facts.get("state") == "ready" and facts.get("workspace") == str(root):
            return Path(facts["project_root"])
    return root


def _config(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), "config", *arguments],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=3)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _proxy(value: str) -> str | None:
    if not value or any(c in value for c in "\r\n\x00"):
        return None
    value = value if "://" in value else "http://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        parsed.port  # Reject invalid port syntax even when a default port is used.
        if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            return None
    except ValueError:
        return None
    return value


def windows_winhttp() -> str:
    try:
        result = subprocess.run(["netsh", "winhttp", "show", "proxy"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, errors="replace", timeout=3,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        # Labels are localized; parse only an actual host:port, never prose.
        values = re.findall(r"(?:https?://)?(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}", result.stdout)
        return values[0] if result.returncode == 0 and values else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def windows_internet_settings() -> tuple[str, bool]:
    if os.name != "nt":
        return "", False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            def value(name, default=None):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return default
            setting = value("ProxyServer", "") if value("ProxyEnable", 0) else ""
            entries = dict(part.split("=", 1) for part in setting.split(";") if "=" in part)
            return entries.get("https", entries.get("http", setting)), bool(value("AutoConfigURL") or value("AutoDetect", 0))
    except OSError:
        return "", False


def resolve_route(root: Path, source: str, env: dict) -> Route:
    if source in {"direct", "inherited"}:
        return Route(source, "" if source == "direct" else None)
    value = ""
    if source.startswith("env:") and source[4:] in PROXIES:
        value = env.get(source[4:], "")
    elif source == "git:https":
        value = _config(root, "--get-urlmatch", "http.proxy", "https://github.com")
    elif source == "windows:internet_settings":
        value = windows_internet_settings()[0]
    elif source == "windows:winhttp" and os.name == "nt":
        value = windows_winhttp()
    parsed = _proxy(value)
    if not parsed:
        raise ValueError("selected proxy source is no longer available; rerun vaws_network.py check --apply")
    return Route(source, parsed)


def discover(root: Path, environment: dict | None = None) -> tuple[list[Route], dict]:
    env = os.environ if environment is None else environment
    routes = [Route("inherited"), Route("direct", "")]
    seen = set()
    notes = []

    def add(source, value):
        parsed = _proxy(value)
        if parsed and parsed not in seen:
            seen.add(parsed)
            routes.append(Route(source, parsed))
        elif value and not parsed:
            notes.append({"source": source, "category": "unsupported_proxy"})

    for name in PROXIES:
        if env.get(name):
            add("env:" + name, env[name])
    add("git:https", _config(root, "--get-urlmatch", "http.proxy", "https://github.com"))
    if os.name == "nt":
        setting, pac = windows_internet_settings()
        add("windows:internet_settings", setting)
        if pac:
            notes.append({"source": "windows:automatic_proxy", "category": "pac_requires_resolution"})
        add("windows:winhttp", windows_winhttp())
    return routes[:6], {"routes": [{"source": r.source, "authenticated": bool(r.proxy and urlsplit(r.proxy).username)} for r in routes[:6]],
                        "notes": notes, "no_proxy_configured": bool(env.get("NO_PROXY") or env.get("no_proxy")),
                        "certificate_sources": [name for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO") if env.get(name)]}


def probe(url: str, route: Route, *, environment: dict | None = None, deadline: float = 7,
          size: int = 32768, worker: Path | None = None, operation: str = "probe") -> dict:
    env = dict(os.environ if environment is None else environment)
    env.pop("VAWS_PROBE_PROXY", None)
    if route.proxy is not None:
        env["VAWS_PROBE_PROXY"] = route.proxy
        # Compare the selected proxy itself, independent of inherited bypasses.
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
    from vaws_certificates import CA_ENV
    ca = next((env[name] for name in CA_ENV if env.get(name)), None)
    if ca:
        env["VAWS_PROBE_CA"] = ca
    else:
        env.pop("VAWS_PROBE_CA", None)
    command = [sys.executable, "-I", str(worker or Path(__file__).with_name("vaws_network_probe.py"))]
    started = time.monotonic()
    from vaws_process_wait import owned
    try:
        with owned(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env) as process:
            output, _ = process.communicate(json.dumps({"url": url, "bytes": size, "operation": operation}).encode(), timeout=deadline)
    except OSError as exc:
        return {"source": route.source, "status": "failed", "category": "probe_process", "error_type": type(exc).__name__}
    except subprocess.TimeoutExpired:
        # The owner closes the whole tree before this result, including a
        # Windows venv redirector's actual interpreter and inherited pipes.
        return {"source": route.source, "status": "failed", "category": "deadline",
                "seconds": round(time.monotonic() - started, 3), "child_reaped": True}
    try:
        result = json.loads(output)
        if not isinstance(result, dict) or process.returncode:
            raise ValueError("invalid worker result")
    except (ValueError, TypeError):
        result = {"status": "failed", "category": "probe_process", "returncode": process.returncode}
    return {"source": route.source, **result}


def fetch_bytes(url: str, *, limit: int = 4 * 1024 * 1024, deadline: float = 7) -> bytes:
    """Bounded public metadata read; response is internal, never diagnostic output."""
    import base64
    result = probe(url, Route("inherited"), size=limit, deadline=deadline, operation="fetch")
    if result["status"] != "ok":
        raise ValueError("HTTP metadata read failed: " + result["category"])
    return base64.b64decode(result["data"], validate=True)


def endpoints(root: Path, targets: tuple[str, ...]) -> dict[str, str]:
    result = {}
    if "github" in targets:
        result["github"] = "https://api.github.com/meta"
    if "pypi" in targets:
        result["pypi"] = "https://pypi.org/simple/packaging/"
        lock = root / "uv.lock"
        if lock.is_file():
            match = re.search(r'https://files\.pythonhosted\.org/packages/[^"\s]+-py3-none-any\.whl', lock.read_text(encoding="utf-8"))
            if match:
                result["pypi_artifact"] = match[0]
        mirror = os.environ.get("VAWS_PYPI_MIRROR", "").strip().rstrip("/")
        if mirror:
            parsed = urlsplit(mirror)
            if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment:
                result["pypi_mirror"] = mirror + "/packaging/"
    if "models" in targets:
        result["models"] = "https://huggingface.co/api/models/qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    return result


def select(results: list[dict]) -> dict | None:
    healthy = [item for item in results if item.get("status") == "ok"]
    if not healthy:
        return None
    best = min(healthy, key=lambda item: item.get("seconds", float("inf")))
    inherited = next((item for item in healthy if item["source"] == "inherited"), None)
    # Avoid needless route changes based on marginal timing variation.
    return inherited if inherited and inherited["seconds"] <= best["seconds"] * 1.25 else best


def inherited_reference(url: str, routes: list[Route]) -> str:
    """Materialize urllib's inherited route so native Git uses the same route."""
    from urllib.request import getproxies, proxy_bypass
    parsed = urlsplit(url)
    if proxy_bypass(parsed.hostname):
        return "direct"
    value = getproxies().get(parsed.scheme, "")
    if not value:
        return "direct"
    normalized = _proxy(value)
    return next((route.source for route in routes if route.proxy and route.proxy == normalized), "inherited")


def check(root: Path, *, targets=("github", "pypi", "models"), apply=False) -> dict:
    root = owner(root.resolve())
    routes, discovery = discover(root)
    from vaws_certificates import environment_for as certificate_environment, inspect as inspect_certificates
    environment = certificate_environment(root, dict(os.environ), read_profile(root))
    discovery["certificates"] = inspect_certificates(environment)
    targets = endpoints(root, tuple(targets))
    jobs = [(name, url, route) for name, url in targets.items() for route in routes]
    started = time.monotonic()
    observations = {name: [] for name in targets}
    print(f"VAWS network: checking {len(targets)} endpoints over {len(routes)} existing routes", file=sys.stderr, flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [(name, pool.submit(probe, url, route, environment=environment)) for name, url, route in jobs]
        for name, future in futures:
            item = future.result()
            if item["status"] != "ok":
                item["remedy"] = REMEDIES.get(item["category"], "Inspect the local network configuration before retrying.")
            observations[name].append(item)
            print(f"VAWS network: {name} via {item['source']}: {item['category']}", file=sys.stderr, flush=True)
    selected = {name: choice["source"] for name, results in observations.items() if (choice := select(results))}
    report = {"schema": 1, "checked_at": datetime.now(timezone.utc).isoformat(), "discovery": discovery,
              "status": "ready" if len(selected) == len(targets) else "partial", "selected": selected,
              "observations": observations, "seconds": round(time.monotonic() - started, 3),
              "scope": "HTTP reachability and bounded samples; metadata success does not prove native tool authentication or every redirected artifact host."}
    if apply and selected:
        previous = read_profile(root)
        choices = {**previous.get("routes", {})}
        for group, endpoint in (("github", "github"), ("pypi", "pypi_artifact"), ("models", "models")):
            if endpoint in selected or group in selected:
                key = endpoint if endpoint in selected else group
                source = selected[key]
                choices[group] = inherited_reference(targets[key], routes) if source == "inherited" else source
        bypass = list(previous.get("no_proxy", []))
        for name, source in selected.items():
            if source == "direct":
                bypass.append(urlsplit(targets[name]).hostname)
        profile = {**previous, "schema": 1, "routes": choices, "no_proxy": sorted(set(bypass)),
                   "transport": {**DEFAULTS, **previous.get("transport", {})},
                   "checked_at": report["checked_at"]}
        atomic(root / PROFILE, profile)
        report["applied"] = True
        report["applied_routes"] = choices
    elif apply:
        report["applied"] = False
    atomic(root / REPORT, report)
    return report


def native_check(root: Path) -> dict:
    """Verify actual Git transport and gh authentication independently of HTTP."""
    from vaws_process_wait import run_captured
    from vaws_github import github_git_environment
    environment = github_git_environment(environment_for(root, target="github"))
    environment.update(GH_PROMPT_DISABLED="1", NO_COLOR="1", GIT_TERMINAL_PROMPT="0")
    checks = {"git": ["git", "-c", "http.sslVerify=true", "ls-remote", "https://github.com/vllm-ascend-workspace/vllm-ascend-workspace.git", "HEAD"],
              "gh": ["gh", "api", "--hostname", "github.com", "user", "--jq", ".login"]}
    results = {}
    for name, command in checks.items():
        started = time.monotonic()
        try:
            result = run_captured(command, stage="network_" + name, timeout=30, env=environment, cwd=root)
            category = "ok" if result.returncode == 0 else "native_tool"
            message = result.stderr.lower()
            for needle, label in (("407", "proxy_auth"), ("certificate", "certificate"), ("resolve host", "dns"),
                                  ("401", "authentication"), ("403", "access_denied"), ("auth login", "authentication")):
                if result.returncode and needle in message:
                    category = label
                    break
            results[name] = {"status": "ok" if result.returncode == 0 else "failed", "category": category, "returncode": result.returncode}
        except FileNotFoundError:
            results[name] = {"status": "missing", "category": "prerequisite"}
        except subprocess.TimeoutExpired:
            results[name] = {"status": "failed", "category": "deadline", "tree_cleanup": True}
        results[name]["seconds"] = round(time.monotonic() - started, 3)
        if results[name]["category"] in REMEDIES:
            results[name]["remedy"] = REMEDIES[results[name]["category"]]
        print(f"VAWS network: native {name}: {results[name]['category']}", file=sys.stderr, flush=True)
    return results


def atomic(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_profile(root: Path) -> dict:
    path = owner(root) / PROFILE
    if not path.is_file():
        return {}
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict) or result.get("schema") != 1:
        raise ValueError("unsupported local network profile; inspect .vaws-local/network.json")
    return result


def environment_for(root: Path, base: dict | None = None, *, target: str = "github") -> dict:
    """Apply a saved selection only; no network probes or credential persistence."""
    env = dict(os.environ if base is None else base)
    config = read_profile(root)
    if not config:
        return env
    selected = config.get("routes", {}).get(target, config.get("proxy_source", "inherited"))
    if selected != "inherited":
        route = resolve_route(owner(root), selected, env)
        for key in PROXIES:
            env.pop(key, None)
        if route.proxy:
            env.update(HTTPS_PROXY=route.proxy, HTTP_PROXY=route.proxy)
    bypass = [part.strip() for part in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",") if part.strip()]
    bypass += ["localhost", "127.0.0.1", "::1", *config.get("no_proxy", [])]
    env["NO_PROXY"] = ",".join(dict.fromkeys(bypass))
    env.pop("no_proxy", None)
    for key, value in config.get("transport", {}).items():
        if key not in DEFAULTS or not isinstance(value, str):
            raise ValueError("unsupported transport setting in local network profile")
        env.setdefault(key, value)
    # Git's configured http.proxy takes precedence over HTTPS_PROXY. Apply the
    # tested GitHub route at URL scope, via child env, including uv Git children.
    git_source = config.get("routes", {}).get("github", config.get("proxy_source", "inherited"))
    if git_source != "inherited":
        git_route = resolve_route(owner(root), git_source, dict(os.environ if base is None else base))
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        if not 0 <= count <= 100:
            raise ValueError("GIT_CONFIG_COUNT is outside the supported range")
        env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com.proxy"
        env[f"GIT_CONFIG_VALUE_{count}"] = git_route.proxy or ""
        env["GIT_CONFIG_COUNT"] = str(count + 1)
        for key in tuple(env):
            if key.upper().startswith("GIT_TRACE") or key.upper() == "GIT_CURL_VERBOSE":
                env.pop(key)
    from vaws_certificates import environment_for as certificate_environment
    return certificate_environment(root, env, config)


@contextmanager
def network_scope(root: Path, *, target: str = "github"):
    """CLI-scoped environment inheritance, restored after all setup workers exit."""
    original = dict(os.environ)
    configured = environment_for(root, original, target=target)
    changes = {key for key in original.keys() | configured.keys() if original.get(key) != configured.get(key)}
    try:
        for key in changes:
            if key in configured:
                os.environ[key] = configured[key]
            else:
                os.environ.pop(key, None)
        yield configured
    finally:
        for key in changes:
            if key in original:
                os.environ[key] = original[key]
            else:
                os.environ.pop(key, None)


def prepare(root: Path) -> dict:
    """Cold setup discovers existing routes; completed setup never calls this."""
    if read_profile(root).get("checked_at"):
        return {"status": "configured", "network_checked": False, "reused": True}
    routes, discovery = discover(root)
    from vaws_certificates import configure
    if not read_profile(root).get("certificates"):
        configure(root)
    if len(routes) == 2 and not os.environ.get("VAWS_PYPI_MIRROR") and not discovery["certificate_sources"]:
        return {"status": "not_needed", "network_checked": False, "discovery": discovery}
    return check(root, targets=("github", "pypi"), apply=True)
