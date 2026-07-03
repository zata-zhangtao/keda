"""SSH remote connection with ControlMaster multiplexing."""

from __future__ import annotations

import argparse
import os
import secrets
import shlex
import subprocess
import sys
from pathlib import Path


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
        cp = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=300)
        if cp.returncode != 0:
            if cp.stderr:
                sys.stderr.write(f"--- ssh master stderr ---\n{cp.stderr}--- end stderr ---\n")
            raise subprocess.CalledProcessError(cp.returncode, cmd, cp.stdout, cp.stderr)

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
                sys.stderr.write(f"--- remote stderr ---\n{cp.stderr}--- end stderr ---\n")
            raise subprocess.CalledProcessError(cp.returncode, cmd, cp.stdout, cp.stderr)
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
