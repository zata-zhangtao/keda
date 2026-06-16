"""Unit tests for the pure helpers in ``scripts/provision_preview_server.py``.

The script is not importable as a regular module (lives outside any package),
so we load it once via :mod:`importlib` and exercise the helpers directly.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "provision_preview_server.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "provision_preview_server", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pps = _load_module()


def _captured_cmd_print(cmd: list[str]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        pps._print_cmd(cmd)
    return buf.getvalue().strip()


def test_print_cmd_redacts_sshpass_password_only():
    cmd = [
        "sshpass",
        "-p",
        "Zata123@",
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        "22",
        "root@1.2.3.4",
        "bash",
        "-c",
        "echo hi",
    ]
    line = _captured_cmd_print(cmd)
    assert "Zata123@" not in line, "sshpass password must be redacted"
    assert "-p '***'" in line or "-p ***" in line
    # SSH port 22 must remain visible (it's `-p 22`, not a secret).
    assert " 22 " in line + " " or line.endswith(" 22") or "'22'" in line


def test_print_cmd_keeps_ssh_port_when_no_sshpass():
    cmd = ["ssh", "-p", "2222", "user@host", "bash", "-c", "uptime"]
    line = _captured_cmd_print(cmd)
    assert "'2222'" in line or " 2222 " in line + " "
    assert "***" not in line


def test_parse_traefik_state_running():
    has, running = pps._parse_traefik_state("preview-traefik running\n")
    assert (has, running) == (True, True)


def test_parse_traefik_state_exited():
    has, running = pps._parse_traefik_state("preview-traefik exited\n")
    assert (has, running) == (True, False)


def test_parse_traefik_state_missing():
    assert pps._parse_traefik_state("") == (False, False)
    assert pps._parse_traefik_state("other-container running\n") == (False, False)


def test_parse_traefik_state_substring_filter_does_not_match_prefix():
    """Docker's ``--filter name=`` is a substring match, so unrelated containers
    like ``preview-traefik-helper`` may appear; the parser must use the exact name."""
    stdout = "preview-traefik-helper running\npreview-traefik exited\n"
    assert pps._parse_traefik_state(stdout) == (True, False)


def test_render_traefik_config_http_only_omits_acme():
    body = pps.render_traefik_config("http-only", email=None)
    assert "letsencrypt" not in body
    assert ":443" not in body


def test_render_traefik_config_acme_requires_email():
    with pytest.raises(ValueError):
        pps.render_traefik_config("acme-http01", email=None)


def test_render_traefik_config_acme_includes_email():
    body = pps.render_traefik_config("acme-http01", email="me@example.com")
    assert "me@example.com" in body
    assert "httpChallenge" in body


def test_merge_preview_section_replaces_existing():
    original = (
        "[other]\nkey = 1\n\n"
        '[preview]\nenabled = false\nbase_domain = "old.example.com"\n\n'
        "[trailing]\nx = 1\n"
    )
    new_section = '[preview]\nenabled = true\nbase_domain = "new.example.com"\n'
    merged = pps._merge_preview_section(original, new_section)
    assert merged.count("[preview]") == 1
    assert "new.example.com" in merged
    assert "old.example.com" not in merged
    assert "[trailing]" in merged


def test_merge_preview_section_appends_when_missing():
    original = "[other]\nkey = 1\n"
    new_section = "[preview]\nenabled = true\n"
    merged = pps._merge_preview_section(original, new_section)
    assert merged.endswith("[preview]\nenabled = true\n")
    assert "[other]" in merged


def test_run_uses_non_login_shell_and_default_timeout(monkeypatch):
    """``Remote.run`` must use ``bash -c`` and default to a 180s captured timeout."""

    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = None
        connect_timeout = 15

    remote = pps.Remote(_Args())

    captured: dict[str, object] = {}

    class _CP:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return _CP()

    monkeypatch.setattr(pps.subprocess, "run", fake_run)
    remote.run("echo hi")

    cmd = captured["cmd"]
    assert "bash" in cmd
    assert "-c" in cmd
    assert "-lc" not in cmd, "login shell rcfiles add seconds to every SSH call"
    assert captured["timeout"] == 180


def test_run_quotes_script_for_remote_shell(monkeypatch):
    """Without quoting, ``bash -c cat /etc/os-release`` is re-parsed by the
    remote shell as ``bash -c cat`` with ``/etc/os-release`` as ``$0``;
    cat then reads stdin and yields no output. The script argument must be
    quoted so the remote shell keeps it intact."""

    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = None
        connect_timeout = 15

    remote = pps.Remote(_Args())

    captured: dict[str, object] = {}

    class _CP:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _CP()

    monkeypatch.setattr(pps.subprocess, "run", fake_run)
    remote.run("cat /etc/os-release")
    cmd = captured["cmd"]
    # The whole script must appear as a single argument; the previous bug
    # passed two separate args ("cat", "/etc/os-release") that the remote
    # sh then misinterpreted.
    assert cmd[-1] == "'cat /etc/os-release'"
    assert "cat" not in cmd[:-1] and "/etc/os-release" not in cmd[:-1]


def test_run_respects_explicit_timeout(monkeypatch):
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = None
        connect_timeout = 15

    remote = pps.Remote(_Args())

    captured: dict[str, object] = {}

    class _CP:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _CP()

    monkeypatch.setattr(pps.subprocess, "run", fake_run)
    remote.run("sleep 1", timeout=42)
    assert captured["timeout"] == 42


def test_base_ssh_includes_keepalive_and_control_master():
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = "pw"
        connect_timeout = 15

    remote = pps.Remote(_Args())
    base = remote._base_ssh()
    joined = " ".join(base)
    assert "ServerAliveInterval=15" in joined
    assert "ServerAliveCountMax=4" in joined
    # The control socket lets later calls reuse one auth handshake; sshpass
    # belongs to connect(), not the per-command base argv.
    assert "ControlMaster=auto" in joined
    assert "ControlPath=" in joined
    assert "sshpass" not in joined, "sshpass must only be used in connect()"


def test_base_scp_reuses_control_path():
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = "pw"
        connect_timeout = 15

    remote = pps.Remote(_Args())
    base = remote._base_scp()
    joined = " ".join(base)
    assert "ControlPath=" in joined
    assert "sshpass" not in joined


def test_connect_invokes_sshpass_with_password(monkeypatch):
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = "secret"
        connect_timeout = 15

    remote = pps.Remote(_Args())

    calls: list[list[str]] = []

    class _CP:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _CP()

    monkeypatch.setattr(pps.subprocess, "run", fake_run)
    remote.connect()
    # First call sets up the master via sshpass + a short `true` command.
    setup = calls[0]
    assert setup[0] == "sshpass"
    assert setup[-1] == "true", "use a short command + ControlPersist, not -N -f"
    assert (
        "-N" not in setup and "-f" not in setup
    ), "sshpass + ssh -N -f silently fails: sshpass uses a pty that -f detaches"
    assert "ControlMaster=auto" in " ".join(setup)
    # Second call must verify the master actually came up.
    verify = calls[1]
    assert verify[0] == "ssh"
    assert "-O" in verify and "check" in verify


def test_connect_omits_sshpass_when_key_auth(monkeypatch):
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = "/tmp/fake_key"
        password = None
        connect_timeout = 15

    remote = pps.Remote(_Args())

    class _CP:
        stdout = ""
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        return _CP()

    monkeypatch.setattr(pps.subprocess, "run", fake_run)
    remote.connect()
    # Should not raise; using key auth means no sshpass anywhere.


def test_connect_fails_loudly_when_master_check_fails(monkeypatch):
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = "secret"
        connect_timeout = 15

    remote = pps.Remote(_Args())

    class _OkCP:
        stdout = ""
        stderr = ""
        returncode = 0

    class _BadCP:
        stdout = ""
        stderr = "no master found"
        returncode = 255

    responses = [_OkCP(), _BadCP()]

    def fake_run(cmd, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(pps.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        remote.connect()
    assert remote._master_started is False


def test_control_opts_use_long_persist():
    """If ControlPersist is too short (e.g. 60s), docker install or apt-get
    can let the master expire mid-run and the next call re-auths."""

    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = None
        connect_timeout = 15

    remote = pps.Remote(_Args())
    joined = " ".join(remote._control_opts())
    assert "ControlPersist=20m" in joined


def test_close_is_idempotent_and_noop_without_master():
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = None
        connect_timeout = 15

    remote = pps.Remote(_Args())
    # Should not raise even though we never started a master.
    remote.close()
    remote.close()


class _FakeRemote:
    """Records every run() call and returns scripted stdout."""

    def __init__(self, captured_outputs: list[str]):
        self._outputs = list(captured_outputs)
        self.calls: list[tuple[str, bool]] = []

    def run(self, script, *, check=True, live=False, timeout=None):
        self.calls.append((script, live))

        class _CP:
            stdout = self._outputs.pop(0) if self._outputs else ""
            stderr = ""
            returncode = 0

        return _CP()


def test_ensure_external_traefik_network_warns_when_no_traefik(capsys):
    """--skip-traefik mode but no traefik container — warn loudly."""
    fake = _FakeRemote(["", ""])
    pps.ensure_external_traefik_network(fake)
    out = capsys.readouterr().out
    assert "No running Traefik-like container" in out
    # First call must create the network (idempotent).
    assert "docker network create traefik" in fake.calls[0][0]


def test_ensure_external_traefik_network_detects_attached_traefik(capsys):
    """If the user's traefik already joins the `traefik` network, all good."""
    fake = _FakeRemote(["", "traefik|traefik:v3.1|traefik,bridge\n"])
    pps.ensure_external_traefik_network(fake)
    out = capsys.readouterr().out
    assert "✓ existing Traefik `traefik` is attached" in out


def test_ensure_external_traefik_network_warns_unattached_traefik(capsys):
    """User has traefik but it isn't on the `traefik` network — guide them."""
    fake = _FakeRemote(["", "traefik|traefik:v3.1|bridge\n"])
    pps.ensure_external_traefik_network(fake)
    out = capsys.readouterr().out
    assert "none is attached to the `traefik` network" in out
    assert "docker network connect traefik" in out


def test_ensure_external_traefik_network_ignores_consumers_of_traefik_net(capsys):
    """A non-Traefik app container that sits on the `traefik` network must
    not be misreported as a Traefik instance (image must contain 'traefik')."""
    fake = _FakeRemote(
        [
            "",
            "kimi-ppt-frontend-1|nginx:alpine|traefik,bridge\n"
            "traefik|traefik:v3.1|traefik\n",
        ]
    )
    pps.ensure_external_traefik_network(fake)
    out = capsys.readouterr().out
    assert "kimi-ppt-frontend-1" not in out
    assert "traefik`" in out and "attached" in out


def test_skip_traefik_flag_is_parseable(monkeypatch):
    """The new --skip-traefik flag must be present in parse_args."""
    monkeypatch.setattr(
        pps.sys,
        "argv",
        [
            "prog",
            "--host",
            "h",
            "--user",
            "u",
            "--password",
            "p",
            "--domain",
            "d.example.com",
            "--skip-traefik",
        ],
    )
    args = pps.parse_args()
    assert args.skip_traefik is True


def test_check_port_conflict_returns_true_when_free(capsys):
    """No conflicting listeners — return True so main() proceeds with install."""
    fake = _FakeRemote([""])
    assert pps.check_port_conflict(fake) is True
    assert capsys.readouterr().out == ""


def test_check_port_conflict_aborts_with_skip_traefik_hint(capsys):
    """When 80/443 are taken, return False and point users at --skip-traefik
    instead of letting `docker run` crash later with a generic port error."""
    sample = (
        'LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("docker-proxy",pid=228414,fd=8))\n'
        'LISTEN 0 4096 0.0.0.0:443 0.0.0.0:* users:(("docker-proxy",pid=228434,fd=8))\n'
    )
    fake = _FakeRemote([sample])
    assert pps.check_port_conflict(fake) is False
    out = capsys.readouterr().out
    assert "--skip-traefik" in out
    assert "0.0.0.0:80" in out


def test_generate_deploy_key_runs_ssh_keygen_and_scps(monkeypatch, tmp_path):
    """generate_deploy_key must call ssh-keygen on the server and scp the
    private key down to a local temp file (mode 0600)."""
    captured: dict[str, object] = {}

    class _FakeR:
        def __init__(self, _args):
            pass

        def run(self, script, **kwargs):
            captured.setdefault("runs", []).append(script)

            class _CP:
                stdout = "MISSING" if "EXISTS" in script else ""
                stderr = ""
                returncode = 0

            return _CP()

        def scp_down(self, remote_path, local_path):
            captured["scp_remote"] = remote_path
            captured["scp_local"] = local_path
            Path(local_path).write_text("FAKE_PRIVATE_KEY\n", encoding="utf-8")

    monkeypatch.setattr(pps.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    result = pps.generate_deploy_key(_FakeR(None))
    assert captured["scp_remote"] == pps._DEPLOY_KEY_REMOTE_PATH
    assert result.exists()
    assert oct(result.stat().st_mode & 0o777) == "0o600"
    # ssh-keygen must be invoked on the server
    assert any("ssh-keygen -t ed25519" in r for r in captured["runs"])
    # Public key must be appended to authorized_keys
    assert any("authorized_keys" in r for r in captured["runs"])


def test_generate_deploy_key_refuses_to_overwrite(monkeypatch):
    """A pre-existing key on the server must NOT be silently overwritten —
    re-running would break the workflow that's already using it."""

    class _FakeR:
        def __init__(self, _args):
            pass

        def run(self, script, **kwargs):
            class _CP:
                stdout = "EXISTS\n" if "EXISTS" in script else ""
                stderr = ""
                returncode = 0

            return _CP()

        def scp_down(self, *_):
            raise AssertionError("scp_down must not be called when key exists")

    with pytest.raises(SystemExit) as excinfo:
        pps.generate_deploy_key(_FakeR(None))
    assert "already exists" in str(excinfo.value)


def test_scp_down_mirrors_scp_argv():
    class _Args:
        host = "1.2.3.4"
        user = "root"
        port = 22
        key = None
        password = "pw"
        connect_timeout = 15

    remote = pps.Remote(_Args())
    local = Path("/tmp/dest/key")
    cmd = remote._base_scp() + ["root@1.2.3.4:/etc/foo", str(local)]
    assert cmd[0] == "scp"
    assert cmd[-2] == "root@1.2.3.4:/etc/foo"
    assert cmd[-1] == "/tmp/dest/key"
    # No sshpass here — scp_down reuses the master socket, no need to re-auth.
    assert "sshpass" not in cmd


def test_generate_deploy_key_flag_is_parseable(monkeypatch):
    monkeypatch.setattr(
        pps.sys,
        "argv",
        [
            "prog",
            "--host",
            "h",
            "--user",
            "u",
            "--password",
            "p",
            "--domain",
            "d.example.com",
            "--generate-deploy-key",
        ],
    )
    args = pps.parse_args()
    assert args.generate_deploy_key is True


def test_print_secrets_instructions_uses_deploy_key_when_set(monkeypatch, capsys):
    """When generate_deploy_key was used, the secret-set command should
    point at the scp'd local file, and warn the user to delete it after."""
    monkeypatch.setenv(
        "PREVIEW_DEPLOY_KEY_PATH", "/tmp/preview-deploy-key-XYZ/id_ed25519"
    )
    args = type(
        "A",
        (),
        {
            "host": "h",
            "user": "u",
            "deploy_user": "d",
            "key": None,
            "create_deploy_user": False,
        },
    )()
    pps.print_secrets_instructions(args)
    out = capsys.readouterr().out
    assert "/tmp/preview-deploy-key-XYZ/id_ed25519" in out
    assert "Delete the local private key" in out


def test_print_secrets_instructions_uses_deploy_user_when_create_flag_set(
    monkeypatch, capsys
):
    """With --create-deploy-user, SERVER_USER should be args.deploy_user
    (the script reconnects as that user), not args.user (the SSH bootstrap)."""
    monkeypatch.delenv("PREVIEW_DEPLOY_KEY_PATH", raising=False)
    args = type(
        "A",
        (),
        {
            "host": "h",
            "user": "root",
            "deploy_user": "deploy",
            "key": "/home/me/.ssh/id",
            "create_deploy_user": True,
        },
    )()
    pps.print_secrets_instructions(args)
    out = capsys.readouterr().out
    assert 'SERVER_USER        --env preview --body "deploy"' in out


def test_print_secrets_instructions_falls_back_to_args_key(monkeypatch, capsys):
    """Without --generate-deploy-key but with --key, fall back to the
    user-supplied local private key path (existing behaviour)."""
    monkeypatch.delenv("PREVIEW_DEPLOY_KEY_PATH", raising=False)
    args = type(
        "A",
        (),
        {
            "host": "h",
            "user": "u",
            "deploy_user": "d",
            "key": "/home/me/.ssh/id",
            "create_deploy_user": False,
        },
    )()
    pps.print_secrets_instructions(args)
    out = capsys.readouterr().out
    assert "/home/me/.ssh/id" in out
    assert "Delete the local private key" not in out


def _make_args(key=None, deploy_user="d", create_deploy_user=False):
    return type(
        "A",
        (),
        {
            "host": "h",
            "user": "root",
            "deploy_user": deploy_user,
            "key": key,
            "create_deploy_user": create_deploy_user,
            "app_dir": "/opt/preview",
        },
    )()


def test_set_github_secrets_uses_deploy_key_when_set(monkeypatch, tmp_path, capsys):
    """When --generate-deploy-key ran, apply_secrets must pipe the
    scp'd deploy key into `gh secret set`, not the operator's --key.
    Regression: previously it ignored PREVIEW_DEPLOY_KEY_PATH and used
    args.key, leaving the deploy user with no matching authorized_keys."""
    deploy_key = tmp_path / "deploy_key"
    deploy_key.write_text("DEPLOY-PRIVATE-KEY-CONTENT", encoding="utf-8")
    operator_key = tmp_path / "operator_key"
    operator_key.write_text("OPERATOR-PRIVATE-KEY-CONTENT", encoding="utf-8")

    monkeypatch.setenv("PREVIEW_DEPLOY_KEY_PATH", str(deploy_key))
    monkeypatch.setattr(pps, "_gh_auth_ok", lambda: True)
    monkeypatch.setattr(pps, "_gh_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(pps, "_gh_secret_exists", lambda name: name != "SERVER_SSH_KEY")
    monkeypatch.setattr(pps, "_confirm", lambda *a, **kw: True)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "x")
    monkeypatch.setattr(pps.getpass, "getpass", lambda *a, **kw: "x")
    captured: list[tuple[list[str], str]] = []

    def fake_run(argv, *args, **kwargs):  # noqa: A002
        if isinstance(argv, list) and len(argv) >= 2 and argv[0:2] == ["gh", "secret"]:
            captured.append((list(argv), kwargs.get("input", "")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pps.subprocess, "run", fake_run)

    pps.apply_secrets(_make_args(key=str(operator_key)))

    secret_calls = [
        (argv, payload) for argv, payload in captured if "SERVER_SSH_KEY" in argv
    ]
    assert len(secret_calls) == 1
    argv, payload = secret_calls[0]
    assert payload == "DEPLOY-PRIVATE-KEY-CONTENT"
    assert "OPERATOR-PRIVATE-KEY-CONTENT" not in payload


def test_set_github_secrets_falls_back_to_args_key(monkeypatch, tmp_path):
    """Without --generate-deploy-key but with --key, fall back to the
    operator's local private key path (existing behaviour)."""
    monkeypatch.delenv("PREVIEW_DEPLOY_KEY_PATH", raising=False)
    operator_key = tmp_path / "operator_key"
    operator_key.write_text("OPERATOR-PRIVATE-KEY-CONTENT", encoding="utf-8")
    monkeypatch.setattr(pps, "_gh_auth_ok", lambda: True)
    monkeypatch.setattr(pps, "_gh_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(pps, "_gh_secret_exists", lambda name: name != "SERVER_SSH_KEY")
    monkeypatch.setattr(pps, "_confirm", lambda *a, **kw: True)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "x")
    monkeypatch.setattr(pps.getpass, "getpass", lambda *a, **kw: "x")
    captured: list[tuple[list[str], str]] = []

    def fake_run(argv, *args, **kwargs):  # noqa: A002
        if isinstance(argv, list) and len(argv) >= 2 and argv[0:2] == ["gh", "secret"]:
            captured.append((list(argv), kwargs.get("input", "")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pps.subprocess, "run", fake_run)

    pps.apply_secrets(_make_args(key=str(operator_key)))

    secret_calls = [
        (argv, payload) for argv, payload in captured if "SERVER_SSH_KEY" in argv
    ]
    assert len(secret_calls) == 1
    _, payload = secret_calls[0]
    assert payload == "OPERATOR-PRIVATE-KEY-CONTENT"


def test_set_github_secrets_skips_ssh_key_when_neither_set(monkeypatch, tmp_path):
    """With neither --key nor --generate-deploy-key, no SERVER_SSH_KEY
    spec is appended (the script prompts the user to fix the gap)."""
    monkeypatch.delenv("PREVIEW_DEPLOY_KEY_PATH", raising=False)
    monkeypatch.setattr(pps, "_gh_auth_ok", lambda: True)
    monkeypatch.setattr(pps, "_gh_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(pps, "_gh_secret_exists", lambda name: name != "SERVER_SSH_KEY")
    monkeypatch.setattr(pps, "_confirm", lambda *a, **kw: True)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "x")
    monkeypatch.setattr(pps.getpass, "getpass", lambda *a, **kw: "x")
    captured: list[tuple[list[str], str]] = []

    def fake_run(argv, *args, **kwargs):  # noqa: A002
        if isinstance(argv, list) and len(argv) >= 2 and argv[0:2] == ["gh", "secret"]:
            captured.append((list(argv), kwargs.get("input", "")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pps.subprocess, "run", fake_run)

    pps.apply_secrets(_make_args(key=None))

    ssh_key_calls = [argv for argv, _ in captured if "SERVER_SSH_KEY" in argv]
    assert ssh_key_calls == []


def test_create_deploy_user_refuses_for_root_deploy():
    args = type(
        "A", (), {"user": "root", "deploy_user": "root", "app_dir": "/opt/preview"}
    )()
    fake = _FakeRemote([])
    with pytest.raises(SystemExit) as excinfo:
        pps.create_deploy_user(fake, args)
    assert "--deploy-user cannot be root" in str(excinfo.value)


def test_create_deploy_user_refuses_same_as_ssh_user():
    args = type(
        "A", (), {"user": "deploy", "deploy_user": "deploy", "app_dir": "/opt/preview"}
    )()
    fake = _FakeRemote([])
    with pytest.raises(SystemExit) as excinfo:
        pps.create_deploy_user(fake, args)
    assert "must differ from --user" in str(excinfo.value)


def test_create_deploy_user_runs_useradd_and_chowns_app_dir(capsys):
    """Fresh deploy: useradd, usermod -aG docker, chown /opt/preview. Does
    NOT touch authorized_keys (operator's --key must not be the deploy user's
    auth surface — that's --generate-deploy-key's job)."""
    args = type(
        "A",
        (),
        {
            "user": "root",
            "deploy_user": "deploy",
            "app_dir": "/opt/preview",
        },
    )()
    fake = _FakeRemote(["MISSING", ""])
    pps.create_deploy_user(fake, args)
    out = capsys.readouterr().out
    assert "user 'deploy' created" in out
    assert "NOT installed" in out
    # First run() probes id; second useradds; third chowns.
    joined = "\n".join(script for script, _live in fake.calls)
    assert "useradd -m -s /bin/bash deploy" in joined
    assert "usermod -aG docker deploy" in joined
    assert "chown -R deploy:deploy /opt/preview" in joined
    # CRITICAL: must NOT install any pub key into deploy user's home.
    assert "/home/deploy/.ssh" not in joined
    assert "authorized_keys" not in joined


def test_create_deploy_user_skips_useradd_when_exists(capsys):
    """If id <user> returns EXISTS, skip the useradd step (idempotent)."""
    args = type(
        "A",
        (),
        {
            "user": "root",
            "deploy_user": "deploy",
            "app_dir": "/opt/preview",
        },
    )()
    fake = _FakeRemote(["EXISTS", ""])
    pps.create_deploy_user(fake, args)
    joined = "\n".join(script for script, _live in fake.calls)
    assert "useradd -m" not in joined
    assert "usermod -aG docker" not in joined
    # chown still runs (idempotent)
    assert "chown -R deploy:deploy /opt/preview" in joined


def test_create_deploy_user_flag_is_parseable(monkeypatch):
    monkeypatch.setattr(
        pps.sys,
        "argv",
        [
            "prog",
            "--host",
            "h",
            "--user",
            "deploy",
            "--key",
            "/tmp/x",
            "--domain",
            "d.example.com",
            "--create-deploy-user",
        ],
    )
    args = pps.parse_args()
    assert args.create_deploy_user is True


def test_generate_deploy_key_installs_pub_to_deploy_when_flag_set(
    monkeypatch, tmp_path
):
    """With --create-deploy-user, the generated public key must land in
    /home/<deploy_user>/.ssh/authorized_keys (chowned to them) — not in
    the bootstrap user's home. The operator's --key is intentionally
    NOT a deploy-user auth surface."""
    captured: dict[str, list[str]] = {}

    class _FakeR:
        def __init__(self, _args):
            pass

        def run(self, script, **kwargs):
            captured.setdefault("runs", []).append(script)

            class _CP:
                stdout = "MISSING"
                stderr = ""
                returncode = 0

            return _CP()

        def scp_down(self, remote_path, local_path):
            Path(local_path).write_text("FAKE", encoding="utf-8")

    monkeypatch.setattr(pps.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    args = type("A", (), {"create_deploy_user": True, "deploy_user": "deploy"})()
    pps.generate_deploy_key(_FakeR(None), args)
    joined = "\n".join(captured["runs"])
    assert "/home/deploy/.ssh/authorized_keys" in joined
    assert "chown deploy:deploy" in joined
    # Must NOT install into the bootstrap user's ~/.ssh/authorized_keys.
    assert (
        'grep -qxF "$(cat ~/.ssh/preview_deploy_key.pub)" ~/.ssh/authorized_keys'
        not in joined
    )


def test_generate_deploy_key_uses_bootstrap_home_by_default(monkeypatch, tmp_path):
    """Without --create-deploy-deploy, the public key is appended to the
    bootstrap user's ~/.ssh/authorized_keys (legacy behaviour)."""
    captured: dict[str, list[str]] = {}

    class _FakeR:
        def __init__(self, _args=None):
            pass

        def run(self, script, **kwargs):
            captured.setdefault("runs", []).append(script)

            class _CP:
                stdout = "MISSING"
                stderr = ""
                returncode = 0

            return _CP()

        def scp_down(self, remote_path, local_path):
            Path(local_path).write_text("FAKE", encoding="utf-8")

    monkeypatch.setattr(pps.tempfile, "mkdtemp", lambda prefix="": str(tmp_path))
    pps.generate_deploy_key(_FakeR())  # no args → default
    joined = "\n".join(captured["runs"])
    assert "~/.ssh/authorized_keys" in joined
    assert "/home/deploy" not in joined


def test_install_authorized_key_expands_tilde(monkeypatch, tmp_path):
    """--key '~/...' must have ~ expanded before deriving the .pub path,
    otherwise pathlib sees a literal '~/...' and refuses."""
    key = tmp_path / "id_ed25519"
    key.write_text("PRIV", encoding="utf-8")
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAA x@local\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    calls: list[str] = []
    remote = type(
        "R",
        (),
        {
            "run": lambda self, script, **kw: calls.append(script)
            or type("CP", (), {"stdout": "", "stderr": "", "returncode": 0})()
        },
    )()
    pps.install_authorized_key(remote, "~/id_ed25519")
    assert calls, "should have run an install command"
    # The pub should have made it into the install script:
    assert "ssh-ed25519 AAAA x@local" in calls[0]
