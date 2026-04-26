#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."   # server/

# Ensure .env exists (copy from .env.example if absent)
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "==> Created .env from .env.example"
  else
    echo "ERROR: no .env or .env.example found" >&2
    exit 1
  fi
fi

# 1. Bring up infra (idempotent)
echo "==> Starting infrastructure..."
docker compose up -d postgres minio minio-init

# Wait for postgres to be healthy before proceeding
echo "==> Waiting for Postgres to be ready..."
for i in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U harness -d harness >/dev/null 2>&1; then
    echo "Postgres ready"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: Postgres did not become ready in time" >&2
    exit 1
  fi
  sleep 1
done

# 2. Apply migrations
echo "==> Applying migrations..."
uv run alembic upgrade head

# 3. Seed templates
echo "==> Seeding templates..."
uv run python -m seed.templates

# 4. Start server in background, capture PID
echo "==> Starting API server..."
uv run uvicorn api.main:app --port 8000 &
SERVER_PID=$!
trap "echo '==> Stopping server...'; kill $SERVER_PID 2>/dev/null; exit" EXIT INT TERM

# 5. Wait for /healthz
echo "==> Waiting for server to be ready..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/healthz >/dev/null; then
    echo "Server ready"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "ERROR: Server did not become ready in time" >&2
    exit 1
  fi
  sleep 1
done

# 6. Run test_client.py with --no-wait so we don't trigger a real benchmark
echo "==> Running test_client.py with --no-wait..."
uv run python test_client.py --no-wait --max-iterations 1 --name "smoke-$(date +%s)"

# 7. Verify queue has a task
echo "==> Verifying task was enqueued..."
uv run python -c "
import asyncio
from sqlalchemy import text
from core.db import init_engine, dispose_engine, get_session

async def main():
    init_engine()
    async with get_session() as s:
        r = await s.execute(text('SELECT task_type, status FROM task_queue ORDER BY created_at DESC LIMIT 1'))
        row = r.first()
        assert row is not None, 'no task in queue'
        assert row[0] == 'benchmark.run', f'expected benchmark.run, got {row[0]}'
        print(f'queue task OK: {row[0]} status={row[1]}')
    await dispose_engine()

asyncio.run(main())
"

echo
echo "=== smoke test PASSED ==="
echo "Stack is wired correctly. Stop here unless you want to spend on actual benchmark runs."
echo "To run a full e2e job: set ANTHROPIC_API_KEY in .env, then in another terminal: 'uv run python -m worker.main'"
echo "Then re-run test_client.py without --no-wait."
