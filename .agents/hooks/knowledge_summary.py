#!/usr/bin/env python3
"""Thin native final-response adapter over the installed knowledge package."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402


def in_project(payload: dict, *, client: str, project: Path) -> bool:
    """Only consume responses from the project selected during hook setup."""
    def contains(value):
        if not isinstance(value, str) or not value.strip():
            return False
        if os.name == "nt" and re.fullmatch(r"/mnt/[a-zA-Z](?:/.*)?", value):
            from vaws_local_owner import managed_path
            value = managed_path(value, windows=True)
        path = Path(value).expanduser()
        if not path.is_absolute():
            return False
        if path.resolve().is_relative_to(project.resolve()):
            return True
        from vaws_local_state import shared_workspace_root
        return shared_workspace_root(path) == shared_workspace_root(project)

    if "cwd" in payload:
        return contains(payload["cwd"])
    roots = payload.get("workspace_roots")
    if client == "cursor" and isinstance(roots, list):
        return bool(roots) and all(contains(root) for root in roots)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", required=True)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--environment-receipt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.environment_receipt:
        os.environ["VAWS_ENV_RECEIPT"] = args.environment_receipt
    try:
        # This optional hook must not interrupt a completed response when the
        # package or workspace environment is unavailable.
        with contextlib.redirect_stderr(io.StringIO()):
            ensure_workspace_interpreter(repo_root=ROOT, packages=("vaws_knowledge",))
    except SystemExit as exc:
        if exc.code:
            print("{}")
        return 0
    except Exception:
        print("{}")
        return 0
    scoped = False
    try:
        from vaws_knowledge.summary_hook import capture_summary
        from vaws_knowledge_service import service_config

        raw = sys.stdin.read(1_048_577)
        if len(raw) <= 1_048_576:
            payload = json.loads(raw)
            if isinstance(payload, dict) and in_project(payload, client=args.client, project=args.project):
                scoped = True
                native = {}
                if args.client == "kimi":
                    from vaws_kimi_summary import summary_result
                    payload, native = summary_result(payload)
                elif args.client == "cursor":
                    from vaws_cursor_summary import summary_result
                    payload, native = summary_result(payload)
                result = capture_summary(payload, config=service_config(args.project), client=args.client)
                facts = {"event": "knowledge_summary", "client": args.client,
                         "status": result.get("status", "unknown") if isinstance(result, dict) else "unknown"}
                if native:
                    facts["native"] = native
                from vaws_workspace_update import redact
                print(redact(json.dumps(facts, ensure_ascii=False)), file=sys.stderr)
    except Exception as exc:
        if scoped:
            from vaws_workspace_update import redact
            print(json.dumps({"event": "knowledge_summary", "client": args.client, "status": "failed",
                              "error_type": type(exc).__name__, "error": redact(str(exc))[:300]},
                             ensure_ascii=False), file=sys.stderr)
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
