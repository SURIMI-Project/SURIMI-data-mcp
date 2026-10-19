FROM python:3.12-slim

LABEL org.opencontainers.image.title="SURIMI-mcp"
LABEL org.opencontainers.image.description="SURIMI-data-mcp, the NLP assessment model of SURIMI (D3.7). An MCP server with 20 tools that answer questions about the SURIMI data lake, over Streamable HTTP (/mcp) and SSE (/sse) for any MCP client. The server calculates each number from the data and signs its calculation record with HMAC. The image holds code only: the data and documents are downloaded from EDITO object storage when the server is deployed, and the DuckDB cube is built from them."
LABEL org.opencontainers.image.source="https://github.com/SURIMI-Project/SURIMI-data-mcp"
LABEL org.opencontainers.image.url="https://www.surimi-project.eu/"
LABEL org.opencontainers.image.licenses="EUPL-1.2"
LABEL org.opencontainers.image.vendor="SURIMI Project"
LABEL org.opencontainers.image.documentation="https://www.surimi-project.eu/"

WORKDIR /app

# Unbuffered output lets kubectl logs show progress while the server and the bootstrap run.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# The next RUN installs curl and the Python dependencies. No data is fetched at build time.
#   * duckdb: the engine behind the cube (engine/cube.py and engine/aggregate.py).
#   * boto3: downloads the data and docs from MinIO or S3 (loader/bootstrap.py, _download_s3_prefix).
#   * sqlglot: parses SQL in provenance/verification.py and engine/sql_tools.py.
#   * PyJWT[crypto]: checks the Keycloak token on /sources/lake (web/lake_auth.py).
#   * pyyaml: reads the dataset catalogue (datasets/catalog.py).
#   * markdown: renders the supporting files at /sources/repo for signed-in readers (web/web_auth.py).
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        fastmcp==3.3.1 trino psycopg2-binary numpy duckdb boto3 "sqlglot>=26.0" "PyJWT[crypto]>=2.8" "markdown>=3.5" "pyyaml>=6.0"

# Code: the surimi_data_mcp package, the schema DDL, the vocabularies, the web pages and the scripts.
COPY surimi_data_mcp/ surimi_data_mcp/
COPY data/init/ data/init/
COPY data/vocabularies/ data/vocabularies/
COPY static/ static/
COPY scripts/ /app/scripts/
# Supporting files the deliverables cite, served to signed-in readers at /sources/repo.
COPY audit-repro/ /app/audit-repro/

# The image holds no data. The bootstrap reads the data and docs from MinIO at deploy,
# and scripts/image_smoke_test.sh checks that /app/data/exports contains no CSVs.

EXPOSE 26610

CMD ["python", "-m", "surimi_data_mcp"]
