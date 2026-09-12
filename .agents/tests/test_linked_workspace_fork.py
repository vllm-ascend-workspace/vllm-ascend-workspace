"""Native conversation forks preserve real Git edits and their selected runtime."""
import json
from pathlib import Path
import sys

import pytest

from test_native_workspace import repository, state
from test_native_worktree_setup import make_repository, setup as preparation
from test_kimi_native_setup import adapter
from vaws_native_workspace import create_linked_workspace, git


def index_bytes(path):
    index = Path(git(path, "rev-parse", "--git-path", "index").decode().strip())
    return (index if index.is_absolute() else path / index).read_bytes()


def test_linked_copy_preserves_stage_working_untracked_and_component_edits(tmp_path):
    source = repository(tmp_path / "source 用户")
    child = repository(tmp_path / "child")
    (source / ".gitignore").write_text(".vaws-local/\n*.cache\n")
    for name in ("vllm", "unused"):
        git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(child), name)
    git(source, "commit", "-qam", "components")
    git(source, "submodule", "deinit", "unused")
    (source / "file.txt").write_text("staged\n")
    git(source, "add", "file.txt")
    (source / "file.txt").write_text("working\n")
    (source / "new 中文.txt").write_text("ordinary untracked\n")
    (source / "intent.txt").write_text("intent-to-add\n")
    git(source, "add", "-N", "intent.txt")
    (source / "ignored.cache").write_text("not editing state")
    module = source / "vllm"
    (module / "file.txt").write_text("staged operator\n")
    git(module, "add", "file.txt")
    (module / "file.txt").write_text("working operator\n")
    (module / "new.bin").write_bytes(b"untracked\0operator")
    before, child_before, index, child_index = state(source), state(module), index_bytes(source), index_bytes(module)
    target = tmp_path / "fork"
    assert create_linked_workspace(source, target)["state"] == "ready"
    assert (target / ".git").is_file() and (target / "vllm/.git").is_file()
    assert git(source, "rev-parse", "--path-format=absolute", "--git-common-dir") == git(target, "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert git(module, "rev-parse", "--path-format=absolute", "--git-common-dir") == git(target / "vllm", "rev-parse", "--path-format=absolute", "--git-common-dir")
    assert state(target) == state(source) == before
    assert state(target / "vllm") == state(module) == child_before
    assert index_bytes(source) == index and index_bytes(module) == child_index
    assert (target / "file.txt").read_text() == "working\n"
    assert (target / "new 中文.txt").read_text() == "ordinary untracked\n"
    assert (target / "vllm/new.bin").read_bytes() == b"untracked\0operator"
    assert not (target / "ignored.cache").exists()
    assert not (target / "unused/.git").exists()
    # A later edit and stage in the fork leaves both original indexes alone.
    (target / "file.txt").write_text("fork-only\n")
    git(target, "add", "file.txt")
    assert state(source) == before and index_bytes(source) == index


@pytest.mark.parametrize("dirty,available", [(True, True), (False, True), (False, False)])
def test_kimi_fork_keeps_current_head_and_saved_pin_without_upstream_check(tmp_path, monkeypatch, capsys, dirty, available):
    f = make_repository(tmp_path, monkeypatch)
    # The source's native session is older than an available prepared upstream.
    # Only dependency/wiring effects are stubbed; Git/index/files use real trees.
    if dirty:
        (f.target / "README").write_text("staged draft\n")
        git(f.target, "add", "README")
        (f.target / "README").write_text("working draft\n")
        (f.target / "new.txt").write_text("untracked draft\n")
    before, index = state(f.target), index_bytes(f.target)
    selected = f.target / ".vaws-local/environment-selection" / f"{sys.platform}.json"
    if available:
        selected.parent.mkdir(parents=True)
        selected.write_text(json.dumps(f.receipt_old))
    native_saved = preparation.saved_ready
    def saved(root):
        if root == f.target and not available:
            raise preparation.EnvironmentError("source environment unavailable")
        return native_saved(root)
    monkeypatch.setattr(preparation, "saved_ready", saved)
    monkeypatch.setattr(adapter, "prepare_worktree", preparation.prepare_worktree)
    result = adapter.setup(f.source, f.target, {"session_id": "forked-native", "source": "fork"})
    assert json.loads(capsys.readouterr().err)["kimi_session_setup"]["update"] == {"status": "kept", "reason": "fork_source"}
    target = Path(result["hookSpecificOutput"]["cwd"])
    assert state(target) == state(f.target) == before
    assert index_bytes(f.target) == index
    assert git(target, "rev-parse", "HEAD").decode().strip() == f.old != f.new
    assert not f.calls["prepare"]
    assert f.calls["configure"][-1]["receipt"] == f.receipt_old
    assert f.calls["configure"][-1]["head"] == f.old
    selection = json.loads((target / ".vaws-local/environment-selection" / f"{sys.platform}.json").read_text())
    assert selection.pop("knowledge") == {"status": "ready", "ready": True}
    assert selection == f.receipt_old
    if dirty:
        assert (target / "README").read_text() == "working draft\n"
        assert (target / "new.txt").read_text() == "untracked draft\n"


def test_failed_fork_copy_cannot_be_resumed_as_a_ready_worktree(tmp_path, monkeypatch):
    from vaws_native_workspace import WorkspaceCopyError
    import vaws_native_workspace

    source = repository(tmp_path / "source")
    (source / ".gitignore").write_text(".vaws-local/\n")
    git(source, "add", ".gitignore")
    git(source, "commit", "-qm", "ignore local state")
    native_copy = vaws_native_workspace.shutil.copy2
    def changing_copy(src, dst):
        result = native_copy(src, dst)
        if Path(src).name == "file.txt":
            (source / "file.txt").write_text("concurrent edit\n")
        return result
    monkeypatch.setattr(vaws_native_workspace.shutil, "copy2", changing_copy)
    payload = {"session_id": "failed-fork", "source": "fork"}
    with pytest.raises(WorkspaceCopyError, match="source changed while copying"):
        adapter.setup(source, source, payload)
    with pytest.raises(ValueError, match="fork copy did not finish"):
        adapter.setup(source, source, payload)
    assert (source / "file.txt").read_text() == "concurrent edit\n"
