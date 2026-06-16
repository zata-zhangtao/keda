# VPS Traefik Deployment

This directory contains containerized deployment templates for the keda project.

- `docker-compose.dokploy.yml` (repository root) — production deployment managed by Dokploy.
- `docker-compose.preview.yml` — per-PR preview stack deployed by GitHub Actions.
- `preview.env.example` — non-sensitive preview environment template.
- `deploy-preview.sh` — non-interactive preview deploy/teardown helper.

## Preview Deployment (per-PR)

A new preview stack is automatically created or updated when a pull request is
opened, synchronized, or reopened. The stack is removed when the PR is closed.

### Trigger events

- `pull_request` types: `opened`, `synchronize`, `reopened`, `closed`
- Comment `/deploy` on a PR
- Manual `workflow_dispatch` with the PR number

### Required GitHub Secrets / Variables

Sensitive values must be provided through GitHub Secrets (or the repository's
CI/CD secret manager) and are never committed to this repository.

| Secret / Variable | Purpose |
| --- | --- |
| `SERVER_HOST` | Preview server hostname or IP |
| `SERVER_USER` | SSH user on the preview server |
| `SERVER_SSH_KEY` | Private SSH key for deployment |
| `REGISTRY_USERNAME` | Container registry username |
| `REGISTRY_PASSWORD` | Container registry password/token |
| `POSTGRES_PASSWORD` | Ephemeral preview database password |

Non-sensitive structure is configured in `config.toml [preview]`:

- `enabled`
- `base_domain`
- `project_slug`
- `app_dir_root`
- `registry_host`
- `registry_namespace`
- `traefik_network`
- `url_scheme`
- `subdomain_template`
- `compose_template`

### How it works

1. `.github/workflows/deploy-preview.yml` resolves the PR number and head SHA.
2. `scripts/preview_env.py` derives non-sensitive values (domain, compose project
   name, image references) from `config.toml [preview]`.
3. Backend and frontend images are built and pushed with the short SHA tag.
4. The compose file and deploy helper are copied to the preview server via SSH.
5. `deploy-preview.sh up` writes the runtime `.env`, pulls images, and starts the
   ephemeral stack.
6. A sticky PR comment with the preview URL is created or updated.

### Failure behavior

Preview deployment runs in an independent workflow and is **not** a required
status check. Failures are reported in the sticky PR comment but do not block
review or merge.

### Local smoke test

Build local images and render the compose file without a remote server:

```bash
# Build images
docker build -t keda-backend:local -f src/backend/Dockerfile .
docker build -t keda-frontend:local -f frontend/Dockerfile ./frontend

# Generate a temporary env file
uv run python scripts/preview_env.py --pr 0 --sha local > /tmp/preview.env
sed -i '' 's|:local|:local|' /tmp/preview.env  # optional: force local tag

# Validate compose rendering
docker compose -f deploy/vps-traefik/docker-compose.preview.yml \
  --env-file /tmp/preview.env config

# Start the stack locally (requires a published port for external access)
docker compose -p keda-pr-0 -f deploy/vps-traefik/docker-compose.preview.yml \
  --env-file /tmp/preview.env up -d
```

The preview stack exposes the frontend on port 80 inside the container network.
For local access, add a published port mapping or use `docker compose exec`.

### Manual teardown

On a server with `APP_DIR` and `COMPOSE_PROJECT_NAME` set:

```bash
bash deploy-preview.sh down
```
