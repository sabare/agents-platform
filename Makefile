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

.PHONY: observability-up observability-down trace-demo
observability-up:
	python scripts/setup_observability.py
	docker compose --env-file .env.observability -f compose.observability.yaml up -d

observability-down:
	docker compose --env-file .env.observability -f compose.observability.yaml down

trace-demo:
	python examples/observability_demo.py

.PHONY: hospital-demo
hospital-demo:
	python examples/hospital_response_demo.py

.PHONY: communication-demo
communication-demo:
	python examples/communication_demo.py
