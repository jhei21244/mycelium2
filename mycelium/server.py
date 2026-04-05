"""FastAPI server — REST API + WebSocket live feed + dashboard."""

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import DB
from .engine import Engine
from .agents import create_agent_pool


class GoalRequest(BaseModel):
    description: str


def create_app(db_path: str = "mycelium.db", agent_count: int = 4) -> FastAPI:
    app = FastAPI(title="Mycelium 2", version="0.1.0")

    db = DB(db_path)
    engine = Engine(db)
    agents = create_agent_pool(agent_count)

    @app.on_event("startup")
    async def startup():
        await db.connect()
        for a in agents.values():
            await db.register_agent(a.agent_id, a.name)
        asyncio.create_task(engine.run(agents))

    @app.on_event("shutdown")
    async def shutdown():
        engine.stop()
        await db.close()

    # ── REST API ───────────────────────────────────────────

    @app.post("/api/goals")
    async def submit_goal(req: GoalRequest):
        try:
            gid = await engine.submit_goal(req.description)
            return {"goal_id": gid, "status": "active"}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/goals")
    async def list_goals():
        goals = await db.list_goals()
        return goals

    @app.get("/api/goals/{goal_id}")
    async def get_goal(goal_id: str):
        goal = await db.get_goal(goal_id)
        if not goal:
            return JSONResponse({"error": "not found"}, status_code=404)
        tasks = await db.get_tasks_for_goal(goal_id)
        for t in tasks:
            deps = await db.get_task_deps(t["id"])
            t["depends_on"] = deps
        goal["tasks"] = tasks
        return goal

    @app.get("/api/signal")
    async def signal_field():
        field = await engine.compute_signal_field()
        return field

    @app.get("/api/agents")
    async def list_agents():
        return await db.list_agents()

    @app.get("/api/events")
    async def recent_events(limit: int = 50):
        return await db.recent_events(limit)

    # ── WebSocket live feed ────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        queue = engine.subscribe()
        try:
            while True:
                event = await queue.get()
                await ws.send_text(json.dumps(event, default=str))
        except WebSocketDisconnect:
            pass
        finally:
            engine.unsubscribe(queue)

    # ── Dashboard ──────────────────────────────────────────

    @app.get("/")
    async def dashboard():
        html_path = Path(__file__).parent / "static" / "index.html"
        return HTMLResponse(html_path.read_text())

    return app
