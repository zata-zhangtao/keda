"""Post-provisioning output and interactive GitHub/Config apply helpers."""

from __future__ import annotations

import argparse
import difflib
import getpass
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from _remote import _confirm


# ---------- print next-steps ----------


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
        print("  - Traefik running as container `preview-traefik` on the `traefik` network")
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
        print(f"     gh secret set SERVER_SSH_KEY     --env preview < {shlex.quote(args.key)}")
    print('     gh secret set REGISTRY_USERNAME  --env preview --body "<your gh username>"')
    print('     gh secret set REGISTRY_PASSWORD  --env preview --body "<ghp_...>"')
    print('     gh secret set POSTGRES_PASSWORD  --env preview --body "<strong random>"')
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
    print("   \U0001f7e2 sticky comment with the preview URL once the build finishes.")
    # The Let's Encrypt warnings only apply when *we* manage Traefik. With
    # --skip-traefik the user's own Traefik (or upstream) owns cert issuance.
    if not args.skip_traefik and args.cert_mode == "acme-http01":
        print()
        print("⚠  Cert mode reminder: Let's Encrypt limits ~50 certs/registered-domain/week.")
        print("   If you expect more active PRs, plan a DNS-01 wildcard switch.")
        if not email:
            print()
            print(
                "ℹ  No ACME email is registered (you omitted --email and no existing config was found)."
            )
            print("   LE will still issue certs, but you won't get expiry notifications.")
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
    print("\U0001f4e6  Apply GitHub Secrets")
    print("=" * 72)

    if not shutil.which("gh"):
        print("ERROR: gh CLI not installed (https://cli.github.com/).", file=sys.stderr)
        return False
    if not _gh_auth_ok():
        print("ERROR: gh not authenticated. Run `gh auth login` first.", file=sys.stderr)
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
        if not _confirm(f"  `preview` env does not exist in {repo}. Create it?", default_yes=True):
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
    print("\U0001f4dd  Apply config.toml")
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
