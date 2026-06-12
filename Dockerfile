# Single Python image: the neonize bridge (whatsmeow under the hood) and the
# MCP server run together in one process. No Go toolchain, no second service.
FROM python:3.11-slim-bookworm AS runtime

# ffmpeg     - audio transcoding for voice messages (neonize uses it)
# libmagic1  - file-type detection required by neonize (python-magic)
# ca-certificates - TLS to WhatsApp
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libmagic1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# uv (Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/whatsapp-mcp-server
COPY whatsapp-mcp-server/ ./
RUN uv sync --frozen --no-dev

# WhatsApp session + message DB + downloaded media live here.
VOLUME ["/app/whatsapp-mcp-server/store"]

# 8080 = login control API; 8000 = MCP streamable-http endpoint.
EXPOSE 8080 8000

ENV MCP_TRANSPORT=streamable-http \
    FASTMCP_HOST=0.0.0.0 \
    FASTMCP_PORT=8000 \
    WHATSAPP_STORE_DIR=/app/whatsapp-mcp-server/store

CMD ["uv", "run", "--no-dev", "main.py"]
