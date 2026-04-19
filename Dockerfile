FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY arc_raiders_mcp/ arc_raiders_mcp/

CMD ["arc-raiders-mcp"]
