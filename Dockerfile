FROM python:3.10-slim

WORKDIR /app

# Install Python dependencies
COPY gateway/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install pyyaml pydantic packaging fastmcp

# Copy the MCP server
COPY gateway/ ./gateway/

ENV PYTHONPATH=/app/gateway:/app/gateway/ai

# MCP server runs via stdio
ENTRYPOINT ["python", "gateway/ai/server.py"]
