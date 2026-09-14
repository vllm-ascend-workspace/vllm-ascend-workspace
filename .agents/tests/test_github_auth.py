"""Actual authentication adapter boundaries, with no network or live credentials."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
import subprocess
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import vaws_github as github


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(github, "ROOT", tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def response(body=b'{"login":"alice","id":42,"type":"User"}', status=200):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = body
    result.status = status
    return result


@pytest.mark.parametrize("name", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_token_auth_uses_https_without_gh_and_returns_only_public_identity(monkeypatch, name):
    secret = "not-a-pattern-shaped-credential"
    monkeypatch.setenv(name, secret)
    with mock.patch.object(github.urlrequest, "build_opener") as build, \
            mock.patch.object(github.subprocess, "run", side_effect=AssertionError("gh is unavailable")):
        build.return_value.open.return_value = response()
        result = github.detect_github_auth()
    assert result == {"provider": name, "authenticated": True, "login": "alice", "github_user_id": 42, "type": "User"}
    request = build.return_value.open.call_args.args[0]
    assert request.full_url == "https://api.github.com/user"
    assert request.get_header("Authorization") == "Bearer " + secret
    assert build.return_value.open.call_args.kwargs["timeout"] == 60
    assert secret not in json.dumps(result)
    assert os.environ[name] == secret


def test_gh_token_has_same_precedence_as_github_cli(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "primary-fixture")
    monkeypatch.setenv("GITHUB_TOKEN", "secondary-fixture")
    with mock.patch.object(github.urlrequest, "build_opener") as build:
        build.return_value.open.return_value = response()
        assert github.detect_github_auth()["provider"] == "GH_TOKEN"
    assert build.return_value.open.call_args.args[0].get_header("Authorization") == "Bearer primary-fixture"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_token_http_errors_keep_status_without_body_or_credential(monkeypatch, status):
    secret = "opaque-private-test-credential"
    monkeypatch.setenv("GH_TOKEN", secret)
    error = github.urlerror.HTTPError("https://api.github.com/user", status, "bad " + secret,
                                    {"Authorization": secret}, io.BytesIO(secret.encode()))
    with mock.patch.object(github.urlrequest, "build_opener") as build:
        build.return_value.open.side_effect = error
        with pytest.raises(github.GitHubAPIError) as caught:
            github.GitHubClient().api("user")
    assert caught.value.status == status
    assert secret not in str(caught.value) + json.dumps(caught.value.evidence)
    assert caught.value.evidence == {"provider": "GH_TOKEN", "endpoint": "user", "method": "GET", "status": status}
    assert error.fp.closed


def test_transport_error_is_redacted_and_never_silently_changes_account(monkeypatch):
    secret = "opaque-private-test-credential"
    monkeypatch.setenv("GH_TOKEN", secret)
    with mock.patch.object(github.urlrequest, "build_opener") as build, \
            mock.patch.object(github.subprocess, "run", side_effect=AssertionError("must not fall back to another account")):
        build.return_value.open.side_effect = github.urlerror.URLError("proxy refused " + secret)
        result = github.detect_github_auth()
    assert result["authenticated"] is False
    assert result["provider"] == "GH_TOKEN"
    assert secret not in json.dumps(result)


@pytest.mark.parametrize("body", [b"not json", b"[]", b"", b"x" * (1024 * 1024 + 1)],
                         ids=["invalid-json", "non-object", "empty", "oversized"])
def test_bad_token_response_does_not_enter_exception_evidence(monkeypatch, body):
    monkeypatch.setenv("GITHUB_TOKEN", "fixture")
    with mock.patch.object(github.urlrequest, "build_opener") as build:
        build.return_value.open.return_value = response(body)
        with pytest.raises(github.GitHubAPIError) as caught:
            github.GitHubClient().api("user")
    assert "stdout" not in caught.value.evidence
    assert "stderr" not in caught.value.evidence


@pytest.mark.parametrize("secret", [" ", "line\nvalue", "tab\tvalue"])
def test_malformed_token_never_reaches_network(monkeypatch, secret):
    monkeypatch.setenv("GH_TOKEN", secret)
    with mock.patch.object(github.urlrequest, "build_opener") as build:
        result = github.detect_github_auth()
    assert result["authenticated"] is False
    build.assert_not_called()


@pytest.mark.parametrize("url", ["http://api.github.com/user", "https://other.example/user", "https://api.github.com@other.example/user"])
def test_redirect_never_sends_authorization_to_another_origin(url):
    request = github.urlrequest.Request("https://api.github.com/user", headers={"Authorization": "Bearer fixture"})
    with pytest.raises(github.GitHubAPIError, match="outside"):
        github._GitHubRedirect().redirect_request(request, None, 302, "Found", {}, url)


def test_write_redirect_does_not_replay_or_convert_to_get():
    request = github.urlrequest.Request("https://api.github.com/repos/a/b/forks", method="POST", data=b"{}")
    with pytest.raises(github.GitHubAPIError, match="inspect its result"):
        github._GitHubRedirect().redirect_request(request, None, 302, "Found", {}, "https://api.github.com/repos/a/c/forks")


@pytest.mark.parametrize("endpoint", ["https://other.example/user", "//other.example/user", "user?access_token=fixture", "user\nprivate"])
def test_api_rejects_external_or_credential_bearing_endpoints_before_auth(endpoint):
    with mock.patch.object(github.subprocess, "run") as run:
        with pytest.raises(github.GitHubAPIError):
            github.GitHubClient().api(endpoint)
    run.assert_not_called()


def test_cli_login_detection_reuses_secure_login_without_exporting_token():
    with mock.patch.object(github.subprocess, "run", return_value=mock.Mock(
            returncode=0, stdout='{"login":"alice","id":42,"type":"User"}', stderr="")) as run:
        result = github.detect_github_auth()
    assert result["authenticated"] is True and result["provider"] == "gh"
    assert run.call_args.args[0] == ["gh", "api", "--hostname", "github.com", "user", "--method", "GET"]


def test_missing_cli_is_a_candidate_detection_failure_not_an_exception():
    with mock.patch.object(github.subprocess, "run", side_effect=FileNotFoundError("gh unavailable")):
        result = github.detect_github_auth()
    assert result["authenticated"] is False and result["provider"] == "gh"


def test_star_is_idempotent_and_auth_failure_is_not_absence():
    client = mock.Mock()
    assert github.ensure_star("owner/project", client=client)["status"] == "already_starred"
    client.api.assert_called_once_with("user/starred/owner/project")
    client.reset_mock()
    client.api.side_effect = [github.GitHubAPIError("Not found", 404), {}]
    assert github.ensure_star("owner/project", client=client)["status"] == "starred"
    assert client.api.call_args_list == [mock.call("user/starred/owner/project"), mock.call("user/starred/owner/project", method="PUT")]
    client.reset_mock()
    client.api.side_effect = github.GitHubAPIError("Forbidden", 403)
    with pytest.raises(github.GitHubAPIError):
        github.ensure_star("owner/project", client=client)
    assert client.api.call_count == 1


def test_real_token_adapter_handles_star_204_and_write_method(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fixture")
    with mock.patch.object(github.urlrequest, "build_opener") as build:
        build.return_value.open.return_value = response(b"", 204)
        assert github.GitHubClient().api("user/starred/owner/project", method="PUT") == {}
    assert build.return_value.open.call_args.args[0].get_method() == "PUT"


def test_cli_adapter_accepts_star_empty_response():
    with mock.patch.object(github.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")):
        assert github.GitHubClient().ensure_star("owner/project")["status"] == "already_starred"


def test_real_git_credential_pipe_supports_token_without_gh_and_does_not_persist(tmp_path, monkeypatch):
    secret = "opaque-git-credential-fixture"
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "0")
    environment = github.github_git_environment()
    result = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                            capture_output=True, text=True, encoding="utf-8", env=environment, cwd=tmp_path, timeout=20)
    assert result.returncode == 0
    assert "password=" + secret in result.stdout
    assert "username=x-access-token" in result.stdout
    assert secret not in result.stderr
    assert not list(tmp_path.iterdir())
    assert all(secret not in value for name, value in environment.items() if name.startswith("GIT_CONFIG_"))


@pytest.mark.parametrize("protocol,host", [("http", "github.com"), ("https", "example.invalid"), ("https", "github.com:443")])
def test_credential_helper_never_discloses_token_to_other_hosts(protocol, host, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "private-helper-fixture")
    script = Path(github.__file__).with_name("vaws_git_credential.py")
    result = subprocess.run([sys.executable, str(script), "get"], input=f"protocol={protocol}\nhost={host}\n\n",
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""


@pytest.mark.parametrize("action", ["store", "erase"])
def test_credential_helper_does_not_store_secrets(action, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "private-helper-fixture")
    script = Path(github.__file__).with_name("vaws_git_credential.py")
    result = subprocess.run([sys.executable, str(script), action], input="protocol=https\nhost=github.com\n\n",
                            capture_output=True, text=True, cwd=tmp_path, timeout=20)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert not list(tmp_path.iterdir())


def test_token_git_helper_is_scoped_idempotent_and_credential_free(tmp_path, monkeypatch):
    secret = "private-repo-helper-fixture"
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    assert github.configure_token_git(tmp_path) == "command_scoped"
    assert github.configure_token_git(tmp_path) == "command_scoped"
    config = (tmp_path / ".git/config").read_text(encoding="utf-8")
    assert secret not in config and "vaws_git_credential.py" not in config
    github.replace_values(tmp_path, "credential.https://github.com.helper", ["", github._credential_command()])
    assert github.configure_token_git(tmp_path) == "legacy_override_removed"
    assert github.config_values(tmp_path, "credential.https://github.com.helper", local=True) == []
    github.replace_values(tmp_path, "credential.https://github.com.helper", ["custom-existing-helper"])
    assert github.configure_token_git(tmp_path) == "existing_helper_preserved"
    assert github.config_values(tmp_path, "credential.https://github.com.helper", local=True) == ["custom-existing-helper"]


def test_git_environment_preserves_existing_process_configuration_and_no_token_is_noop():
    base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.longpaths", "GIT_CONFIG_VALUE_0": "true"}
    assert github.github_git_environment(base) == {**base, "GIT_TERMINAL_PROMPT": "0"}
    result = github.github_git_environment({**base, "GITHUB_TOKEN": "fixture"})
    assert result["GIT_CONFIG_COUNT"] == "3"
    assert result["GIT_CONFIG_KEY_0"] == "core.longpaths" and result["GIT_CONFIG_VALUE_0"] == "true"
    assert result["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper" and result["GIT_CONFIG_VALUE_1"] == ""


def test_actual_git_cannot_dump_token_into_inherited_trace_sink(tmp_path, monkeypatch):
    secret = "private-trace-regression-fixture"
    trace = tmp_path / "trace.json"
    monkeypatch.setenv("GH_TOKEN", secret)
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    monkeypatch.setenv("GIT_TRACE2_ENV_VARS", "GH_TOKEN")
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    result = github.git(tmp_path, "version")
    assert result.returncode == 0
    assert not trace.exists()
    assert secret not in result.stdout + result.stderr
