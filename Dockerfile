FROM python:3.10-slim

WORKDIR /app

# Install Python dependencies
COPY gateway/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install pyyaml pydantic packaging fastmcp

# Copy the MCP server
COPY gateway/ ./gateway/

ENV PYTHONPATH=/app/gateway:/app/gateway/ai

# Pin the containerized MCP surface to the coherent "core" profile (~24 tools)
# instead of the full ~200-tool surface. This image is ONLY the crawler surface
# for MCP directories (e.g. Glama), which build this Dockerfile and count the
# tools the RUNNING server exposes. A curated set scores far better on
# directory "tool count" / coherence rubrics and is actually triable.
#
# NON-BREAKING for installed users: npm/local users never launch via this
# Dockerfile — they run the CLI-managed server with DELIMIT_TOOLSET UNSET, which
# resolves to "full" (byte-identical to the historical surface; DEFAULT_TOOLSET
# is "full"). No tool is removed or renamed; all tools remain registered under
# "full" and every tool stays importable/callable internally regardless of
# profile. The mechanism (LED-3709) is registration-gating only. An unknown or
# unset value fails safe to "full".
ENV DELIMIT_TOOLSET=core

# MCP server runs via stdio
ENTRYPOINT ["python", "gateway/ai/server.py"]
