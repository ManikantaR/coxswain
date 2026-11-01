# coxd — the coxswain v3.5 supervisor, packaged to run 24/7 on the NAS (DESIGN-V35 D20).
#
# It rides the Claude PRO subscription via the `claude` CLI (the engine the Agent SDK
# drives) — NOT an API key. Auth is a one-time `claude login` into the mounted creds
# volume (device-code flow); see deploy/README.md. The NAS host has only Docker, so
# everything coxd needs (git, gh, node, the claude CLI, python deps) lives in here.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg tini tzdata \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Docker CLI + compose plugin (CLIENT only — no daemon in here). coxd drives the NAS
# host's Docker via the mounted /var/run/docker.sock to build+recreate a repo's
# containers when you tap Deploy on the board. See deploy/docker-compose.coxd.yml.
RUN install -m 0755 -d /usr/share/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /usr/share/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
        > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI (node) that the Agent SDK shells out to, + coxd's python deps.
# PINNED (factory Phase 0): an unattended factory must not change engine under itself
# overnight. The CLI self-updated 2.1.197 → 2.1.316 inside the running container and
# the SDK floated to whatever pip resolved. These are the versions verified working on
# the NAS 2026-10-31; bump deliberately (build-arg) and re-run the proving task.
ARG CLAUDE_CODE_VERSION=2.1.316
ARG CLAUDE_AGENT_SDK_VERSION=0.2.174
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && pip install --no-cache-dir "claude-agent-sdk==${CLAUDE_AGENT_SDK_VERSION}" uvicorn starlette
# Stop the CLI self-updating at runtime (that is how the pin drifted before).
ENV DISABLE_AUTOUPDATER=1

# git → GitHub auth through gh (which reads GH_TOKEN). In /etc/gitconfig (system
# level) so it is part of the IMAGE: `gh auth setup-git` writes /root/.gitconfig,
# which is outside every volume and was wiped by the 2026-10-24 rebuild — after which
# fetch failed silently (stale base) and every push would have been rejected.
RUN git config --system credential.https://github.com.helper '' \
 && git config --system --add credential.https://github.com.helper '!/usr/bin/gh auth git-credential'

# Shared git object store for the root control plane + the worker user (see
# deploy/entrypoint.sh): new objects/refs stay group-writable, root trusts the
# worker-owned worktrees, and auto-gc never repacks under a running worker.
RUN git config --system core.sharedRepository group \
 && git config --system --add safe.directory '*' \
 && git config --system gc.auto 0

# Worker user (coxd/sandbox.py): the claude CLI, its tools, the gate and `pnpm install`
# run as this uid via setpriv (util-linux, in the base image). It cannot read the
# store, /root, or root's /proc/<pid>/environ (GH_TOKEN, COXD_TG_TOKEN).
RUN useradd --uid 10001 --create-home --shell /bin/sh coxw
ENV COXD_WORKER_USER=coxw
COPY deploy/coxd-claude /usr/local/bin/coxd-claude
COPY deploy/entrypoint.sh /usr/local/bin/coxd-entrypoint

# The claude CLI keeps its account/onboarding state in ~/.claude.json, which sits
# OUTSIDE the config dir — so every rebuild "logged it out". Point the whole config
# dir at the claude-creds volume, now in the worker's home (the worker runs claude).
ENV CLAUDE_CONFIG_DIR=/home/coxw/.claude

# pnpm for pnpm-monorepo targets (MoneyPulse, aura-tutor). Without it every worker
# dispatch died at provisioning with `pnpm: not found`. corepack ships with node; the
# pinned default matches MoneyPulse's `packageManager` field, and corepack will fetch a
# different pin on demand for other repos (no interactive prompt — we're headless).
ARG PNPM_VERSION=10.32.1
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable \
 && corepack prepare pnpm@${PNPM_VERSION} --activate \
 && pnpm --version \
 && su coxw -c 'corepack prepare pnpm@'"${PNPM_VERSION}"' --activate && pnpm --version' \
 && command -v setpriv

WORKDIR /app
COPY coxd /app/coxd

# COXD_HOME (sqlite store + repo registry) and COXD_REPO_ROOT (target repos) are volumes.
ENV COXD_HOME=/data/coxswain COXD_REPO_ROOT=/repo
# Unbuffered stdout so `docker logs coxd` actually shows the serve banner, cleanup
# errors and streamed deploy output (it was empty: block-buffered, never flushed).
ENV PYTHONUNBUFFERED=1
EXPOSE 8791
# Concurrency 1: one Claude subscription — parallel workers drain the 5-hour window
# ~N× faster and collide in shared files (factory Phase 0).
# cli.py uses flat imports → its own dir is on sys.path[0]; run it by path.
ENTRYPOINT ["tini", "--", "/usr/local/bin/coxd-entrypoint"]
CMD ["python", "/app/coxd/cli.py", "serve", "--port", "8791", "--concurrency", "1"]
