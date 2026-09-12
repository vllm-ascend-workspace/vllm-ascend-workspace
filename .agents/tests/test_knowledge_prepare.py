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


def test_mounted_workspace_reserves_windows_owner_before_it_is_installed(monkeypatch):
    monkeypatch.setattr(owner, "os", SimpleNamespace(name="posix", environ={"WSL_DISTRO_NAME": "test"}))
    root = PurePosixPath("/mnt/d/work")
    assert owner.windows_mounted_workspace(root)
    monkeypatch.setattr(envs, "native_ready", lambda root: pytest.fail("mounted workspaces must not select a Linux owner"))
    def unavailable(root):
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
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: str(missing))
    monkeypatch.setattr(knowledge.subprocess, "run", lambda *args, **kwargs: pytest.fail("missing owner must not start a fallback"))
    result = knowledge.prepare_knowledge(tmp_path)
    assert result["status"] == "pending" and result["ready"] is False
    assert not (tmp_path / "win32").exists()


def test_unprepared_owner_receipt_reports_pending_without_starting_knowledge(tmp_path, monkeypatch):
    def unavailable(root):
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
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: sys.executable)
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
    monkeypatch.setattr(knowledge, "knowledge_owner_python", lambda root: sys.executable)
    monkeypatch.setattr(knowledge, "knowledge_owner_env", lambda root: {})
    monkeypatch.setattr(knowledge.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "not JSON"))
    assert knowledge.prepare_knowledge(tmp_path)["status"] == "pending"


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("previous_pin", [None, "old-ready.json"])
def test_sync_reports_dependency_success_separately_from_knowledge(tmp_path, monkeypatch, capsys, ready, previous_pin):
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
    prepared = []
    monkeypatch.setattr(deps, "prepare_knowledge", lambda root: prepared.append((root, deps.os.environ.get(envs.PIN_ENV))) or {"status": "ready" if ready else "pending", "ready": ready})
    assert deps.main(["sync", "--locked"]) == 0
    assert calls == [(tmp_path, {"install_options": ["--locked"]})]
    assert linked == ([(tmp_path, {"key": receipt["key"], "environment_root": Path(receipt["root"])})] if deps.os.name == "nt" else [])
    assert prepared == [(tmp_path, receipt["receipt"])]
    assert deps.os.environ.get(envs.PIN_ENV) == previous_pin
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["returncode"] == 0
    assert payload["environment"] == receipt["root"] and payload["receipt"] == receipt
    assert payload["knowledge"]["ready"] is ready


def test_failed_dependency_install_does_not_prepare_knowledge(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    monkeypatch.setenv(envs.PIN_ENV, "old-ready.json")
    def failed(root, **kwargs):
        raise envs.EnvironmentError("locked dependency installation failed with exit code 1")
    monkeypatch.setattr(deps, "prepare_environment", failed)
    monkeypatch.setattr(links, "link_environment", lambda *args, **kwargs: pytest.fail("failed install must not publish an alias"))
    monkeypatch.setattr(deps, "prepare_knowledge", lambda root: pytest.fail("failed install must not prepare knowledge"))
    assert deps.main(["sync"]) == 1
    assert deps.os.environ[envs.PIN_ENV] == "old-ready.json"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "knowledge" not in payload
    assert "exit code 1" in payload["error"]


@pytest.mark.parametrize("arguments", [
    ["--packages-only", "--locked", "--group", "dev"],
    ["--locked", "--group", "dev", "--packages-only"],
])
def test_packages_only_preserves_uv_options_without_touching_knowledge(tmp_path, monkeypatch, capsys, arguments):
    monkeypatch.setattr(deps, "ROOT", tmp_path)
    monkeypatch.setenv(envs.PIN_ENV, "running-client-receipt.json")
    receipt = {"key": "a" * 64, "root": str(tmp_path / "ready"),
               "receipt": str(tmp_path / "ready/.vaws-ready.json")}
    installed = []
    monkeypatch.setattr(deps, "prepare_environment", lambda root, **kwargs: installed.append((root, kwargs)) or receipt)
    monkeypatch.setattr(links, "link_environment", lambda *args, **kwargs: None)
    monkeypatch.setattr(deps, "prepare_knowledge", lambda root: pytest.fail("packages-only must not touch knowledge"))
    assert deps.main(["sync", *arguments]) == 0
    assert installed == [(tmp_path, {"install_options": ["--locked", "--group", "dev"]})]
    assert deps.os.environ[envs.PIN_ENV] == "running-client-receipt.json"
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["receipt"] == receipt
    assert payload["knowledge"] == {"status": "skipped", "reason": "packages_only"}
