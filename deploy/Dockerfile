# coxd — the coxswain v3.5 supervisor, packaged to run 24/7 on the NAS (DESIGN-V35 D20).
#
# It rides the Claude PRO subscription via the `claude` CLI (the engine the Agent SDK
# drives) — NOT an API key. Auth is a one-time `claude login` into the mounted creds
# volume (device-code flow); see deploy/README.md. The NAS host has only Docker, so
# everything coxd needs (git, gh, node, the claude CLI, python deps) lives in here.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg tini \
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
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI (node) that the Agent SDK shells out to, + coxd's python deps.
RUN npm install -g @anthropic-ai/claude-code \
 && pip install --no-cache-dir "claude-agent-sdk>=0.2.119" uvicorn starlette

WORKDIR /app
COPY coxd /app/coxd

# COXD_HOME (sqlite store + repo registry) and COXD_REPO_ROOT (target repos) are volumes.
ENV COXD_HOME=/data/coxswain COXD_REPO_ROOT=/repo
EXPOSE 8791
# cli.py uses flat imports → its own dir is on sys.path[0]; run it by path.
ENTRYPOINT ["tini", "--"]
CMD ["python", "/app/coxd/cli.py", "serve", "--port", "8791", "--concurrency", "2"]
