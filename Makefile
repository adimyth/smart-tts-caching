PY := .venv/bin/python
SERVICE ?= all

.PHONY: sync lint format test test-redis harness redis-up redis-down

sync:
	uv sync

lint:
	$(PY) -m ruff check packages
	$(PY) -m ruff format --check packages
	$(PY) -m pyright

format:
	$(PY) -m ruff format packages
	$(PY) -m ruff check --fix packages

# Unit tests. Tests marked `redis` skip unless TTS_CACHE_REDIS_URL is set.
test:
	$(PY) -m pytest -q

# Unit tests plus the Redis suite against the dedicated instance on port 6380.
test-redis:
	TTS_CACHE_REDIS_URL=$${TTS_CACHE_REDIS_URL:-redis://localhost:6380} $(PY) -m pytest -q

redis-up:
	redis-server --port 6380 --maxmemory 256mb --maxmemory-policy allkeys-lfu --save "" --daemonize yes

redis-down:
	redis-cli -p 6380 shutdown nosave || true

# Live provider tests (needs .env). Not run in CI.
harness:
	$(PY) harness/attribution.py $(SERVICE) --quiet
