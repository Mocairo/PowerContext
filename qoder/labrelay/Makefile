.PHONY: seekdb-up seekdb-logs seekdb-down backend-install backend-test backend-dev frontend-install frontend-build frontend-dev config-check

seekdb-up:
	docker compose --env-file .env up -d seekdb

seekdb-logs:
	docker compose --env-file .env logs -f seekdb

seekdb-down:
	docker compose --env-file .env down

backend-install:
	uv sync --directory backend --extra dev

backend-test:
	uv run --directory backend pytest

backend-dev:
	uv run --directory backend uvicorn labrelay.api.main:create_default_app --factory --reload --host 127.0.0.1 --port 8010

frontend-install:
	npm --prefix frontend install

frontend-build:
	npm --prefix frontend run build

frontend-dev:
	npm --prefix frontend run dev

config-check:
	@for file in configs/app.yaml configs/retrieval.yaml configs/planner.yaml configs/security.yaml configs/logging.yaml .env.example docker-compose.yml backend/pyproject.toml frontend/package.json; do \
		test -f "$$file" || { printf 'missing config: %s\n' "$$file"; exit 1; }; \
	done
