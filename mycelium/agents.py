"""Agent workers — perceive the signal field, claim tasks, execute real work.

Agents run Python code in isolated subprocesses. They don't decide what to do —
the signal field guides them. They just execute faithfully and report results.
"""

import asyncio
import uuid


class Agent:
    """A worker agent that executes Python code in a subprocess."""

    def __init__(self, name: str, agent_id: str | None = None, timeout: float = 30.0):
        self.agent_id = agent_id or uuid.uuid4().hex[:8]
        self.name = name
        self.status = "idle"
        self.timeout = timeout

    async def execute(self, code: str, task_name: str) -> str:
        """Execute Python code in a subprocess. Returns stdout. Raises on failure."""
        self.status = "working"
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"Task '{task_name}' timed out after {self.timeout}s")

            stdout_text = stdout.decode().strip()
            stderr_text = stderr.decode().strip()

            if proc.returncode != 0:
                error = stderr_text or stdout_text or f"Exit code {proc.returncode}"
                raise RuntimeError(f"Task '{task_name}' failed: {error}")

            return stdout_text
        finally:
            self.status = "idle"


def create_agent_pool(count: int = 4) -> dict[str, Agent]:
    """Create a pool of named agents."""
    # Mycelium-themed names
    names = [
        "Hypha-α", "Hypha-β", "Hypha-γ", "Hypha-δ",
        "Hypha-ε", "Hypha-ζ", "Hypha-η", "Hypha-θ",
    ]
    agents = {}
    for i in range(min(count, len(names))):
        a = Agent(name=names[i])
        agents[a.agent_id] = a
    return agents
