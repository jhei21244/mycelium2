"""The Engine — signal field computation, agent dispatch, state machine.

Runs as an async background loop. Every tick:
1. Promote pending tasks whose deps are satisfied
2. Compute signal field for all ready tasks
3. Match idle agents to strongest signals
4. Check for goal completion
5. Broadcast state changes
"""

import asyncio
import time

from .db import DB
from .signal import compute_signal
from .planner import plan_goal


class Engine:
    def __init__(self, db: DB):
        self.db = db
        self._subscribers: list[asyncio.Queue] = []
        self._running = False
        self.tick_interval = 0.5  # seconds

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q) if hasattr(self._subscribers, 'discard') else None
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def broadcast(self, event: dict):
        dead = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # ── Goal submission ────────────────────────────────────

    async def submit_goal(self, description: str) -> str:
        """Accept a goal, decompose it into tasks, activate it."""
        gid = await self.db.create_goal(description)
        await self.broadcast({"type": "goal_created", "goal_id": gid, "description": description})

        # Plan: decompose into task DAG
        try:
            specs = await plan_goal(description)
        except Exception as e:
            await self.db.update_goal_status(gid, "failed")
            await self.broadcast({"type": "goal_failed", "goal_id": gid, "error": str(e)})
            raise

        # Persist tasks and wire up dependencies
        name_to_id: dict[str, str] = {}
        for spec in specs:
            dep_ids = [name_to_id[d] for d in spec.depends_on_names if d in name_to_id]
            tid = await self.db.create_task(
                goal_id=gid,
                name=spec.name,
                description=spec.description,
                code=spec.code,
                priority=spec.priority,
                depends_on=dep_ids if dep_ids else None,
            )
            name_to_id[spec.name] = tid

        await self.db.update_goal_status(gid, "active")
        await self.broadcast({"type": "goal_active", "goal_id": gid, "tasks": len(specs)})
        return gid

    # ── Signal field ───────────────────────────────────────

    async def compute_signal_field(self) -> list[dict]:
        """Return all ready tasks with their computed signal strengths."""
        ready = await self.db.get_ready_tasks()
        now = time.time()
        field = []
        for t in ready:
            sig = compute_signal(t["priority"], t["failures"], t["available_at"], now)
            field.append({**t, "signal": sig})
        field.sort(key=lambda x: x["signal"], reverse=True)
        return field

    # ── Core tick ──────────────────────────────────────────

    async def tick(self, agents: dict):
        """One tick of the engine loop."""
        # 1. Promote tasks whose deps are now satisfied
        goals = await self.db.list_goals()
        for g in goals:
            if g["status"] == "active":
                promoted = await self.db.promote_pending_tasks(g["id"])
                for tid in promoted:
                    await self.broadcast({"type": "task_promoted", "task_id": tid})

        # 2. Compute signal field
        field = await self.compute_signal_field()
        if field:
            await self.broadcast({
                "type": "signal_field",
                "tasks": [{"id": t["id"], "name": t["name"], "signal": t["signal"]} for t in field],
            })

        # 3. Match idle agents to tasks
        idle_agents = [a for a in agents.values() if a.status == "idle"]
        for task_info in field:
            if not idle_agents:
                break
            agent = idle_agents.pop(0)
            claimed = await self.db.claim_task(task_info["id"], agent.agent_id)
            if claimed:
                await self.db.update_agent(agent.agent_id, "working", task_info["id"])
                await self.broadcast({
                    "type": "task_claimed",
                    "task_id": task_info["id"],
                    "task_name": task_info["name"],
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "signal": task_info["signal"],
                })
                # Dispatch execution
                asyncio.create_task(self._execute_task(agent, task_info))

        # 4. Check goal completion
        for g in goals:
            if g["status"] == "active":
                result = await self.db.check_goal_completion(g["id"])
                if result:
                    await self.db.update_goal_status(g["id"], result)
                    await self.broadcast({
                        "type": "goal_completed" if result == "completed" else "goal_failed",
                        "goal_id": g["id"],
                    })

    async def _execute_task(self, agent, task_info: dict):
        """Run a task via the agent, handle result."""
        try:
            result = await agent.execute(task_info["code"], task_info["name"])
            await self.db.complete_task(task_info["id"], result)
            await self.db.update_agent(agent.agent_id, "idle", None)
            await self.broadcast({
                "type": "task_done",
                "task_id": task_info["id"],
                "task_name": task_info["name"],
                "agent_id": agent.agent_id,
                "result": result[:500] if result else "ok",
            })
        except Exception as e:
            error_msg = str(e)
            new_status = await self.db.fail_task(task_info["id"], error_msg)
            await self.db.update_agent(agent.agent_id, "idle", None)
            await self.broadcast({
                "type": "task_failed",
                "task_id": task_info["id"],
                "task_name": task_info["name"],
                "agent_id": agent.agent_id,
                "error": error_msg[:500],
                "will_retry": new_status == "ready",
            })

    # ── Run loop ───────────────────────────────────────────

    async def run(self, agents: dict):
        """Main engine loop — runs until stopped."""
        self._running = True
        while self._running:
            try:
                await self.tick(agents)
            except Exception as e:
                await self.broadcast({"type": "engine_error", "error": str(e)})
            await asyncio.sleep(self.tick_interval)

    def stop(self):
        self._running = False
