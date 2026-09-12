"""Native project instructions route new sessions once and preserve user text."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_start_guidance import BEGIN, END, add_start_guidance, guidance, managed_block


CLIENTS = ("codex", "cursor", "claude", "grok", "kimi")


def plan(project, client, files=None):
    files = {} if files is None else files
    notes = []
    add_start_guidance(files, notes, client, project)
    return files, notes


def test_all_clients_share_one_agents_block_and_resume_contract(tmp_path):
    original = "# Project rules\n\nKeep this user's instructions.\n"
    (tmp_path / "AGENTS.md").write_text(original)
    rendered = []
    for client in CLIENTS:
        files, notes = plan(tmp_path, client)
        text = files[tmp_path / "AGENTS.md"]
        rendered.append(text)
        assert text.endswith(original)
        assert text.count(BEGIN) == text.count(END) == 1
        assert "command checks saved initialization itself" in text
        assert "no configuration inspection is needed beforehand" in text
        assert ".agents/scripts/vaws_start.py --client CLIENT" in text
        assert "--context-file PATH" in text
        assert all(name in text for name in CLIENTS)
        assert "Skill" not in text
        assert "Resume keeps the earlier W, task and environment" in text
        assert "do not prepare, update or create another directory" in text
        assert "absolute paths under W" in text
        assert "do not repeat session setup" in text
        assert notes[-1]["skill_required"] is False
        assert notes[-1]["resume"] == "reuse-existing-workspace"
        repeated, _ = plan(tmp_path, client, dict(files))
        assert repeated == files
    assert len(set(rendered)) == 1
    assert (tmp_path / "AGENTS.md").read_text() == original


def test_claude_imports_agents_without_replacing_custom_instructions(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    custom = "# Claude preferences\n\nPreserve my review guidance.\n"
    claude.write_text(custom)
    files, _ = plan(tmp_path, "claude")
    assert files[claude].count("@AGENTS.md") == 1
    assert files[claude].endswith(custom)
    assert files[claude].count(BEGIN) == files[claude].count(END) == 1
    repeated, _ = plan(tmp_path, "claude", dict(files))
    assert repeated == files


def test_claude_existing_agents_import_does_not_get_another_block(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    original = "\n".join(("# Custom", "@AGENTS.md", "Keep this text.", ""))
    claude.write_text(original)
    files, _ = plan(tmp_path, "claude")
    assert claude not in files
    assert claude.read_text() == original


def test_cursor_generated_rule_is_always_applied_and_names_cursor(tmp_path):
    files, _ = plan(tmp_path, "cursor")
    rule = files[tmp_path / ".cursor/rules/vaws-session-start.mdc"]
    assert rule.startswith("---\n")
    assert "\nalwaysApply: true\n" in rule.split("---", 2)[1]
    assert "vaws_start.py --client cursor" in rule
    assert "--client CLIENT" not in rule
    assert "command checks saved initialization itself" in rule
    assert rule.count(BEGIN) == rule.count(END) == 1


def test_cursor_preserves_an_existing_custom_rule(tmp_path):
    rule = tmp_path / ".cursor/rules/vaws-session-start.mdc"
    rule.parent.mkdir(parents=True)
    custom = "---\nalwaysApply: false\n---\n\nUser-owned startup rule.\n"
    rule.write_text(custom)
    files, notes = plan(tmp_path, "cursor")
    assert rule not in files
    assert rule.read_text() == custom
    assert any(note.get("reason") == "custom-session-start-rule" for note in notes)


def test_cursor_refresh_preserves_user_text_outside_its_generated_block(tmp_path):
    files, _ = plan(tmp_path, "cursor")
    rule = tmp_path / ".cursor/rules/vaws-session-start.mdc"
    custom = "\n## Local Cursor instructions\nKeep this user-authored paragraph.\n"
    files[rule] += custom
    refreshed, _ = plan(tmp_path, "cursor", dict(files))
    assert refreshed[rule].endswith(custom)
    assert "\nalwaysApply: true\n" in refreshed[rule]
    assert refreshed[rule].count(BEGIN) == refreshed[rule].count(END) == 1


def test_managed_block_updates_only_owned_text():
    original = "User introduction.\n" + BEGIN + "\nOld guidance.\n" + END + "\nUser conclusion.\n"
    updated = managed_block(original, "New guidance.")
    assert updated == "User introduction.\n" + BEGIN + "\nNew guidance.\n" + END + "\nUser conclusion.\n"
    assert managed_block(updated, "New guidance.") == updated


@pytest.mark.parametrize("original", [
    BEGIN + "\nUnfinished block.\n",
    "Orphan end marker.\n" + END,
    END + "\nReversed markers.\n" + BEGIN,
    BEGIN + "\n" + BEGIN + "\n" + END,
    BEGIN + "\n" + END + "\n" + END,
])
def test_malformed_markers_are_rejected(original):
    with pytest.raises(ValueError, match="markers"):
        managed_block(original, guidance())


@pytest.mark.parametrize("client,relative,body", [
    ("kimi", "AGENTS.md", BEGIN + "\nUnfinished instructions.\n"),
    ("claude", "CLAUDE.md", "\n".join((BEGIN, "@AGENTS.md", ""))),
    ("cursor", ".cursor/rules/vaws-session-start.mdc", BEGIN + "\nUnfinished rule.\n"),
])
def test_malformed_existing_projection_does_not_get_silently_replaced(tmp_path, client, relative, body):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    with pytest.raises(ValueError, match="markers"):
        plan(tmp_path, client)
    assert path.read_text() == body
