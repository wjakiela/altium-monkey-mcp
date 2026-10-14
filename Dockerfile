# Stdio MCP server; mount your Altium projects, e.g.:
#   docker run -i --rm -v "$PWD:/work" altium-monkey-mcp
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE altium_monkey_mcp.py ./
RUN pip install --no-cache-dir .

WORKDIR /work
ENTRYPOINT ["altium-monkey-mcp"]
