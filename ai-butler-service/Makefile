COMPOSE = docker compose --env-file .env.local -f compose.dev.yml
RUN = $(if $(wildcard .venv/bin/python),.venv/bin/,uv run )

.PHONY: doctor bootstrap infra-up infra-down infra-up-knowledge stack-up stack-down migrate langgraph-setup knowledge-setup seed reset-users reset-verification-data verification-rebuild dev-api dev-worker dev-scheduler openapi format lint typecheck test smoke eval-smoke eval eval-live docs-check secrets ci

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
	$(RUN)alembic upgrade head
	$(RUN)python -m ai_butler.scripts.setup_langgraph

langgraph-setup:
	$(RUN)python -m ai_butler.scripts.setup_langgraph

knowledge-setup:
	$(RUN)python -m ai_butler.scripts.setup_knowledge

seed:
	$(RUN)butler-seed

reset-users:
	$(RUN)butler-reset-users --confirm-delete-all-users

reset-verification-data:
	@test "$(CONFIRM)" = "DELETE_ALL_VERIFICATION_DATA" || (echo "refusing reset: pass CONFIRM=DELETE_ALL_VERIFICATION_DATA"; exit 2)
	@case "$${APP_ENV:-development}" in development|test) ;; *) echo "refusing reset outside development/test"; exit 2 ;; esac
	$(COMPOSE) stop api agent-worker scheduler-worker
	$(COMPOSE) down --volumes --remove-orphans

verification-rebuild: reset-verification-data
	$(COMPOSE) up -d --wait postgres qdrant
	$(COMPOSE) build api
	$(COMPOSE) run --rm api alembic upgrade head
	$(COMPOSE) run --rm api python -m ai_butler.scripts.setup_langgraph
	$(COMPOSE) run --rm api python -m ai_butler.scripts.setup_knowledge
	$(COMPOSE) run --rm api butler-seed

dev-api:
	$(RUN)butler-api

dev-worker:
	$(RUN)butler-agent-worker

dev-scheduler:
	$(RUN)butler-scheduler-worker

openapi:
	$(RUN)python scripts/generate_openapi.py

format:
	$(RUN)ruff format src tests scripts

lint:
	$(RUN)ruff format --check src tests scripts
	$(RUN)ruff check src tests scripts
	$(RUN)python scripts/check_source_lines.py --max-lines 500 src

typecheck:
	$(RUN)mypy

test:
	$(RUN)pytest

smoke:
	$(RUN)pytest tests/smoke

eval-smoke:
	@DEEPEVAL_DISABLE_DOTENV=1 DEEPEVAL_TELEMETRY_OPT_OUT=1 \
		$(RUN)pytest tests/evals -m eval_smoke --no-cov

eval:
	@DEEPEVAL_DISABLE_DOTENV=1 DEEPEVAL_TELEMETRY_OPT_OUT=1 \
		$(RUN)pytest tests/evals -m 'eval_core or eval_security' --no-cov

eval-live:
	@DEEPEVAL_DISABLE_DOTENV=1 DEEPEVAL_TELEMETRY_OPT_OUT=1 \
		$(RUN)butler-eval-live --suite live --trials 3

docs-check:
	$(RUN)python scripts/check_markdown_links.py

secrets:
	@git ls-files -z | xargs -0 $(RUN)detect-secrets-hook --no-verify \
		--exclude-files '(^|/)uv\.lock$$|(^|/)openapi\.json$$' \
		--exclude-lines 'butler_(app|test|migrator)|POSTGRES_PASSWORD'

ci: lint typecheck test eval-smoke docs-check secrets
	@echo "backend checks passed"
