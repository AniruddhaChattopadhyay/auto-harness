# Agent Optimization Service

A FastAPI + Postgres + MinIO service that runs an inner coding agent against TerminalBench tasks, observes failures, invokes an outer LLM agent (Claude Agent SDK) to propose improvements to the inner agent's source, applies them, re-runs the benchmark, and persists the full iteration history — all over HTTP. Two-process model: HTTP server + queue-driven worker. Designed against the open-source `neosigmaai/auto-harness` reference repo.

---

## Setup and Run Instructions

### Prerequisites

- Docker + Docker Compose (for Postgres + MinIO)
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) package manager
- API keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `E2B_API_KEY`

### Fast path — idempotent smoke test

Brings up infra, migrates, seeds, starts the API, and runs `test_client.py --no-wait` to verify
the wiring without spending on actual benchmark runs.

```bash
cd server/
cp .env.example .env
# Edit .env — fill in OPENAI_API_KEY, ANTHROPIC_API_KEY, E2B_API_KEY
bash scripts/smoke.sh
```

The MinIO web console is at `http://localhost:9001` (credentials: `minioadmin` / `minioadmin`).
Postgres listens on `localhost:5432` (db/user/password: `harness` / `harness` / `harness`).

After the smoke test passes the stack is idle — no background worker was started. To run a real
iteration loop, follow the manual path below.

### Manual path — full iteration run

```bash
cd server/
cp .env.example .env      # fill in all three API keys

# 1. Local infrastructure
docker compose up -d

# 2. Python deps
uv venv --python 3.12
uv sync --no-install-project --all-extras --prerelease=allow

# 3. Migrations + seed
uv run alembic upgrade head
uv run python -m seed.templates

# 4. Terminal A — start API server
uv run uvicorn api.main:app --port 8000

# 5. Terminal B — start worker
uv run python -m worker.main

# 6. Terminal C — submit a job
uv run python test_client.py --max-iterations 3
```

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | Yes | — | Inner agent model (GPT) |
| `ANTHROPIC_API_KEY` | Yes | — | Outer agent (Claude Agent SDK) |
| `E2B_API_KEY` | Yes | — | Sandbox provider |
| `BENCHMARK_MODEL` | No | `gpt-5.4` | Inner agent model injected into sandbox |
| `OPTIMIZER_MODEL` | No | `claude-sonnet-4-6` | Outer agent model |
| `OPTIMIZER_KIND` | No | `claude_agent_sdk` | Which `OuterAgent` impl to use |
| `DATABASE_URL` | No | `postgresql+asyncpg://harness:harness@localhost:5432/harness` | Postgres DSN |
| `MINIO_ENDPOINT` | No | `localhost:9000` | MinIO address |
| `MINIO_ACCESS_KEY` | No | `minioadmin` | MinIO credentials |
| `MINIO_SECRET_KEY` | No | `minioadmin` | MinIO credentials |
| `MINIO_BUCKET` | No | `harness` | Bucket name |
| `WORKER_BENCHMARK_CONCURRENCY` | No | `2` | Parallel `benchmark.run` handlers |
| `WORKER_OPTIMIZER_CONCURRENCY` | No | `2` | Parallel `optimizer.propose` handlers |
| `WORKER_POLL_INTERVAL_SECONDS` | No | `2` | Queue poll cadence |
| `WORKER_TASK_TIMEOUT_SECONDS` | No | `1800` | Stale-running sweeper threshold |
| `LOG_LEVEL` | No | `INFO` | Log verbosity |

---

## TerminalBench Tasks Selected

The seeded task list (`template_id = terminal_bench`) covers five tasks from `terminal-bench@2.0`:

| Task ID | Category | Why included |
|---|---|---|
| `fix-git` | Git | Simple, fast, high baseline pass rate — anchors the score |
| `configure-git-webserver` | Sysadmin / web | Tests Apache + git smart-HTTP config; different failure mode from pure git tasks |
| `git-leak-recovery` | Security / git | Requires BFG or `git filter-repo`; tests the agent's knowledge of secret removal |
| `nginx-request-logging` | Sysadmin | Tests nginx config mutation; reliably quick in E2B |
| `count-dataset-tokens` | Data processing | Tokenization task; baseline agent applies a spurious domain filter — exactly the kind of diagnosable failure the optimizer should catch |

**Selection rationale:**

- **Diverse but bounded.** Covers git recovery, web server configuration, security, sysadmin, and data processing in just five tasks — broad enough to expose different failure modes, small enough for a tight feedback loop (~1–4 min total wall time in E2B with parallel sandboxes).
- **Verified baseline behavior.** These five were empirically validated against the upstream reference repo: with `gpt-5.4` as the inner agent the baseline run scored **4/5 = 0.80** in approximately 1 minute 12 seconds; only `count-dataset-tokens` failed. With `gpt-4o-mini` the same set scored 1/5. This spread gives the optimizer meaningful headroom — there is at least one task that is reliably easy and at least one that is reliably hard.
- **Diagnosable failure.** The `count-dataset-tokens` failure is specifically traceable: the agent adds a "science" domain filter that the verifier does not expect. The optimizer can read the trace, identify the spurious filter, and propose a targeted edit — exactly the signal the outer agent is designed to act on.

**What we would change with more time:** expand to 10–15 tasks per the spec. The five-task list is a "verified small" subset chosen to keep iteration cost low during development; the full 89 tasks in `terminal-bench@2.0` are too many for a tight feedback loop.

---

## Key Design Decisions

### 1. Two-level model: Experiment contains Jobs

One **experiment** owns the immutable baseline configuration, task list, optimizer settings, and rolling best-agent state. A **job** is a session of N iterations within an experiment. When a job finishes, a new job can pick up from the experiment's current best without losing state.

This split lets a user run five iterations, review the results, then push five more — the new job automatically starts from whatever the optimizer reached last time. Collapsing everything into a single "job" concept would lose that continuity.

At-most-one-active-job-per-experiment is enforced via a Postgres partial unique index on `job(experiment_id) WHERE status IN ('queued', 'running')`. A second `POST /experiments/{id}/jobs` while one is active returns 409.

### 2. Queue as the state machine

There is no long-running orchestrator process. The optimization loop is driven by two short-lived Postgres-backed queue task types:

- `benchmark.run` — runs the inner agent across all tasks via E2B sandboxes, writes trial results, enqueues `optimizer.propose`.
- `optimizer.propose` — checks stop conditions; if continuing, runs the Claude Agent SDK agentic loop, uploads the new agent file and learnings, inserts the next iteration row, enqueues `benchmark.run`.

Each worker claims a message, does its work, and writes business state + the next queue message in a single DB transaction. If the worker crashes mid-iteration the partial work is discarded; the experiment state reflects only fully-persisted iterations. No separate resume code path is needed.

The queue is a Postgres `task_queue` table consumed via `SELECT ... FOR UPDATE SKIP LOCKED`. Atomic with business-state writes, no Redis or Celery dependency.

### 3. Postgres for relational state, MinIO for blobs

Postgres holds anything that needs to be filtered, sorted, joined, or aggregated: IDs, statuses, scores, timestamps, foreign keys. MinIO (S3-compatible, running locally via Docker) holds blobs: agent `.py` files, message traces, optimizer transcripts, verifier outputs. Postgres rows carry `*_uri` pointer columns that address MinIO objects.

Mixing multi-MB documents into Postgres TOAST is possible but bloats backups, slows queries, and conflates two different access patterns. MinIO gives proper object semantics (prefix listing, HEAD, lifecycle) without an AWS dependency.

### 4. Workspace per experiment

Every experiment has its own MinIO prefix (`experiments/{exp_id}/`) and every non-experiment Postgres row carries an `experiment_id` foreign key. Two experiments running concurrently share no state — no agent files, no traces, no best-pointer.

### 5. Agent representation = full Python file

An `agent_version` is a thin Postgres row pointing to a `.py` file in MinIO. The file is the editable surface — system prompt, tools schema, loop, max steps, truncator — same as the reference repo's `agent/agent.py`. The file runs inside the E2B sandbox, never on the worker host, so the earlier concern about executing LLM-generated code directly on the server does not apply.

Structured-JSON config was considered and rejected: it would have fenced out loop-level edits that the reference repo's PROGRAM.md documents as a legitimate optimization technique, and iteration history as unified `.py` diffs is more honest than JSONB field diffs.

### 6. `OuterAgent` ABC + Claude Agent SDK as v1 implementation

The optimizer worker depends on an abstract `OuterAgent` interface (Python ABC), not on any SDK directly. The concrete v1 implementation (`ClaudeAgentSDKOuterAgent`) runs a Claude Agent SDK agentic loop against an ephemeral scratch directory pre-populated with `agent.py`, `learnings.md`, per-task traces, and an `INSTRUCTIONS.md`.

Tool surface is restricted at runtime via the SDK's `can_use_tool` callback: `Read` anywhere in the scratch directory; `Edit` and `Write` only on `agent.py` and `learnings.md`; no `Bash`, no network. Future implementations (`OpenAIAgentsSDKOuterAgent`, `SingleShotOuterAgent`) plug in via a string registry keyed by `experiment.optimizer_kind`.

### 7. Benchmark templates table

The outer agent's instruction template and per-benchmark defaults (task list, default models, concurrency) live in a seeded `templates` Postgres table — not API-mutable, edited only via SQL or a seed re-run. API callers pick a benchmark by `template_id`; all defaults cascade from the matching row. Resolution order: API body wins, then template defaults, then env-level fallbacks.

### 8. Two system prompts, two homes

The optimizer's meta-prompt lives in Postgres (`templates.instruction_template`) — it is owned by the experiment configuration, so the DB is the right home. The inner agent's system prompt lives inside the `.py` file in MinIO — it is owned by the file, so extracting it into a column would create a dual source of truth. Each prompt lives where its owner's source of truth lives.

### 9. `null` reward vs zero reward

`trial.reward IS NULL AND status='infra_error'` means the sandbox or runner crashed before the verifier ran. `reward=0 AND status='done'` means the agent ran successfully but failed the verifier. The reference repo conflates these, silently recording `0.0000` on harbor crashes. We distinguish them so infrastructure failures surface as infrastructure errors rather than being silently misattributed to agent quality.

### 10. SQLModel + Alembic + asyncpg

SQLModel provides Pydantic + SQLAlchemy in one type: the same class is the DB row schema and (with thin response/request subclasses) the API I/O schema. No DTO duplication. Alembic manages migrations via `alembic upgrade head`. asyncpg is the async driver, matching FastAPI's async-first model.

---

## API Surface

The service follows a 202-Accepted async pattern: `POST /experiments/{id}/jobs` returns immediately; the caller polls `GET /experiments/{id}/jobs/{job_id}` until status reaches a terminal state.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/experiments` | Create experiment. Body: `{name, template_id, task_ids?, optimizer_kind?, optimizer_model?}` |
| `GET` | `/experiments/{id}` | Experiment summary: best score, iteration count, latest job status |
| `POST` | `/experiments/{id}/jobs` | Start a job. Body: `{max_iterations, stopping_criteria?}`. Returns 409 if a job is already active |
| `GET` | `/experiments/{id}/jobs/{job_id}` | Job status — what `test_client.py` polls |
| `POST` | `/experiments/{id}/jobs/{job_id}/cancel` | Passive cancel: sets `status='cancelled'`; next worker bails |
| `GET` | `/experiments/{id}/iterations` | List all iterations across all jobs (experiment-scoped, monotonic numbering) |
| `GET` | `/experiments/{id}/iterations/{n}` | Full iteration detail: agent version, optimizer reasoning, per-task trial results, outcome |
| `GET` | `/tasks` | Read-only list of TerminalBench tasks the service supports (from the seeded template) |
| `GET` | `/healthz` | Liveness check |

### `test_client.py` usage

```bash
# Submit and immediately exit (wiring check only — no benchmark cost)
uv run python test_client.py --no-wait --max-iterations 1 --name smoke-test

# Full run: submit, poll, print iteration history
uv run python test_client.py --max-iterations 3

# Override specific tasks and poll faster
uv run python test_client.py \
  --task-ids fix-git,nginx-request-logging \
  --max-iterations 5 \
  --poll-interval 3

# All flags
uv run python test_client.py --help
```

| Flag | Default | Purpose |
|---|---|---|
| `--base-url` | `http://localhost:8000` | Service address |
| `--template-id` | `terminal_bench` | Which template to use |
| `--max-iterations` | `3` | Iteration cap for the job |
| `--name` | `smoke-<timestamp>` | Experiment name |
| `--task-ids` | template default | Comma-separated subset of task IDs |
| `--poll-interval` | `5.0` | Seconds between status polls |
| `--timeout` | `5400` | Total polling timeout in seconds |
| `--no-wait` | off | Submit and exit without polling |

**Note:** if `ANTHROPIC_API_KEY` is not set, the baseline benchmark (iteration 0) will run but the optimizer step will fail at the Claude Agent SDK call. The iteration is recorded as `failed` with a clear error message; the job stops cleanly. The smoke test uses `--no-wait` specifically to avoid this dependency.

---

## What We Would Do Differently with More Time

- **Expand the task list to 10–15.** The spec asks for 10–20 representative tasks. The current five-task set was chosen for fast iteration during development; a larger set would give the optimizer more diverse failure signal and make the benchmark more meaningful.
- **`SingleShotOuterAgent` for cheap regression tests.** A one-call structured-output implementation of the `OuterAgent` ABC would let us run optimizer regression tests without paying for a full agentic session per iteration.
- **`OpenAIAgentsSDKOuterAgent` as a second concrete impl.** Demonstrating the strategy pattern working across two real SDK implementations would validate the abstraction, not just describe it.
- **Real queue back-end for high throughput.** Postgres `FOR UPDATE SKIP LOCKED` is correct and sufficient for the assignment's load. At high concurrency (hundreds of concurrent jobs) a dedicated queue (Redis, pgmq) would be preferable without changing the message vocabulary.
- **True mid-iteration cancellation.** Currently cancellation is passive: the next worker bails on `job.status='cancelled'`. A real interrupt would require the `OuterAgent` ABC to expose a cancel hook and the Claude Agent SDK call to cooperate. Non-trivial; bounded waste from one extra iteration makes it a reasonable defer.
- **Idempotency keys on `POST /experiments/{id}/jobs`.** Retrying a failed create call can currently produce a duplicate job if the first request reached the DB. An idempotency key column + unique index would make retries safe.
- **Unit test coverage.** The current build validates correctness through the smoke test. Unit tests for the queue claim/ack logic, the stop-condition state machine, and the `OuterAgent` result-parsing path would be the first additions.
- **Stale-running sweeper as a standalone cron.** Currently the sweeper runs on a timer inside the worker process. A singleton cron job would be cleaner and easier to monitor.

---

## What We Intentionally Did Not Implement

### M5 — Multi-tenancy, orgs, and RBAC

Out of scope per the assignment ("M4 and M5 are intentionally open-ended"). The data model has no `user`, `org`, or `role` tables; all API routes are unauthenticated. Adding M5 would require: auth middleware on FastAPI routes, `org_id` / `owner_user_id` columns on every non-config row, and row-level scoping in all queries. None of this changes the core architecture — the extension points are clear.

### Mid-iteration interrupt on cancel

Per the passive-cancellation decision: `POST /jobs/{id}/cancel` sets `job.status='cancelled'` only. The in-flight worker completes at most one more benchmark or optimizer call before checking the flag and stopping. Worst-case waste is one extra E2B run and one extra LLM session — bounded and acceptable for the assignment's scope.

### Experiment branching ("rewind to iteration N")

No "start a new experiment from iteration N's agent" API knob. The mental model "create a new experiment with that snapshot as the baseline" achieves the same outcome: fetch the agent file from MinIO for the desired iteration, create a new experiment with it. Adding a branching API would require a tree-shaped iteration graph instead of a chain; the complexity is not justified for v1.

### Inline agent `.py` validation beyond `ast.parse()`

We run `ast.parse()` on the proposed agent file for syntax and a throwaway-sandbox import smoke-test before benchmarking. We do not enforce that the file exposes a `HarnessAgent` class or matches a specific interface contract. The harbor subprocess fails loudly on a malformed file; the iteration row is marked `failed` with a clear error message via the existing error path. Stricter compile-time validation would be a test-suite concern, not a production blocker.

### Full PR workflow on `neosigmaai/auto-harness`

The assignment spec asks for a branch and pull request on the upstream public repo. Per project ground rules this build is purely local with no GitHub remote. The deliverable is source code + this README.
