FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120
WORKDIR /app

# Keep tested runtime dependencies cached when only application code changes.
COPY pyproject.toml constraints-runtime.txt ./
RUN python -c 'import pathlib,tomllib; p=tomllib.loads(pathlib.Path("pyproject.toml").read_text()); pathlib.Path("/tmp/runtime-requirements.txt").write_text("\n".join(p["project"]["dependencies"]))' \
    && pip install --no-cache-dir -c constraints-runtime.txt -r /tmp/runtime-requirements.txt

COPY README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

COPY alembic.ini ./
COPY migrations ./migrations

CMD ["uvicorn", "agents_platform.api:app", "--host", "0.0.0.0", "--port", "8000"]
