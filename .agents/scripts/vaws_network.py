#!/usr/bin/env python3
"""Inspect existing enterprise routes and repair workspace transport settings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_network import REPORT, check, discover, owner, read_profile, native_check, atomic, environment_for

if __name__ == "__main__":
    from vaws_diagnostics_adapter import bootstrap
    _vaws_entry = bootstrap(__file__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="read existing configuration and the last observation; no network")
    inspect = sub.add_parser("check", help="bounded read-only probes of existing routes")
    inspect.add_argument("--targets", nargs="+", choices=("github", "pypi", "models"), default=["github", "pypi", "models"])
    inspect.add_argument("--apply", action="store_true", help="save source references and transport defaults locally; no global settings")
    inspect.add_argument("--native", action="store_true", help="also test actual Git and authenticated gh with 30-second deadlines")
    certificates = sub.add_parser("certificates", help="reuse system trust for VAWS clients, without changing the OS store")
    certificates.add_argument("--ca-bundle", type=Path, help="explicitly trust this PEM CA file from a verified organizational source")
    execute = sub.add_parser("run", help="run a command with the saved transport and bounded process-tree cleanup")
    execute.add_argument("--target", choices=("github", "pypi", "models"), default="github")
    execute.add_argument("--timeout", type=float, default=900)
    execute.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "check":
        result = check(ROOT, targets=args.targets, apply=args.apply)
        if args.native:
            result["native"] = native_check(ROOT)
            if any(item["status"] != "ok" for item in result["native"].values()):
                result["status"] = "partial"
            atomic(owner(ROOT) / REPORT, result)
    elif args.command == "certificates":
        from vaws_certificates import configure
        result = configure(ROOT, args.ca_bundle)
    elif args.command == "run":
        from vaws_process_wait import owned, wait_with_progress
        from vaws_github import github_git_environment
        import subprocess
        command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not command:
            parser.error("run requires a command after --")
        environment = github_git_environment(environment_for(ROOT, target=args.target))
        with owned(command, env=environment, stdin=subprocess.DEVNULL) as process:
            return wait_with_progress(process, stage="network_command", timeout=args.timeout)
    else:
        _, discovery = discover(ROOT)
        path = owner(ROOT) / REPORT
        result = {"network_checked": False, "discovery": discovery,
                  "profile": read_profile(ROOT),
                  "last_check": json.loads(path.read_text(encoding="utf-8")) if path.exists() else None}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "partial" else 1


if __name__ == "__main__":
    import subprocess
    try:
        raise SystemExit(_vaws_entry.run(main))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "failed", "category": "deadline" if isinstance(exc, subprocess.TimeoutExpired) else "local_configuration",
                          "error_type": type(exc).__name__, "remedy": "Inspect network status and explicit proxy/CA settings; no TLS bypass was applied."}))
        raise SystemExit(2)
