#!/usr/bin/env python3
"""Provision a VPS for keda PR preview deployments.

Bootstraps a fresh server with Docker and Traefik so the GitHub Actions
``deploy-preview`` workflow can land per-PR preview stacks on it. After
this script finishes, follow the printed next-steps to wire up DNS,
GitHub Secrets, and ``config.toml [preview]``.

The script is intended for one-time setup of a preview host. It is
idempotent: re-running will skip what's already in place. Reuse flags
let you skip pieces you already have on the server.

Examples
--------

Login with your own SSH key (preferred over password)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy \\
        --key ~/.ssh/preview_id_ed25519 \\
        --domain preview.example.com \\
        --email you@example.com

The ``--key`` flag is for **logging into the server while running this
script**; the script installs your public key in the server's
``authorized_keys`` so you can keep using it on subsequent runs. It is
**not** the key used by GitHub Actions — for that, see
``--generate-deploy-key`` below.

Password (uses ``sshpass``; install via ``brew install sshpass``)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user root --password 'YOUR_PASSWORD' \\
        --domain preview.example.com --email you@example.com

HTTP-only (no TLS, internal/Tailscale access)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/... \\
        --domain preview.example.com --cert-mode http-only

Server already runs Docker (skip the apt-get install step)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/... \\
        --domain preview.example.com --email you@example.com \\
        --skip-docker

Server already runs Traefik (reuse it instead of installing ``preview-traefik``;
preview stacks will join its ``traefik`` network and rely on its routing)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/... \\
        --domain preview.example.com \\
        --skip-traefik

Both available (typical for shared multi-tenant hosts)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/... \\
        --domain preview.example.com \\
        --skip-docker --skip-traefik

Full reset (wipe ``preview-traefik`` and re-issue Let's Encrypt certs)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/... \\
        --domain preview.example.com --email you@example.com \\
        --force

Generate a fresh GitHub Actions deploy key on the server (preferred over
``--key`` because the private key never needs to live on the local
machine that runs this script — it is created on the server, scp'd to a
local temp file, and you pipe it into ``gh secret set``)::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/... \\
        --domain preview.example.com --email you@example.com \\
        --generate-deploy-key

After that, the script prints a ``gh secret set SERVER_SSH_KEY …`` line
that points at the local file and warns you to ``rm`` it once the
secret is uploaded.

First-time setup of a non-root deploy user (recommended for production
hosts). Connects as root, runs ``useradd`` + ``usermod -aG docker`` +
installs the local public key into ``/home/<user>/.ssh/authorized_keys``
+ ``chown`` the app dir, then re-opens the SSH master as the new user.
After this, every subsequent step — and the GitHub Actions workflow —
runs as the unprivileged ``deploy`` user::

    uv run python scripts/provision_preview_server.py \\
        --host 1.2.3.4 --user deploy --key ~/.ssh/preview_id_ed25519 \\
        --domain preview.example.com \\
        --skip-traefik --skip-docker \\
        --create-deploy-user --generate-deploy-key
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from textwrap import dedent


def _confirm(prompt: str, default_yes: bool = False) -> bool:
    """Read a y/N answer from stdin; return True iff the user answered yes.

    Falls back to the default on empty input. Refuses to read if stdin is
    not a TTY so the script fails cleanly in CI.
    """
    if not sys.stdin.isatty():
        print(f"(non-interactive stdin; auto-rejecting: {prompt})", file=sys.stderr)
        return False
    suffix = " [Y/n]" if default_yes else " [y/N]"
    response = input(prompt + suffix + " ").strip().lower()
    if not response:
        return default_yes
    return response in ("y", "yes")


def _print_cmd(cmd: list[str]) -> None:
    """Echo a command, redacting ``sshpass -p <password>`` only.

    SSH/SCP also use ``-p`` (port) and ``-P`` (port). Those must stay visible
    for debugging, so we only redact the password token that ``sshpass``
    always places at index 1 of the wrapped command.
    """
    redacted = list(cmd)
    if len(cmd) >= 3 and cmd[0] == "sshpass" and cmd[1] == "-p":
        redacted[2] = "***"
    print("$ " + " ".join(shlex.quote(token) for token in redacted), flush=True)


class Remote:
    """Thin wrapper over ``ssh``/``scp``, multiplexed over a single TCP/auth.

    The previous version opened a fresh SSH connection per command. On
    password-auth clouds (Aliyun, Tencent Cloud) each new connection re-runs
    the full PAM stack and the sshd MaxStartups throttle can stall the second
    call indefinitely. Here we open one SSH ``ControlMaster`` up front and
    reuse the unix-socket for every subsequent ``ssh``/``scp`` invocation, so
    we authenticate exactly once.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        # Keep the socket path well under the OS unix-socket limit (~104 on
        # macOS, ~108 on Linux). The hex suffix avoids collisions across
        # concurrent runs without needing a directory.
        self._control_path = f"/tmp/pp-{os.getpid()}-{secrets.token_hex(3)}.sock"
        self._master_started = False

    def _ssh_target(self) -> str:
        return f"{self.args.user}@{self.args.host}"

    def _common_ssh_opts(self) -> list[str]:
        return [
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={self.args.connect_timeout}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
        ]

    def _control_opts(self) -> list[str]:
        return [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self._control_path}",
            # Long persist: docker install or apt-get can leave the socket
            # idle for several minutes between SSH calls. If the master
            # expires mid-run, the next call re-auths and risks throttling.
            "-o",
            "ControlPersist=20m",
        ]

    def connect(self) -> None:
        """Open the SSH control master so later calls reuse the same TCP/auth.

        Idempotent: subsequent calls are no-ops. ``sshpass`` is only invoked
        here; once the master is up, ``-o ControlMaster=auto`` lets every
        subsequent ssh/scp reuse the socket without re-authenticating.

        We deliberately do NOT use ``-N -f`` here: ``sshpass`` uses a pty to
        intercept the password prompt, and ``-f`` detaches that pty, so the
        master daemon often fails to fork silently. Running a short command
        (``true``) and relying on ``ControlPersist`` to keep the master alive
        in the background is robust under both key and password auth.
        """
        if self._master_started:
            return
        cmd: list[str] = (
            ["ssh"]
            + self._common_ssh_opts()
            + self._control_opts()
            + [
                "-p",
                str(self.args.port),
            ]
        )
        if self.args.key:
            cmd += ["-i", self.args.key]
        cmd.append(self._ssh_target())
        cmd.append("true")
        if self.args.password:
            cmd = ["sshpass", "-p", self.args.password] + cmd
        _print_cmd(cmd)
        # 5 min covers the slowest cloud-image PAM stacks; ConnectTimeout
        # already caps the TCP handshake separately.
        cp = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=300
        )
        if cp.returncode != 0:
            if cp.stderr:
                sys.stderr.write(
                    f"--- ssh master stderr ---\n{cp.stderr}--- end stderr ---\n"
                )
            raise subprocess.CalledProcessError(
                cp.returncode, cmd, cp.stdout, cp.stderr
            )

        # Verify the master daemon actually backgrounded itself. If it didn't,
        # every later call would re-auth from scratch and probably stall on
        # sshd MaxStartups throttling — fail loud now instead.
        check_cmd = ["ssh"] + self._control_opts() + ["-O", "check", self._ssh_target()]
        _print_cmd(check_cmd)
        check = subprocess.run(check_cmd, capture_output=True, text=True, timeout=10)
        if check.returncode != 0:
            sys.stderr.write(
                "\n❌ SSH ControlMaster did not start.\n"
                f"   ssh -O check stderr: {check.stderr.strip()}\n"
                "   Without multiplexing, each command would re-auth and likely stall.\n"
                "   Try: pass an SSH key via --key instead of --password (much faster),\n"
                "   or check the server's sshd MaxStartups / fail2ban rules.\n"
            )
            raise subprocess.CalledProcessError(
                check.returncode, check_cmd, check.stdout, check.stderr
            )
        self._master_started = True

    def close(self) -> None:
        """Tear down the control master if it is up. Safe to call multiple times."""
        if not self._master_started:
            return
        self._master_started = False
        try:
            subprocess.run(
                ["ssh"] + self._control_opts() + ["-O", "exit", self._ssh_target()],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            pass
        try:
            os.unlink(self._control_path)
        except OSError:
            pass

    def _base_ssh(self) -> list[str]:
        """SSH argv that reuses the control socket; no sshpass needed here."""
        cmd: list[str] = (
            ["ssh"]
            + self._common_ssh_opts()
            + self._control_opts()
            + [
                "-p",
                str(self.args.port),
            ]
        )
        if self.args.key:
            cmd += ["-i", self.args.key]
        cmd.append(self._ssh_target())
        return cmd

    def _base_scp(self) -> list[str]:
        """SCP argv that reuses the SSH control socket."""
        cmd: list[str] = (
            ["scp"]
            + self._common_ssh_opts()
            + self._control_opts()
            + [
                "-P",
                str(self.args.port),
            ]
        )
        if self.args.key:
            cmd += ["-i", self.args.key]
        return cmd

    def run(
        self,
        script: str,
        *,
        check: bool = True,
        live: bool = False,
        timeout: int | None = None,
    ):
        """Run a shell snippet on the remote host.

        ``live=True`` streams stdout/stderr to the terminal for long-running
        commands like ``apt-get``. Otherwise output is captured and returned
        as a :class:`subprocess.CompletedProcess` whose ``.stdout`` holds
        the script's stdout.

        ``timeout`` is in seconds; pass ``None`` to use the default (180s for
        captured, 900s for live). Use ``bash -c`` (not ``-lc``) so login-shell
        rcfiles like ``/etc/profile.d/*`` don't add seconds to every call.

        The script is ``shlex.quote``-d into a single argument: SSH joins
        argv with spaces and the remote shell re-parses, so an unquoted
        ``bash -c cat /etc/os-release`` would degrade to ``bash -c cat`` with
        ``/etc/os-release`` as ``$0`` — cat then reads stdin and produces no
        output. Quoting keeps the whole snippet as one ``-c`` argument.
        """
        cmd = self._base_ssh() + ["bash", "-c", shlex.quote(script)]
        _print_cmd(cmd)
        effective_timeout = timeout if timeout is not None else (900 if live else 180)
        if live:
            proc = subprocess.Popen(cmd)
            try:
                rc = proc.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, effective_timeout)
            if check and rc != 0:
                raise subprocess.CalledProcessError(rc, cmd)
            return rc
        try:
            cp = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(cmd, effective_timeout) from None
        if check and cp.returncode != 0:
            # SSH failures (exit 255) are cryptic without stderr — surface them.
            if cp.stderr:
                sys.stderr.write(
                    f"--- remote stderr ---\n{cp.stderr}--- end stderr ---\n"
                )
            raise subprocess.CalledProcessError(
                cp.returncode, cmd, cp.stdout, cp.stderr
            )
        return cp

    def scp(self, local: Path, remote: str) -> None:
        cmd = self._base_scp() + [
            str(local),
            f"{self.args.user}@{self.args.host}:{remote}",
        ]
        _print_cmd(cmd)
        subprocess.run(cmd, check=True)

    def scp_down(self, remote: str, local: "Path") -> None:
        """Pull a file from the server to the local path. Symmetric of ``scp``."""
        cmd = self._base_scp() + [
            f"{self.args.user}@{self.args.host}:{remote}",
            str(local),
        ]
        _print_cmd(cmd)
        subprocess.run(cmd, check=True)


_DEPLOY_KEY_REMOTE_PATH = ".ssh/preview_deploy_key"


def generate_deploy_key(remote: Remote, args: argparse.Namespace | None = None) -> Path:
    """Generate an ed25519 key pair on the server for GitHub Actions to use.

    The private key never lives on the local machine that runs this script;
    it is generated on the server, scp'd to a local temp file only so the
    operator can pipe it into ``gh secret set``, and the temp file is
    returned for the caller to handle. Public key is appended to a target
    ``authorized_keys`` so ``ssh -i <key> user@host`` works immediately.

    Target for the public key:

    - If ``args.create_deploy_user`` is set: ``/home/<deploy_user>/.ssh/authorized_keys``
      (chown to that user). This is the only key that user can authenticate
      with — keep the operator's login key off that surface.
    - Otherwise: ``~/.ssh/authorized_keys`` (i.e. the SSH bootstrap user's home).

    Returns the local path to the private key file (mode 0600). The caller
    must remind the user to delete it after uploading to GitHub.
    """
    if args is not None and getattr(args, "create_deploy_user", False):
        target_ak = f"/home/{args.deploy_user}/.ssh/authorized_keys"
        target_owner = f"{args.deploy_user}:{args.deploy_user}"
        install_block = (
            f"install -d -m 700 -o {args.deploy_user} -g {args.deploy_user} "
            f"/home/{args.deploy_user}/.ssh\n"
        )
    else:
        target_ak = "~/.ssh/authorized_keys"
        target_owner = "$(stat -c '%U:%G' ~ 2>/dev/null || echo root:root)"
        install_block = "mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"

    exists = remote.run(
        f"if [ -f ~/{_DEPLOY_KEY_REMOTE_PATH} ]; then echo EXISTS; else echo MISSING; fi"
    )
    if "EXISTS" in exists.stdout:
        raise SystemExit(
            f"ERROR: ~/{_DEPLOY_KEY_REMOTE_PATH} already exists on the server.\n"
            "   Refusing to overwrite. To rotate, delete it (or remove it from\n"
            "   the server) and re-run with --generate-deploy-key."
        )
    remote.run(
        f"""
set -euo pipefail
ssh-keygen -t ed25519 -f ~/{_DEPLOY_KEY_REMOTE_PATH} -N '' \\
    -C 'github-actions-preview-deployment'
{install_block}touch {target_ak} && chmod 600 {target_ak}
if ! grep -qxF "$(cat ~/{_DEPLOY_KEY_REMOTE_PATH}.pub)" {target_ak}; then
    cat ~/{_DEPLOY_KEY_REMOTE_PATH}.pub >> {target_ak}
fi
chown {target_owner} {target_ak} 2>/dev/null || true
chmod 600 ~/{_DEPLOY_KEY_REMOTE_PATH} ~/{_DEPLOY_KEY_REMOTE_PATH}.pub
""",
        live=True,
    )
    local_dir = Path(tempfile.mkdtemp(prefix="preview-deploy-key-"))
    local_path = local_dir / "id_ed25519"
    remote.scp_down(_DEPLOY_KEY_REMOTE_PATH, local_path)
    os.chmod(local_path, 0o600)
    return local_path


def detect_os(remote: Remote) -> dict[str, str]:
    """Parse ``/etc/os-release`` into a key/value dict."""
    cp = remote.run("cat /etc/os-release")
    info: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        info[key.strip()] = value.strip().strip('"').strip("'")
    return info


def install_docker(remote: Remote, os_info: dict[str, str]) -> None:
    """Install Docker via the official repo. Idempotent."""
    has = remote.run(
        "command -v docker >/dev/null 2>&1 && echo INSTALLED || echo MISSING"
    )
    if "INSTALLED" in has.stdout:
        # Detect a half-installed Docker (binary present, daemon not running).
        ping = remote.run("docker info >/dev/null 2>&1 && echo OK || echo NOT_RUNNING")
        if "NOT_RUNNING" in ping.stdout:
            print("Docker binary present but daemon not running; starting it")
            remote.run(
                "(systemctl enable --now docker 2>/dev/null "
                "|| service docker start 2>/dev/null) || true",
                live=True,
            )
        version_cp = remote.run("docker --version")
        print(f"Docker already present: {version_cp.stdout.strip()}")
        return

    os_id = os_info.get("ID", "")
    if os_id in {"debian", "ubuntu"}:
        remote.run(
            dedent("""\
                set -euo pipefail
                export DEBIAN_FRONTEND=noninteractive
                . /etc/os-release
                apt-get update -y
                apt-get install -y ca-certificates curl gnupg
                install -m 0755 -d /etc/apt/keyrings
                curl -fsSL "https://download.docker.com/linux/${ID}/gpg" \\
                    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
                chmod a+r /etc/apt/keyrings/docker.gpg
                echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \\
                    > /etc/apt/sources.list.d/docker.list
                apt-get update -y
                apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                systemctl enable --now docker
            """),
            live=True,
        )
    elif os_id in {"rhel", "centos", "rocky", "almalinux", "fedora", "amzn"}:
        remote.run(
            dedent("""\
                set -euo pipefail
                dnf -y install dnf-plugins-core
                dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
                dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
                systemctl enable --now docker
            """),
            live=True,
        )
    else:
        raise SystemExit(
            f"Unsupported OS id: {os_id!r}. Install Docker manually then re-run with --skip-docker."
        )

    if remote.args.user != "root":
        print(
            f"Note: {remote.args.user} is not in the docker group. "
            f"After this script finishes, run on the server:\n"
            f"    sudo usermod -aG docker {remote.args.user} && logout\n"
            f"Or have your deploy script prefix `docker` commands with `sudo`."
        )


_HTTP_ONLY_CONFIG = """\
api:
  dashboard: true
entryPoints:
  web:
    address: ":80"
providers:
  docker:
    network: traefik
    exposedByDefault: false
log:
  level: INFO
"""

_ACME_HTTP01_CONFIG = """\
api:
  dashboard: true
entryPoints:
  web:
    address: ":80"
  websecure:
    address: ":443"
certificatesResolvers:
  letsencrypt:
    acme:
      email: "{email}"
      storage: /letsencrypt/acme.json
      httpChallenge:
        entryPoint: web
providers:
  docker:
    network: traefik
    exposedByDefault: false
log:
  level: INFO
"""


def render_traefik_config(cert_mode: str, email: str | None) -> str:
    """Return the Traefik static config YAML for the requested cert mode.

    Raises :class:`ValueError` if ``cert_mode`` is ``acme-http01`` and no
    email is provided.
    """
    if cert_mode == "http-only":
        return _HTTP_ONLY_CONFIG
    if not email:
        raise ValueError("email required for cert-mode=acme-http01")
    return _ACME_HTTP01_CONFIG.format(email=email)


def read_existing_email(remote: Remote, cfg_dir: str) -> str | None:
    """Extract the ACME email from an existing ``traefik.yml`` on the server.

    Returns ``None`` if no ``email:`` line is present. Lets subsequent
    re-runs of the script omit ``--email`` and still keep the same LE
    account registered.
    """
    target = f"{cfg_dir}/traefik.yml"
    cp = remote.run(
        f"grep -E '^[[:space:]]+email:' {shlex.quote(target)} 2>/dev/null || true"
    )
    for line in cp.stdout.splitlines():
        if "email:" not in line:
            continue
        value = line.split("email:", 1)[1].strip()
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        if value:
            return value
    return None


def upload_traefik_config(remote: Remote, app_dir: str, body: str) -> str:
    """Write the static config to ``<app_dir>/traefik/traefik.yml``."""
    cfg_dir = f"{app_dir}/traefik"
    remote.run(f"mkdir -p {shlex.quote(cfg_dir)}/letsencrypt")
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".yml", encoding="utf-8"
    ) as f:
        f.write(body)
        local = Path(f.name)
    try:
        remote.scp(local, f"{cfg_dir}/traefik.yml")
    finally:
        local.unlink(missing_ok=True)
    return cfg_dir


def _parse_traefik_state(stdout: str) -> tuple[bool, bool]:
    """Parse ``docker ps -a --format '{{.Names}} {{.State}}'`` output.

    Returns ``(has_container, is_running)``. The ``--filter name=`` flag is a
    substring match, so the output can contain unrelated containers like
    ``preview-traefik-helper``; pick the exact name.
    """
    for line in stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "preview-traefik":
            return True, parts[1] == "running"
    return False, False


def ensure_traefik(
    remote: Remote, app_dir: str, desired_cfg: str, *, force: bool = False
) -> None:
    """Idempotently reconcile the ``preview-traefik`` container with ``desired_cfg``.

    Preserves any existing ``letsencrypt/acme.json`` so issued certs are not
    lost across re-runs. With ``force=True``, removes the container and the
    ACME storage so certs get re-issued from scratch.
    """
    cfg_dir = f"{app_dir}/traefik"
    target_yml = f"{cfg_dir}/traefik.yml"
    le_dir = f"{cfg_dir}/letsencrypt"
    le_json = f"{le_dir}/acme.json"

    if force:
        print(
            "Force mode: removing any existing preview-traefik container and acme.json"
        )
        remote.run("docker rm -f preview-traefik 2>/dev/null || true", live=True)
        remote.run(f"rm -f {shlex.quote(le_json)}", live=True)

    # Always make sure the letsencrypt dir + a 0600 acme.json exist. Traefik
    # refuses to start if the file is missing or world-readable.
    remote.run(
        f"mkdir -p {shlex.quote(le_dir)} && "
        f"if [ ! -f {shlex.quote(le_json)} ]; then "
        f"  touch {shlex.quote(le_json)} && chmod 600 {shlex.quote(le_json)}; "
        f"fi"
    )

    state = remote.run(
        "docker ps -a --filter name=preview-traefik " "--format '{{.Names}} {{.State}}'"
    )
    has_container, is_running = _parse_traefik_state(state.stdout)

    cfg_matches = False
    if has_container:
        cur = remote.run(
            f"cat {shlex.quote(target_yml)} 2>/dev/null || echo __MISSING__"
        )
        cfg_matches = cur.stdout.strip() == desired_cfg.strip()

    if has_container and cfg_matches and is_running:
        print("✓ preview-traefik already running with current config; skipping")
        return

    if has_container and not cfg_matches:
        print("Traefik config differs; rewriting and recreating container")
        upload_traefik_config(remote, app_dir, desired_cfg)
    elif has_container and cfg_matches and not is_running:
        print("Container exists but stopped; starting it")
    else:
        print("No preview-traefik container found; creating fresh")
        upload_traefik_config(remote, app_dir, desired_cfg)

    if has_container:
        remote.run("docker rm -f preview-traefik", live=True)

    _run_traefik_container(remote, cfg_dir)


def _run_traefik_container(remote: Remote, cfg_dir: str) -> None:
    """Create the ``preview-traefik`` container and join the external ``traefik`` network."""
    remote.run(
        dedent(f"""\
            set -euo pipefail
            docker network create traefik 2>/dev/null || true
            docker run -d --name preview-traefik --restart=unless-stopped \\
                -p 80:80 -p 443:443 \\
                -v /var/run/docker.sock:/var/run/docker.sock:ro \\
                -v {shlex.quote(cfg_dir)}:/etc/traefik:rw \\
                -v {shlex.quote(cfg_dir)}/letsencrypt:/letsencrypt:rw \\
                --network traefik \\
                traefik:v3.1
            docker ps --filter name=preview-traefik --format '{{{{.Status}}}}'
        """),
        live=True,
    )


def ensure_external_traefik_network(remote: Remote) -> None:
    """Make sure the docker network ``traefik`` exists when reusing the user's
    own Traefik (``--skip-traefik`` mode).

    Preview compose stacks always attach to this network and rely on the
    user's Traefik to route to them via docker labels. We:

    1. Create the ``traefik`` network if missing (no-op if it already exists).
    2. Look for any running container whose **image** name contains ``traefik``
       and warn if none of them is attached to the ``traefik`` network —
       otherwise the user's Traefik won't discover preview containers.
    """
    remote.run("docker network create traefik 2>/dev/null || true", live=True)
    state = remote.run(
        "docker ps --format '{{.Names}}|{{.Image}}|{{.Networks}}' || true"
    )
    traefik_containers: list[tuple[str, list[str]]] = []
    for line in state.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 3:
            continue
        name, image, networks = parts
        # Match on image only — matching on the Networks column would
        # mis-report every container that *uses* the traefik network as if
        # it were a Traefik instance.
        if "traefik" in image.lower():
            traefik_containers.append((name, [n.strip() for n in networks.split(",")]))
    if not traefik_containers:
        print(
            "  ⚠ No running Traefik-like container found. --skip-traefik assumes "
            "you already run one; preview routing won't work until it is up."
        )
        return
    attached = [name for name, nets in traefik_containers if "traefik" in nets]
    if attached:
        for name in attached:
            print(f"  ✓ existing Traefik `{name}` is attached to the `traefik` network")
    else:
        names = ", ".join(name for name, _ in traefik_containers)
        print(
            f"  ⚠ Found Traefik container(s) [{names}] but none is attached to the `traefik` network.\n"
            f"     Attach them so preview stacks become routable:\n"
            f"         docker network connect traefik <traefik-container>"
        )


def check_port_conflict(remote: Remote) -> bool:
    """Return True if 80/443 are free (for ``preview-traefik`` to bind), False otherwise.

    When False, prints a clear pointer to ``--skip-traefik``. Run this BEFORE
    ``ensure_traefik`` so we abort early instead of crashing inside docker
    with a generic "port is already allocated" error.
    """
    out = remote.run(
        "command -v ss >/dev/null 2>&1 && ss -tlnp 2>/dev/null "
        "| grep -E ':80\\b|:443\\b' "
        "| grep -v preview-traefik || true"
    )
    if not out.stdout.strip():
        return True
    print("❌ Ports 80/443 are already bound by another process:")
    for line in out.stdout.strip().splitlines():
        print(f"    {line}")
    print(
        "   preview-traefik cannot start while these ports are taken.\n"
        "   If you already run your own Traefik / reverse proxy, re-run with:\n"
        "       --skip-traefik\n"
        "   Otherwise stop the conflicting process (or use --force to wipe a\n"
        "   stale preview-traefik, but that won't help if a non-preview\n"
        "   container owns the ports)."
    )
    return False


def create_deploy_user(remote: Remote, args: argparse.Namespace) -> None:
    """Bootstrap a non-root deployment user on the server.

    Steps performed (the caller must still have root authority on the
    master socket — i.e. ``--user root`` or a sudo-capable user):

    1. ``useradd -m -s /bin/bash <deploy_user>`` (idempotent: skipped if exists)
    2. ``usermod -aG docker <deploy_user>`` so docker commands don't need sudo
    3. ``mkdir -p /opt/preview && chown <deploy_user>`` so the deploy
       workflow can write compose files without sudo.

    Deliberately does NOT install ``--key``'s public key into the new
    user's ``authorized_keys``: ``--key`` is the human operator's login
    key, scoped to the bootstrap user (``--user``). The deploy user's
    only SSH authentication must come from ``--generate-deploy-key``
    (the GitHub Actions deploy key), keeping the two keys separate and
    the operator's private key off the server's deploy surface.
    """
    if args.deploy_user == "root":
        raise SystemExit("ERROR: --deploy-user cannot be root.")
    if args.deploy_user == args.user:
        raise SystemExit(
            "ERROR: --deploy-user must differ from --user. The SSH user must "
            "already exist (so we can authenticate to useradd); the deploy "
            "user is created fresh."
        )

    probe = remote.run(
        f"id {shlex.quote(args.deploy_user)} >/dev/null 2>&1 && echo EXISTS || echo MISSING"
    )
    if "EXISTS" in probe.stdout:
        print(
            f"  --create-deploy-user: user {args.deploy_user!r} already exists; skipping useradd"
        )
    else:
        remote.run(
            f"""
set -euo pipefail
useradd -m -s /bin/bash {shlex.quote(args.deploy_user)}
usermod -aG docker {shlex.quote(args.deploy_user)}
""",
            live=True,
        )
        print(f"  ✓ user {args.deploy_user!r} created; added to docker group")

    remote.run(
        f"""
set -euo pipefail
mkdir -p {shlex.quote(args.app_dir)}
chown -R {shlex.quote(args.deploy_user)}:{shlex.quote(args.deploy_user)} {shlex.quote(args.app_dir)}
""",
        live=True,
    )
    print(f"  ✓ {args.app_dir} owned by {args.deploy_user}")
    print(
        f"  ℹ  --key was NOT installed into /home/{args.deploy_user}/.ssh/authorized_keys.\n"
        f"     The deploy user will only be reachable by the key generated\n"
        f"     via --generate-deploy-key (next step)."
    )


def install_authorized_key(remote: Remote, key_path: str) -> None:
    """Make sure the local public key is in the server's ``authorized_keys``
    so password-based logins can be disabled later. Idempotent."""
    if not key_path:
        return
    # Expand a leading ``~`` to the user's home. Python's Path does not do
    # tilde expansion on its own, so users who wrote ``--key ~/.ssh/X``
    # would otherwise get a literal "~/..." path that doesn't exist.
    expanded = os.path.expanduser(key_path)
    pub_path = Path(expanded + ".pub")
    if not pub_path.exists():
        print(f"No public key at {pub_path}; skipping authorized_keys install.")
        return
    pub = pub_path.read_text(encoding="utf-8").strip()
    quoted = shlex.quote(pub)
    remote.run(
        f"""\
set -euo pipefail
mkdir -p ~/.ssh && chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
if grep -qxF {quoted} ~/.ssh/authorized_keys; then
  echo "key already in authorized_keys"
else
  echo {quoted} >> ~/.ssh/authorized_keys
  echo "key appended to authorized_keys"
fi
"""
    )


def print_next_steps_header(args: argparse.Namespace, app_dir: str) -> None:
    """Print the success banner and server-state summary once at the top."""
    print()
    print("=" * 72)
    print("✅  Provisioning complete")
    print("=" * 72)
    print()
    print("Server state:")
    print("  - Docker installed and running")
    if args.skip_traefik:
        print(
            "  - Traefik: using your existing container (--skip-traefik); "
            "preview stacks attach to the `traefik` network"
        )
    else:
        print(
            "  - Traefik running as container `preview-traefik` on the `traefik` network"
        )
        print(f"  - Cert mode:    {args.cert_mode}")
    print(f"  - App dir:      {app_dir}")
    print()


def print_dns_instructions(args: argparse.Namespace) -> None:
    """Print DNS A-record instructions (always shown; out of script reach)."""
    print("1. Configure DNS at your registrar:")
    print(f"     *.{args.domain}    A    {args.host}")
    if args.cert_mode == "acme-http01":
        print(f"     {args.domain}      A    {args.host}    # apex for ACME account")
    print()


def print_secrets_instructions(args: argparse.Namespace) -> None:
    """Print the gh secret set commands as copy-paste text."""
    print("2. Set GitHub Secrets on the `preview` environment:")
    print(f'     gh secret set SERVER_HOST        --env preview --body "{args.host}"')
    server_user = args.deploy_user if args.create_deploy_user else args.user
    print(f'     gh secret set SERVER_USER        --env preview --body "{server_user}"')
    deploy_key_path = os.environ.get("PREVIEW_DEPLOY_KEY_PATH")
    if deploy_key_path:
        print(
            f"     gh secret set SERVER_SSH_KEY     --env preview < {shlex.quote(deploy_key_path)}"
        )
        print(
            f"     ⚠  Delete the local private key after upload: rm {shlex.quote(deploy_key_path)}"
        )
    elif args.key:
        print(
            f"     gh secret set SERVER_SSH_KEY     --env preview < {shlex.quote(args.key)}"
        )
    print(
        '     gh secret set REGISTRY_USERNAME  --env preview --body "<your gh username>"'
    )
    print('     gh secret set REGISTRY_PASSWORD  --env preview --body "<ghp_...>"')
    print(
        '     gh secret set POSTGRES_PASSWORD  --env preview --body "<strong random>"'
    )
    print()


def print_config_instructions(args: argparse.Namespace, gh_namespace: str) -> None:
    """Print the config.toml [preview] template as copy-paste text."""
    scheme = "https" if args.cert_mode == "acme-http01" else "http"
    print("3. Fill `config.toml [preview]`:")
    print("     [preview]")
    print("     enabled = true")
    print(f'     base_domain = "{args.domain}"')
    print(f'     app_dir_root = "{args.app_dir}"')
    print(f'     url_scheme = "{scheme}"')
    print('     registry_host = "ghcr.io"')
    print(f'     registry_namespace = "{gh_namespace}"')
    print('     traefik_network = "traefik"')
    print()


def print_post_apply(args: argparse.Namespace, email: str | None) -> None:
    """Print the final 'open a test PR' instruction and any mode-specific warnings."""
    print("4. Open a test PR; the `Preview Deployment` workflow should post a")
    print("   🟢 sticky comment with the preview URL once the build finishes.")
    # The Let's Encrypt warnings only apply when *we* manage Traefik. With
    # --skip-traefik the user's own Traefik (or upstream) owns cert issuance.
    if not args.skip_traefik and args.cert_mode == "acme-http01":
        print()
        print(
            "⚠  Cert mode reminder: Let's Encrypt limits ~50 certs/registered-domain/week."
        )
        print("   If you expect more active PRs, plan a DNS-01 wildcard switch.")
        if not email:
            print()
            print(
                "ℹ  No ACME email is registered (you omitted --email and no existing config was found)."
            )
            print(
                "   LE will still issue certs, but you won't get expiry notifications."
            )
            print("   Re-run with --email <you@example.com> to enable them.")
    print()


# ---------- apply (interactive) ----------


def _gh_repo_slug() -> str | None:
    """Return the current repo's ``owner/name`` via ``gh``, or ``None`` on failure."""
    if not shutil.which("gh"):
        return None
    res = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _gh_auth_ok() -> bool:
    if not shutil.which("gh"):
        return False
    res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return res.returncode == 0


def _gh_secret_exists(name: str) -> bool:
    """Return True if ``name`` is already set on the ``preview`` environment."""
    res = subprocess.run(
        ["gh", "secret", "list", "--env", "preview"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return False
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        # First column is the secret name; skip header.
        if line.split()[0] == name:
            return True
    return False


def apply_secrets(args: argparse.Namespace) -> bool:
    """Interactively set the GitHub Secrets for the ``preview`` environment.

    Returns True if every requested secret was either already correct or
    successfully set; False if any step could not be performed (in which
    case the caller should fall back to the print-only instructions).
    """
    print()
    print("=" * 72)
    print("📦  Apply GitHub Secrets")
    print("=" * 72)

    if not shutil.which("gh"):
        print("ERROR: gh CLI not installed (https://cli.github.com/).", file=sys.stderr)
        return False
    if not _gh_auth_ok():
        print(
            "ERROR: gh not authenticated. Run `gh auth login` first.", file=sys.stderr
        )
        return False

    repo = _gh_repo_slug()
    if not repo:
        print(
            "ERROR: not in a GitHub repo (or no remote). Run from the keda repo.",
            file=sys.stderr,
        )
        return False
    print(f"Repo: {repo}")

    # Make sure the preview environment exists.
    env_check = subprocess.run(
        ["gh", "api", f"repos/{repo}/environments/preview"],
        capture_output=True,
        text=True,
    )
    if env_check.returncode != 0:
        if not _confirm(
            f"  `preview` env does not exist in {repo}. Create it?", default_yes=True
        ):
            return False
        create = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repo}/environments/preview",
                "-X",
                "PUT",
                # GitHub requires wait_timer as an integer; -F sends the
                # field typed (without the explicit "0" stringification
                # that -f applies, which GitHub rejects with 422).
                "-F",
                "wait_timer=0",
            ],
            capture_output=True,
            text=True,
        )
        if create.returncode != 0:
            print(f"ERROR: failed to create env: {create.stderr}", file=sys.stderr)
            return False
        # GitHub takes a few seconds to propagate a freshly-created
        # environment to the secrets API. Without this sleep, the very
        # first `gh secret set` call returns EOF and the user has to retry.
        print("  ✓ preview env created; waiting 5s for it to propagate…")
        time.sleep(5)
        print("  ✓ preview env created")

    server_user = args.deploy_user if args.create_deploy_user else args.user
    deploy_key_path = os.environ.get("PREVIEW_DEPLOY_KEY_PATH")
    specs: list[tuple[str, str, str | None]] = [
        ("SERVER_HOST", "literal", args.host),
        ("SERVER_USER", "literal", server_user),
    ]
    if deploy_key_path:
        specs.append(("SERVER_SSH_KEY", "file", deploy_key_path))
    elif args.key:
        specs.append(("SERVER_SSH_KEY", "file", args.key))
    specs += [
        ("REGISTRY_USERNAME", "prompt", "GitHub username (ghcr.io namespace)"),
        (
            "REGISTRY_PASSWORD",
            "prompt_secret",
            "Classic GitHub PAT (fine-grained does NOT support packages:write). "
            "https://github.com/settings/tokens/new — scopes: write:packages + "
            "read:packages + repo. Verify: "
            "echo $TOKEN | docker login ghcr.io -u <user> --password-stdin",
        ),
        ("POSTGRES_PASSWORD", "generate", None),
    ]

    all_ok = True
    for name, kind, payload in specs:
        print()
        if _gh_secret_exists(name):
            if not _confirm(f"  {name} already exists. Overwrite?", default_yes=False):
                print(f"  - skipping {name}")
                continue

        if kind == "literal":
            value = payload or ""
            preview = value if len(value) <= 40 else value[:37] + "..."
        elif kind == "file":
            value = Path(os.path.expanduser(payload)).read_text(encoding="utf-8")
            preview = f"<contents of {payload}>"
        elif kind == "prompt":
            value = input(f"  {name} ({payload}): ").strip()
            preview = (value[:30] + "...") if len(value) > 30 else value
        elif kind == "prompt_secret":
            value = getpass.getpass(f"  {name}: ")
            preview = "***"
        elif kind == "generate":
            import secrets as _sec

            value = _sec.token_urlsafe(32)
            preview = value[:12] + "..."
            print(f"  Generated {name}: {value}")
            print("  ⚠ Save this — you need it to drop the preview database later.")
        else:
            continue

        if not value:
            print(f"  - {name} is empty, skipping")
            all_ok = False
            continue

        if kind in ("literal", "prompt") and not _confirm(
            f"  Set {name} = {preview}?", default_yes=True
        ):
            continue

        result = subprocess.run(
            ["gh", "secret", "set", name, "--env", "preview"],
            input=value,
            text=True,
            capture_output=True,
        )
        # One retry to absorb transient "EOF" / "Not Found" responses from
        # GitHub's environment-propagation race right after env creation.
        if result.returncode != 0:
            time.sleep(5)
            result = subprocess.run(
                ["gh", "secret", "set", name, "--env", "preview"],
                input=value,
                text=True,
                capture_output=True,
            )
        if result.returncode != 0:
            print(f"  ERROR setting {name}: {result.stderr.strip()}", file=sys.stderr)
            all_ok = False
            continue
        print(f"  ✓ {name} set")

    print()
    return all_ok


def _build_preview_section(args: argparse.Namespace, gh_namespace: str) -> str:
    scheme = "https" if args.cert_mode == "acme-http01" else "http"
    return (
        "[preview]\n"
        "enabled = true\n"
        f'base_domain = "{args.domain}"\n'
        f'app_dir_root = "{args.app_dir}"\n'
        f'url_scheme = "{scheme}"\n'
        'registry_host = "ghcr.io"\n'
        f'registry_namespace = "{gh_namespace}"\n'
        'traefik_network = "traefik"\n'
    )


def _find_config_toml() -> Path | None:
    """Walk up from cwd looking for ``config.toml``."""
    for candidate in (Path.cwd(), *Path.cwd().parents):
        target = candidate / "config.toml"
        if target.is_file():
            return target
    return None


def _merge_preview_section(content: str, new_section: str) -> str:
    """Replace the ``[preview]`` block in ``content``; append if missing."""
    pattern = r"(?ms)^\[preview\].*?(?=^\[|\Z)"
    if re.search(pattern, content):
        return re.sub(
            pattern,
            new_section.strip() + "\n\n",
            content,
            count=1,
        )
    return content.rstrip() + "\n\n" + new_section


def apply_config(args: argparse.Namespace, gh_namespace: str) -> bool:
    """Interactively update ``config.toml``'s ``[preview]`` section.

    Returns True if the file is up to date or the diff was applied;
    False on any unrecoverable error (caller falls back to print).
    """
    print()
    print("=" * 72)
    print("📝  Apply config.toml")
    print("=" * 72)

    config_path = _find_config_toml()
    if not config_path:
        print(
            f"ERROR: config.toml not found in {Path.cwd()} or parent dirs.",
            file=sys.stderr,
        )
        return False
    print(f"  Found: {config_path}")

    current = config_path.read_text(encoding="utf-8")
    new_section = _build_preview_section(args, gh_namespace)
    new_content = _merge_preview_section(current, new_section)

    if current == new_content:
        print("  ✓ config.toml already up to date; nothing to do.")
        return True

    print()
    print("Proposed diff:")
    print("-" * 72)
    diff = difflib.unified_diff(
        current.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=str(config_path),
        tofile=str(config_path),
        n=3,
    )
    for line in diff:
        sys.stdout.write(line)
    print("-" * 72)
    print()

    if not _confirm("Apply this diff?", default_yes=True):
        return False

    backup = config_path.with_suffix(config_path.suffix + ".bak")
    backup.write_text(current, encoding="utf-8")
    config_path.write_text(new_content, encoding="utf-8")
    print(f"  ✓ {config_path} updated (backup: {backup.name})")
    print("  → Commit the config.toml change to your repo.")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision a VPS for keda PR preview deployments.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", required=True, help="Server hostname or IP")
    parser.add_argument(
        "--user",
        default="root",
        help="SSH user used to connect to the server while running this script "
        "(default: root). The useradd + key install + chown performed by "
        "--create-deploy-user need root authority, so this should stay root "
        "on first run; later runs can use the deploy user directly.",
    )
    parser.add_argument(
        "--deploy-user",
        default="deploy",
        help="Name of the non-root user --create-deploy-user will bootstrap "
        "(default: deploy). Must differ from --user (you can't useradd the "
        "user you SSH in as). After creation the script reconnects as this "
        "user, and GitHub Actions' SERVER_USER secret should also be set to "
        "this name.",
    )
    parser.add_argument("--port", type=int, default=22, help="SSH port (default: 22)")
    parser.add_argument("--key", help="Path to SSH private key (preferred)")
    parser.add_argument(
        "--password",
        help="SSH password (uses sshpass; install via `brew install sshpass` on macOS)",
    )
    parser.add_argument("--connect-timeout", type=int, default=15)
    parser.add_argument(
        "--domain",
        required=True,
        help="Base preview domain, e.g. preview.example.com",
    )
    parser.add_argument(
        "--email",
        help="Contact email for Let's Encrypt. Required on first install with "
        "--cert-mode acme-http01; re-runs reuse the email from the existing "
        "traefik.yml if this flag is omitted.",
    )
    parser.add_argument(
        "--app-dir",
        default="/opt/preview",
        help="Where preview stacks live on the server (default: /opt/preview)",
    )
    parser.add_argument(
        "--cert-mode",
        choices=["acme-http01", "http-only"],
        default="acme-http01",
        help="acme-http01: Let's Encrypt HTTP-01 per-SNI (no DNS token needed; rate-limited per-domain/week). "
        "http-only: no TLS, internal/Tailscale access only.",
    )
    parser.add_argument(
        "--skip-docker",
        action="store_true",
        help="Skip Docker install (assume present)",
    )
    parser.add_argument(
        "--skip-traefik",
        action="store_true",
        help="Skip installing the preview-traefik container. Use this when the "
        "server already runs a Traefik that owns 80/443. The script will "
        "still ensure the `traefik` docker network exists and warn if the "
        "existing Traefik isn't attached to it.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Test SSH connection and report what would be done, then exit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe any existing preview-traefik container and letsencrypt/acme.json before installing",
    )
    parser.add_argument(
        "--apply-secrets",
        action="store_true",
        help="Interactively set GitHub Secrets on the preview environment via gh CLI",
    )
    parser.add_argument(
        "--apply-config",
        action="store_true",
        help="Interactively update config.toml [preview] section in the current repo",
    )
    parser.add_argument(
        "--generate-deploy-key",
        action="store_true",
        help="Generate a fresh ed25519 key pair on the server (NOT on the local "
        "machine) for GitHub Actions to use. The public key is appended "
        "to the server's authorized_keys; the private key is scp'd to a "
        "local temp file so you can pipe it into `gh secret set "
        "SERVER_SSH_KEY --env preview < /path/to/key`. Recommended over "
        "passing --key, which requires the private key to already be on "
        "the local machine.",
    )
    parser.add_argument(
        "--create-deploy-user",
        action="store_true",
        help="Bootstrap a non-root deployment user (``--user``) on the server: "
        "useradd, add to the docker group, install --key's public key into "
        "/home/<user>/.ssh/authorized_keys, and chown --app-dir to the new "
        "user. The script then re-opens its SSH control master as the new "
        "user, so every subsequent step runs with non-root authority. "
        "Requires --key (the new user has no password).",
    )

    args = parser.parse_args()
    if not args.key and not args.password:
        parser.error("Provide either --key (preferred) or --password")
    if args.password and not shutil.which("sshpass"):
        parser.error(
            "--password requires sshpass. Install it: "
            "brew install sshpass (macOS) or apt install sshpass (Debian/Ubuntu)"
        )
    return args


def main() -> int:
    args = parse_args()
    remote = Remote(args)

    print(f"==> Opening SSH control master to {args.user}@{args.host}:{args.port}")
    try:
        remote.connect()
    except subprocess.CalledProcessError:
        sys.stderr.write(
            f"\n❌ SSH connection to {args.user}@{args.host}:{args.port} failed.\n"
            f"\n"
            f"Test manually for the actual error:\n"
            f"    ssh -i {args.key or '<key>'} -p {args.port} {args.user}@{args.host} echo hi\n"
            f"\n"
            f"Common causes:\n"
            f"  - Wrong host or IP, wrong password, or wrong username\n"
            f"    (Aliyun Ubuntu defaults to 'ubuntu'; some images use 'root')\n"
            f"  - Server firewall / cloud security group blocks port {args.port}\n"
            f"  - SSH key not in the server's authorized_keys\n"
            f"    Fix: ssh-copy-id -i {args.key} {args.user}@{args.host}\n"
            f"  - Server not running / not reachable from your network\n"
        )
        return 1
    except subprocess.TimeoutExpired:
        sys.stderr.write(
            f"\n❌ SSH connection to {args.user}@{args.host}:{args.port} timed out after 5 min.\n"
            f"The server may be unreachable or sshd is severely overloaded.\n"
        )
        return 1

    try:
        return _main_after_connect(args, remote)
    finally:
        remote.close()


def _main_after_connect(args: argparse.Namespace, remote: "Remote") -> int:
    print("==> Verifying remote shell")
    ping = remote.run("echo connected && uname -a && head -3 /etc/os-release")
    print(ping.stdout)

    if args.check_only:
        os_info = detect_os(remote)
        print(
            f"Detected OS: {os_info.get('ID')} "
            f"{os_info.get('VERSION_CODENAME') or os_info.get('VERSION_ID')}"
        )
        print(
            f"Would install Docker: {'no (--skip-docker)' if args.skip_docker else 'yes'}"
        )
        print(f"Would configure Traefik with cert mode: {args.cert_mode}")
        print(f"Would use app dir: {args.app_dir}")
        print("(check-only: no changes made)")
        return 0

    os_info = detect_os(remote)
    print(
        f"==> Detected OS: {os_info.get('ID')} "
        f"{os_info.get('VERSION_CODENAME') or os_info.get('VERSION_ID')}"
    )

    if args.create_deploy_user:
        print(f"==> Bootstrapping deploy user {args.deploy_user!r} on the server")
        create_deploy_user(remote, args)

    if not args.skip_docker:
        print("==> Installing Docker")
        install_docker(remote, os_info)

    cfg_dir = f"{args.app_dir}/traefik"

    email: str | None = None
    if args.skip_traefik:
        print(
            "==> --skip-traefik: not touching Traefik; only ensuring the `traefik` docker network exists"
        )
        ensure_external_traefik_network(remote)
    else:
        print(
            f"==> Writing Traefik config to {args.app_dir}/traefik (mode={args.cert_mode})"
        )
        # Resolve ACME email: --email flag wins; otherwise try to reuse from the
        # existing config (idempotent re-runs). Refuse if first-install and no
        # email is given anywhere — LE requires an account to issue certs.
        email = args.email
        if args.cert_mode == "acme-http01" and not email and not args.force:
            existing = read_existing_email(remote, cfg_dir)
            if existing:
                email = existing
                print(f"==> Reusing ACME email from existing config: {email}")

        if args.cert_mode == "acme-http01" and not email:
            print(
                "ERROR: --email is required for cert-mode=acme-http01.\n"
                "  - First install: provide --email <you@example.com>.\n"
                "  - Re-runs (without --force): omit --email to reuse the existing email.\n"
                "  - With --force: re-provide --email (the existing config is wiped).",
                file=sys.stderr,
            )
            return 2

        # Fail fast: if 80/443 are already taken, abort with a clear pointer
        # to --skip-traefik instead of crashing inside `docker run`.
        if not check_port_conflict(remote):
            return 3

        cfg_body = render_traefik_config(args.cert_mode, email)
        print(f"==> Reconciling preview-traefik at {cfg_dir} (mode={args.cert_mode})")
        ensure_traefik(remote, args.app_dir, cfg_body, force=args.force)

    if args.key:
        print("==> Ensuring public key is in authorized_keys")
        install_authorized_key(remote, args.key)

    deploy_key_path: Path | None = None
    if args.generate_deploy_key:
        print("==> Generating fresh ed25519 deploy key on the server")
        deploy_key_path = generate_deploy_key(remote, args)
        os.environ["PREVIEW_DEPLOY_KEY_PATH"] = str(deploy_key_path)

    # Determine gh namespace once (used by both apply and print paths).
    gh_namespace = "<your gh username or org>"
    repo = _gh_repo_slug()
    if repo:
        gh_namespace = repo.split("/", 1)[0]

    # ---- post-provisioning summary ----
    print_next_steps_header(args, args.app_dir)
    print_dns_instructions(args)  # always; can't be automated

    if args.apply_secrets:
        if not apply_secrets(args):
            print("(Falling back to copy-paste instructions below.)")
            print_secrets_instructions(args)
    else:
        print_secrets_instructions(args)

    if args.apply_config:
        if not apply_config(args, gh_namespace):
            print("(Falling back to copy-paste instructions below.)")
            print_config_instructions(args, gh_namespace)
    else:
        print_config_instructions(args, gh_namespace)

    print_post_apply(args, email)
    return 0


if __name__ == "__main__":
    sys.exit(main())
