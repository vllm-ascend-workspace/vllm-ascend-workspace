"""Installation readiness and one native knowledge owner per mounted workspace."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

import vaws_environment as envs
import vaws_environment_link as links
import vaws_knowledge_service as knowledge
import vaws_local_owner as owner

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("knowledge_dependency_setup", ROOT / ".agents/scripts/vaws_deps.py")
deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deps)


def test_local_knowledge_defaults_to_bundled_model_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "shared_workspace_root", lambda root: root)
    monkeypatch.setattr(knowledge, "knowledge_identity", lambda root: {"origin_repo": "example/repo"})
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    assert knowledge.knowledge_server_env(tmp_path)["LITELLM_LOCAL_MODEL_COST_MAP"] == "true"
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "false")
    assert knowledge.knowledge_server_env(tmp_path)["LITELLM_LOCAL_MODEL_COST_MAP"] == "false"


def test_origin_probe_does_not_inherit_mcp_stdin_and_is_bounded(tmp_path, monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 5
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(knowledge.subprocess, "run", timeout)
    assert knowledge.origin_repo_from_git(tmp_path) == "local/unpublished"


def test_mounted_workspace_reserves_windows_owner_before_it_is_installed(monkeypatch):
    monkeypatch.setattr(owner, "os", SimpleNamespace(name="posix", environ={"WSL_DISTRO_NAME": "test"}))
    root = PurePosixPath("/mnt/d/work")
    assert owner.windows_mounted_workspace(root)
    monkeypatch.setattr(envs, "native_ready", lambda root: pytest.fail("mounted workspaces must not select a Linux owner"))
    def unavailable(root, **kw):
        raise envs.EnvironmentError("no Windows environment selection exists")
    monkeypatch.setattr(envs, "windows_ready", unavailable)
    with pytest.raises(envs.EnvironmentError, match="no Windows environment"):
        owner.managed_python(root)
    receipt = {"python": r"C:\ready\abc\Scripts\python.exe"}
    monkeypatch.setattr(envs, "windows_ready", lambda root: receipt)
    assert owner.managed_python(root) == "/mnt/c/ready/abc/Scripts/python.exe"
    assert owner.managed_receipt(root) == receipt
    assert not owner.windows_mounted_workspace(PurePosixPath("/opt/work"))
    monkeypatch.setattr(envs, "native_ready", lambda root: {"python": "/linux/ready/bin/python"})
    assert owner.managed_python(PurePosixPath("/opt/work")) == "/linux/ready/bin/python"


def test_windows_owner_receives_native_paths_and_explicit_environment(monkeypatch):
    root = PurePosixPath("/mnt/d/work")
    receipt = r"C:\ready\abc\.vaws-ready.json"
    monkeypatch.setattr(knowledge, "managed_receipt", lambda root: {"receipt": receipt})
    monkeypatch.setattr(knowledge, "windows_mounted_workspace", lambda root: True)
    monkeypatch.setattr(knowledge, "knowledge_server_env", lambda root: {
        "VAWS_KNOWLEDGE_CONFIG": "/mnt/d/work/.vaws-local/knowledge/service.json",
        "VAWS_KNOWLEDGE_PROJECT_ROOTS": "/mnt/d/work/.agents/knowledge",
        "VAWS_KNOWLEDGE_CANDIDATE_ROOT": "/mnt/d/work/.vaws-local/knowledge/candidate",
        "VAWS_KNOWLEDGE_STATE": "/mnt/d/work/.vaws-local/knowledge/instance",
        "VAWS_KNOWLEDGE_ORIGIN_REPO": "example/repo",
    })
    monkeypatch.setenv("WSLENV", "CUSTOM/w")
    environment = knowledge.knowledge_owner_env(root)
    assert environment["VAWS_KNOWLEDGE_PROJECT_ROOTS"] == r"D:\work\.agents\knowledge"
    assert environment["VAWS_KNOWLEDGE_CONFIG"] == r"D:\work\.vaws-local\knowledge\service.json"
    assert environment["VAWS_KNOWLEDGE_STATE"] == r"D:\work\.vaws-local\knowledge\instance"
    assert environment["VAWS_KNOWLEDGE_ORIGIN_REPO"] == "example/repo"
    assert environment[envs.PIN_ENV] == receipt
    assert "CUSTOM/w" in environment["WSLENV"]
    assert "VAWS_KNOWLEDGE_STATE/w" in environment["WSLENV"]
    assert envs.PIN_ENV + "/w" in environment["WSLENV"]
    assert knowledge.knowledge_owner_path(root, root) == r"D:\work"


def test_missing_owner_reports_pending_without_running_linux_backend(tmp_path, monkeypatch):
    missing = tmp_path / "win32/Scripts/python.exe"
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root, **kw: str(missing))
    monkeypatch.setattr(knowledge.subprocess, "run", lambda *args, **kwargs: pytest.fail("missing owner must not start a fallback"))
    result = knowledge.prepare_knowledge(tmp_path)
    assert result["status"] == "pending" and result["ready"] is False
    assert not (tmp_path / "win32").exists()


def test_unprepared_owner_receipt_reports_pending_without_starting_knowledge(tmp_path, monkeypatch):
    def unavailable(root, **kw):
        raise envs.EnvironmentError("Windows owner has no ready receipt")
    monkeypatch.setattr(knowledge, "knowledge_owner_python", unavailable)
    monkeypatch.setattr(knowledge.subprocess, "run", lambda *args, **kwargs: pytest.fail("unprepared owner must not start knowledge"))
    result = knowledge.prepare_knowledge(tmp_path)
    assert result["status"] == "pending" and result["ready"] is False
    assert result["reason"] == "Windows owner has no ready receipt"


@pytest.mark.parametrize("code,payload,ready", [
    (0, {"status": "ready", "ready": True}, True),
    (1, {"status": "pending", "ready": False, "reason": "offline"}, False),
    (1, {"status": "ready", "ready": True}, False),
])
def test_prepare_uses_installed_cli_and_preserves_readiness(tmp_path, monkeypatch, code, payload, ready):
    calls = []
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root, **kw: sys.executable)
    monkeypatch.setattr(knowledge, "knowledge_owner_env", lambda root: {"KNOWLEDGE_OWNER": "native"})
    monkeypatch.setattr(knowledge, "shared_workspace_root", lambda root: root)
    monkeypatch.setattr(knowledge.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, code, json.dumps(payload)))
    result = knowledge.prepare_knowledge(tmp_path)
    command, kwargs = calls[0]
    assert command == [sys.executable, "-c", knowledge.PREPARE_CODE]
    assert kwargs["env"]["KNOWLEDGE_OWNER"] == "native"
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] is sys.stderr
    assert result["ready"] is ready
    assert result["status"] == ("ready" if ready else "pending")


def test_invalid_prepare_reply_is_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root, **kw: sys.executable)
    monkeypatch.setattr(knowledge, "knowledge_owner_env", lambda root: {})
    monkeypatch.setattr(knowledge.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "not JSON"))
    assert knowledge.prepare_knowledge(tmp_path)["status"] == "pending"


def test_native_crash_retains_status_and_runtime_remedy(tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root, **kw: sys.executable)
    monkeypatch.setattr(knowledge, "knowledge_owner_env", lambda root: {})
    monkeypatch.setattr(knowledge.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, -1073741819, ""))
    code, result = knowledge.run_knowledge_cli(tmp_path, ["status"])
    assert code == -1073741819 and result["ready"] is False
    assert "0xC0000005" in result["reason"]
    assert "Visual C++ runtime" in result["reason"]


@pytest.mark.parametrize("options", [[], ["--locked"], ["--locked", "--group", "dev"], ["--group", "dev", "--locked"]])
@pytest.mark.parametrize("previous_pin", [None, "old-ready.json"])
def test_sync_only_installs_packages_and_does_not_touch_knowledge(tmp_path, monkeypatch, capsys, options, previous_pin):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    if previous_pin is None:
        monkeypatch.delenv(envs.PIN_ENV, raising=False)
    else:
        monkeypatch.setenv(envs.PIN_ENV, previous_pin)
    receipt = {"key": "a" * 64, "root": str(tmp_path / "ready"),
               "receipt": str(tmp_path / "ready/.vaws-ready.json")}
    calls = []
    monkeypatch.setattr(deps, "prepare_environment", lambda root, **kwargs: calls.append((root, kwargs)) or receipt)
    linked = []
    monkeypatch.setattr(links, "link_environment", lambda root, **kwargs: linked.append((root, kwargs)))
    monkeypatch.setattr(knowledge, "prepare_knowledge", lambda *a, **k: pytest.fail("package sync must not prepare knowledge"))
    assert deps.main(["sync", *options]) == 0
    assert calls == [(tmp_path, {"install_options": options, "timings": {}})]
    assert linked == ([(tmp_path, {"key": receipt["key"], "environment_root": Path(receipt["root"])})] if deps.os.name == "nt" else [])
    assert deps.os.environ.get(envs.PIN_ENV) == previous_pin
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["returncode"] == 0
    assert payload["environment"] == receipt["root"] and payload["receipt"] == receipt
    assert "knowledge" not in payload
    assert not (tmp_path / ".vaws-local/knowledge").exists()


def test_failed_dependency_install_does_not_prepare_knowledge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    monkeypatch.setenv(envs.PIN_ENV, "old-ready.json")
    def failed(root, **kwargs):
        raise envs.EnvironmentError("locked dependency installation failed with exit code 1")
    monkeypatch.setattr(deps, "prepare_environment", failed)
    monkeypatch.setattr(links, "link_environment", lambda *args, **kwargs: pytest.fail("failed install must not publish an alias"))
    monkeypatch.setattr(knowledge, "prepare_knowledge", lambda root: pytest.fail("failed install must not prepare knowledge"))
    assert deps.main(["sync"]) == 1
    assert deps.os.environ[envs.PIN_ENV] == "old-ready.json"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "knowledge" not in payload
    assert "exit code 1" in payload["error"]


def test_explicit_capability_sync_prewarms_only_the_selected_child(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    receipt = {"key": "b" * 64, "root": str(tmp_path / "runtime"), "receipt": str(tmp_path / "bundle.json")}
    monkeypatch.setattr(deps, "prepare_environment", lambda *args, **kw: receipt)
    monkeypatch.setattr(links, "link_environment", lambda *args, **kw: None)
    calls = []
    monkeypatch.setattr(deps, "capability_receipt", lambda *args, **kw: calls.append((args, kw)))
    assert deps.main(["sync", "--capability", "knowledge"]) == 0
    assert calls == [((receipt, "knowledge"), {"prepare_missing": True, "timings": {}})]
    assert json.loads(capsys.readouterr().out)["receipt"] == receipt


def test_dependency_entry_does_not_import_knowledge_service(monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name == "vaws_knowledge_service" or name == "vaws_knowledge" or name.startswith("vaws_knowledge."):
            pytest.fail("dependency entry imported the optional knowledge runtime")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    fresh = importlib.util.spec_from_file_location("packages_without_knowledge", ROOT / ".agents/scripts/vaws_deps.py")
    fresh.loader.exec_module(importlib.util.module_from_spec(fresh))


def test_removed_packages_only_flag_is_rejected_before_installation(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\npackage = false\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    monkeypatch.setattr(envs, "_python_identity", lambda *a, **k: pytest.fail("removed option reached installation"))
    assert deps.main(["sync", "--packages-only"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "unsupported immutable sync option" in payload["error"]
    assert not (tmp_path / ".vaws-local").exists()
