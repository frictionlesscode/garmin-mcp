FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV GARMIN_TOKEN_DIR=/data/garth \
    SQLITE_PATH=/data/garmindb/DBs/garmin.db \
    TZ=America/New_York \
    LOG_LEVEL=INFO \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "garmin_mcp.server"]
