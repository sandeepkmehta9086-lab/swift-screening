.PHONY: install lint type test test-fast perf bench openapi scenarios demo up check e2e corpus

install:
	uv sync
lint:
	uv run ruff check src tests tools && uv run ruff format --check src tests tools
type:
	uv run mypy src
test:
	uv run pytest --ignore=tests/perf --cov=swift_screening --cov-fail-under=90
test-fast:
	uv run pytest tests/unit -q
perf:
	uv run pytest tests/perf -m perf -s
openapi:
	uv run python tools/export_openapi.py
scenarios:
	uv run python tools/scenario_coverage.py
demo:
	uv run swift-screening demo
up:
	docker compose up --build
check: lint type test perf
corpus:
	uv run python tools/generate_corpus.py
# Full stack test: build, start API + PostgreSQL, run e2e tests, verify audit chain, stop.
# Host port: make e2e SWIFT_SCREENING_PORT=18080   (if 8080 is taken)
SWIFT_SCREENING_PORT ?= 8080
export SWIFT_SCREENING_PORT
e2e:
	docker compose up -d --build --wait
	SWIFT_SCREENING_E2E_URL=http://127.0.0.1:$(SWIFT_SCREENING_PORT) uv run pytest tests/e2e -v
	docker compose exec -T api swift-screening audit verify-chain
	docker compose down
