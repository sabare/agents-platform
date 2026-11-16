FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120
WORKDIR /app

# Keep tested runtime dependencies cached when only application code changes.
COPY pyproject.toml constraints-runtime.txt ./
RUN python -c 'import pathlib,tomllib; p=tomllib.loads(pathlib.Path("pyproject.toml").read_text()); pathlib.Path("/tmp/runtime-requirements.txt").write_text("\n".join(p["project"]["dependencies"]))' \
    && pip install --no-cache-dir -c constraints-runtime.txt -r /tmp/runtime-requirements.txt

# Token counting must work without network access during agent execution.
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache
RUN python -c 'import tiktoken; tiktoken.get_encoding("o200k_base"); tiktoken.get_encoding("cl100k_base")'

ENV PYTHONPATH=/app/src

COPY README.md ./
COPY src ./src

COPY alembic.ini ./
COPY migrations ./migrations

CMD ["uvicorn", "agents_platform.api:app", "--host", "0.0.0.0", "--port", "8000"]
