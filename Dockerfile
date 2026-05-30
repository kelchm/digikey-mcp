# syntax=docker/dockerfile:1
#
# DigiKey MCP server — HTTP-transport image for sidecar / containerized deployments.
# Defaults to FastMCP's HTTP transport on 0.0.0.0:8000 so the port is reachable
# from outside the container; for stdio-over-pipe usage (mcpjungle, child-process
# MCP clients) set DIGIKEY_MCP_TRANSPORT=stdio at run time.

FROM python:3.12-slim

# Non-root user. uid 1000 matches the typical first-user mapping on Linux hosts,
# so a bind-mounted /data volume is writable without an explicit chown.
RUN useradd -u 1000 -m -d /home/digikey digikey

WORKDIR /app

# Copy only what's needed for `pip install .` — keeps the image lean and means
# code changes don't bust the dep-install layer.
COPY pyproject.toml ./
COPY digikey_mcp_server.py digikey_mcp_auth.py ./

RUN pip install --no-cache-dir .

# Token cache lives here. Operators mount a writable volume; without one the
# server falls back to in-memory tokens and warns (and restart requires a
# fresh DIGIKEY_REFRESH_TOKEN_SEED).
RUN mkdir -p /data && chown digikey:digikey /data
VOLUME ["/data"]

ENV DIGIKEY_TOKEN_CACHE=/data/tokens.json \
    DIGIKEY_MCP_TRANSPORT=http \
    DIGIKEY_MCP_HOST=0.0.0.0 \
    DIGIKEY_MCP_PORT=8000 \
    PYTHONUNBUFFERED=1

# Pin the numeric UID rather than the username so Kubernetes' runAsNonRoot pod
# admission check (which runs before the container starts and can't resolve
# /etc/passwd) accepts the image without requiring runAsUser: 1000 on the pod.
USER 1000
EXPOSE 8000
ENTRYPOINT ["digikey-mcp"]
