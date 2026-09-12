"""Project instructions for one native session's selected editing directory.

The native clients read these files themselves. Planning does not start a
session, invoke a Skill, or infer identity from the current directory.
"""
from __future__ import annotations

from pathlib import Path


BEGIN = "<!-- BEGIN VAWS session-start -->"
END = "<!-- END VAWS session-start -->"


def guidance(client: str | None = None) -> str:
    name = client or "CLIENT"
    selection = (" Replace CLIENT with your native client: codex, cursor, claude, grok or kimi."
                 if client is None else "")
    return (
        "For a new native session, make the first repository action "
        f"`uv run --no-project python .agents/scripts/vaws_start.py --client {name}`."
        + selection + " Add `--context-file PATH` when the native hook supplied that context "
        "and the command cannot obtain it from the client environment (official Kimi needs it). "
        "The command checks saved initialization itself and reports missing first-use setup; "
        "no configuration inspection is needed beforehand. If the native hook explicitly "
        "reports a prepared workspace W and selected environment, use them directly.\n\n"
        "Use the returned `workspace` as W: shell tools use W as cwd (or `cd W && ...`), "
        "and file, search and patch tools use absolute paths under W. Sources and the selected "
        "environment are already bound; do not repeat session setup. Official Kimi calls to "
        "the task, remote-dev and knowledge MCP providers also carry the returned `context_file`. "
        "Other clients receive context through hooks; if a tool reports missing context, pass "
        "the existing `context_file`. Use configured knowledge tools when useful.\n\n"
        "Resume keeps the earlier W, task and environment; do not prepare, update or create "
        "another directory. Client UI/default cwd can remain at the original project.\n"
    )


def managed_block(original: str, body: str) -> str:
    block = BEGIN + "\n" + body.rstrip() + "\n" + END
    if BEGIN in original or END in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise ValueError("VAWS session-start instruction markers are incomplete or duplicated")
        start, end = original.index(BEGIN), original.index(END)
        if end < start:
            raise ValueError("VAWS session-start instruction markers are reversed")
        return original[:start] + block + original[end + len(END):]
    return block + "\n\n" + original


def add_start_guidance(files: dict[Path, str], notes: list, client: str, project: Path) -> None:
    """Plan only generated blocks; keep all other project instructions intact."""
    path = project / "AGENTS.md"
    original = files.get(path)
    if original is None:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    files[path] = managed_block(original, guidance())
    if client == "claude":
        projection = project / "CLAUDE.md"
        original = files.get(projection)
        if original is None:
            original = projection.read_text(encoding="utf-8") if projection.exists() else ""
        if BEGIN in original or END in original or "@AGENTS.md" not in original.splitlines():
            files[projection] = managed_block(original, "@AGENTS.md")
    elif client == "cursor":
        projection = project / ".cursor/rules/vaws-session-start.mdc"
        original = files.get(projection)
        if original is None:
            original = projection.read_text(encoding="utf-8") if projection.exists() else ""
        if original and BEGIN not in original and END not in original:
            notes.append({"path": str(projection), "action": "preserved", "reason": "custom-session-start-rule"})
        elif original:
            files[projection] = managed_block(original, guidance("cursor"))
        else:
            body = managed_block("", guidance("cursor"))
            files[projection] = "---\ndescription: Select this native session's editing workspace\nalwaysApply: true\n---\n\n" + body
    notes.append({"client": client, "path": str(path), "action": "configured",
                  "reason": "new-session-project-guidance", "skill_required": False,
                  "resume": "reuse-existing-workspace"})
