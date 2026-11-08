# Deployer: the only container with docker.sock. Stdlib-only Python + docker CLI/compose.
# Build context is the coxswain repo root.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io docker-compose-v2 tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN docker compose version
COPY deploy/deployer/server.py /app/server.py
ENTRYPOINT ["tini", "--"]
CMD ["python", "/app/server.py"]
