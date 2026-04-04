# Mycelium 2

Autonomous agent coordination through stigmergy.

Give it a goal in plain English. It decomposes the goal into a dependency DAG of executable tasks. Agents self-organise around a **signal field** — no central scheduler assigns work. Tasks emit signals based on urgency, priority, and wait time. Agents perceive the field and claim the strongest signal they find. When a task completes, downstream tasks become visible. When a task fails, its signal amplifies for faster retry.

## Architecture

```
Goal (plain English)
  │
  ▼
Planner ──→ Task DAG (dependency graph)
  │
  ▼
Signal Field ──→ Each ready task emits a signal
  │               signal = priority × (1 + 0.5 × failures) × log(1 + age/30)
  ▼
Agent Pool ──→ Agents claim strongest signal, execute in subprocess
  │
  ▼
Engine Tick ──→ Promote deps, dispatch, detect completion
```

**Signal formula**: `urgency × temporal_pressure`
- **urgency** = `priority × (1 + 0.5 × failure_count)` — failed tasks get louder
- **temporal_pressure** = `log(1 + wait_seconds / 30)` — grows with time, never decays

When a task is claimed, its signal drops to zero. Other agents ignore it. When it fails and retries, it re-enters the field with amplified priority.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Dashboard: http://localhost:8420
API: http://localhost:8420/api

## Submit a Goal

```bash
curl -X POST http://localhost:8420/api/goals \
  -H "Content-Type: application/json" \
  -d '{"description": "Write the first 10 prime numbers to /tmp/myc2/primes.txt and their sum to /tmp/myc2/sum.txt"}'
```

Or use the dashboard UI.

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/goals` | Submit a new goal |
| GET | `/api/goals` | List all goals |
| GET | `/api/goals/{id}` | Goal detail with tasks |
| GET | `/api/signal` | Current signal field |
| GET | `/api/agents` | Agent pool status |
| GET | `/api/events` | Event log |
| WS | `/ws` | Live event stream |

## LLM Planning

Set `ANTHROPIC_API_KEY` for full natural-language goal decomposition via Claude. Without it, the system uses a regex-based fallback planner that handles file-creation goals.

## Components

- **`mycelium/signal.py`** — Signal field computation (pure function)
- **`mycelium/planner.py`** — Goal → Task DAG (LLM + fallback)
- **`mycelium/engine.py`** — Core tick loop: promote, dispatch, complete
- **`mycelium/agents.py`** — Agent pool, subprocess execution
- **`mycelium/db.py`** — Async SQLite persistence
- **`mycelium/server.py`** — FastAPI REST + WebSocket + dashboard
