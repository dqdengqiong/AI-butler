COMPOSE = docker compose --env-file .env.local -f compose.dev.yml

.PHONY: doctor bootstrap infra-up infra-down infra-up-knowledge stack-up stack-down migrate langgraph-setup seed dev-api dev-worker dev-scheduler openapi format lint typecheck test smoke eval-smoke eval eval-live docs-check secrets ci

doctor:
	@./scripts/doctor.sh

bootstrap:
	@./scripts/bootstrap.sh

infra-up:
	$(COMPOSE) up -d postgres

infra-up-knowledge:
	$(COMPOSE) --profile knowledge up -d postgres qdrant

stack-up:
	$(COMPOSE) up -d --build
	$(COMPOSE) exec -T api alembic upgrade head
	$(COMPOSE) exec -T api python -m ai_butler.scripts.setup_langgraph
	$(COMPOSE) exec -T api butler-seed

stack-down:
	$(COMPOSE) down

infra-down:
	$(COMPOSE) down

migrate:
	uv run alembic upgrade head
	uv run python -m ai_butler.scripts.setup_langgraph

langgraph-setup:
	uv run python -m ai_butler.scripts.setup_langgraph

seed:
	uv run butler-seed

dev-api:
	uv run fastapi dev

dev-worker:
	uv run butler-agent-worker

dev-scheduler:
	uv run butler-scheduler-worker

openapi:
	uv run python scripts/generate_openapi.py

format:
	uv run ruff format src tests scripts

lint:
	uv run ruff format --check src tests scripts
	uv run ruff check src tests scripts

typecheck:
	uv run mypy

test:
	uv run pytest

smoke:
	uv run pytest tests/smoke

eval-smoke:
	@DEEPEVAL_DISABLE_DOTENV=1 DEEPEVAL_TELEMETRY_OPT_OUT=1 \
		uv run pytest tests/evals -m eval_smoke --no-cov

eval:
	@DEEPEVAL_DISABLE_DOTENV=1 DEEPEVAL_TELEMETRY_OPT_OUT=1 \
		uv run pytest tests/evals -m 'eval_core or eval_security' --no-cov

eval-live:
	@DEEPEVAL_DISABLE_DOTENV=1 DEEPEVAL_TELEMETRY_OPT_OUT=1 \
		uv run butler-eval-live --suite live --trials 3

docs-check:
	uv run python scripts/check_markdown_links.py

secrets:
	@git ls-files -z | xargs -0 uv run detect-secrets-hook --no-verify \
		--exclude-files '(^|/)uv\.lock$$|(^|/)openapi\.json$$' \
		--exclude-lines 'butler_(app|test|migrator)|POSTGRES_PASSWORD'

ci: lint typecheck test eval-smoke docs-check secrets
	@echo "backend checks passed"
