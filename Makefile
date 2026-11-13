.PHONY: install up down logs migrate seed api worker check test smoke

install:
	python -m pip install -e '.[dev]'

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

migrate:
	alembic upgrade head

seed:
	python -m agents_platform.seed

api:
	uvicorn agents_platform.api:app --reload --host 127.0.0.1 --port 8000

worker:
	agents-worker

check:
	ruff check .
	ruff format --check .
	agents-platform --check

test:
	pytest -m 'not integration'

smoke:
	RUN_INTEGRATION_TESTS=1 pytest -m integration
