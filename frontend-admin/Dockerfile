# ------------------------------------------------------------------------------
# Frontend — Admin Dashboard (Vite + React + TanStack Router)
# ------------------------------------------------------------------------------
# Build context: ./frontend-admin
# Production runtime: nginx
# ------------------------------------------------------------------------------

# ── Build stage ───────────────────────────────────────────────────────────────
FROM node:22-alpine AS builder

WORKDIR /app

# Run pnpm in non-interactive CI mode
ENV CI=true

# Install pnpm
RUN npm install -g pnpm

# Install dependencies first to leverage Docker cache
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# Inject the build-time version string (defaults to "dev" for local builds).
ARG VITE_APP_VERSION=dev
ENV VITE_APP_VERSION=${VITE_APP_VERSION}

# Copy source and build
COPY . .
RUN pnpm build

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

# Copy built static assets
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
