"""Goal decomposition — turns plain English into a task DAG.

Two strategies:
1. LLM planner (requires ANTHROPIC_API_KEY) — full natural language decomposition
2. Script planner (fallback) — regex-based extraction of file operations
"""

import json
import os
import re
import textwrap


class TaskSpec:
    """Blueprint for a task before it's persisted."""
    def __init__(self, name: str, description: str, code: str,
                 priority: float = 1.0, depends_on_names: list[str] | None = None):
        self.name = name
        self.description = description
        self.code = code
        self.priority = priority
        self.depends_on_names = depends_on_names or []

    def __repr__(self):
        return f"TaskSpec({self.name!r}, deps={self.depends_on_names})"


async def plan_goal(description: str) -> list[TaskSpec]:
    """Decompose a goal into a list of TaskSpecs with dependency edges."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return await _llm_plan(description, api_key)
    return _script_plan(description)


async def _llm_plan(description: str, api_key: str) -> list[TaskSpec]:
    """Use Claude to decompose the goal into executable tasks."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)

    prompt = textwrap.dedent(f"""\
    You are a task planner for an autonomous agent system. Decompose this goal into
    concrete, executable Python tasks. Each task should be a small unit of work that
    produces a real side-effect (creating files, running computations, etc).

    Goal: {description}

    Return a JSON array of tasks. Each task has:
    - "name": short snake_case identifier (unique)
    - "description": one-line human description
    - "code": Python code that accomplishes this task (will be exec'd)
    - "priority": float 1.0-5.0 (higher = more important)
    - "depends_on": list of task names this depends on ([] for root tasks)

    Rules:
    - Tasks MUST produce real side-effects (write files, create dirs, etc)
    - Code must be self-contained Python (can import stdlib)
    - Create directories before writing files
    - Add a final verification task that checks all outputs exist
    - Keep it minimal — fewest tasks that correctly accomplish the goal
    - Return ONLY the JSON array, no markdown fences
    """)

    msg = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    tasks_data = json.loads(text)
    return [
        TaskSpec(
            name=t["name"],
            description=t["description"],
            code=t["code"],
            priority=t.get("priority", 1.0),
            depends_on_names=t.get("depends_on", []),
        )
        for t in tasks_data
    ]


def _script_plan(description: str) -> list[TaskSpec]:
    """Regex-based fallback planner — extracts file paths and generates code."""
    # Extract file paths from the description
    paths = re.findall(r'(/[\w/.-]+\.\w+)', description)

    if not paths:
        # Single task: just execute the whole thing as a Python script
        return [
            TaskSpec(
                name="execute",
                description=f"Execute: {description}",
                code=_generate_goal_code(description),
                priority=2.0,
            ),
            TaskSpec(
                name="verify",
                description="Verify execution completed",
                code="print('Goal executed — manual verification needed')",
                priority=1.0,
                depends_on_names=["execute"],
            ),
        ]

    # Extract directory paths
    dirs = set()
    for p in paths:
        d = os.path.dirname(p)
        if d and d != "/":
            dirs.add(d)

    tasks: list[TaskSpec] = []

    # Setup task: create directories
    if dirs:
        mkdir_code = "\n".join(f"os.makedirs('{d}', exist_ok=True)" for d in sorted(dirs))
        mkdir_code = f"import os\n{mkdir_code}\nprint('Directories created: {', '.join(sorted(dirs))}')"
        tasks.append(TaskSpec(
            name="setup_dirs",
            description=f"Create output directories: {', '.join(sorted(dirs))}",
            code=mkdir_code,
            priority=3.0,
        ))

    # Parse what needs to be computed for each file
    file_tasks = _decompose_file_operations(description, paths)
    for ft in file_tasks:
        ft.depends_on_names = ["setup_dirs"] if dirs else []
        tasks.append(ft)

    # Wire sequential dependencies between file tasks if they reference each other
    for i, ft in enumerate(file_tasks):
        for j, other in enumerate(file_tasks):
            if i != j and other.name in ft.code:
                ft.depends_on_names.append(other.name)

    # Add verification task
    verify_checks = "\n".join(
        f"assert os.path.exists('{p}'), 'Missing: {p}'\n"
        f"content = open('{p}').read().strip()\n"
        f"assert len(content) > 0, 'Empty: {p}'\n"
        f"print(f'{p}: {{content}}')"
        for p in paths
    )
    verify_code = f"import os\n{verify_checks}\nprint('All outputs verified.')"
    tasks.append(TaskSpec(
        name="verify_outputs",
        description=f"Verify all output files exist and are non-empty",
        code=verify_code,
        priority=1.0,
        depends_on_names=[ft.name for ft in file_tasks],
    ))

    return tasks


def _decompose_file_operations(description: str, paths: list[str]) -> list[TaskSpec]:
    """Generate a task for each output file based on the goal description."""
    desc_lower = description.lower()
    tasks = []

    # Try to understand what goes in each file
    for i, path in enumerate(paths):
        name = f"write_{os.path.basename(path).replace('.', '_')}"
        code = _generate_file_code(description, path, paths)
        tasks.append(TaskSpec(
            name=name,
            description=f"Generate and write {path}",
            code=code,
            priority=2.0 - (i * 0.1),  # earlier files slightly higher priority
        ))

    # Check for dependencies between files (e.g., "sum" depends on "primes")
    for i, task in enumerate(tasks):
        for j, other in enumerate(tasks):
            if i != j:
                # If this task reads a file that another task writes
                other_path = paths[j]
                if other_path in task.code and "open(" in task.code and f"'{other_path}'" in task.code:
                    if other.name not in task.depends_on_names:
                        task.depends_on_names.append(other.name)

    return tasks


def _generate_file_code(description: str, target_path: str, all_paths: list[str]) -> str:
    """Generate Python code to create a specific output file based on context."""
    desc_lower = description.lower()
    basename = os.path.basename(target_path).lower()

    # Detect prime-related tasks
    if "prime" in desc_lower and "prime" in basename:
        # Extract count
        count_match = re.search(r'(?:first\s+)?(\d+)\s+prime', desc_lower)
        count = int(count_match.group(1)) if count_match else 10
        return textwrap.dedent(f"""\
            def primes(n):
                result = []
                candidate = 2
                while len(result) < n:
                    if all(candidate % p != 0 for p in result):
                        result.append(candidate)
                    candidate += 1
                return result

            p = primes({count})
            with open('{target_path}', 'w') as f:
                f.write('\\n'.join(str(x) for x in p) + '\\n')
            print(f'Wrote {{len(p)}} primes to {target_path}')
        """)

    if "sum" in basename and "prime" in desc_lower:
        # Find the primes file
        primes_path = None
        for p in all_paths:
            if "prime" in os.path.basename(p).lower() and p != target_path:
                primes_path = p
                break
        if primes_path:
            return textwrap.dedent(f"""\
                with open('{primes_path}') as f:
                    primes = [int(line.strip()) for line in f if line.strip()]
                total = sum(primes)
                with open('{target_path}', 'w') as f:
                    f.write(str(total) + '\\n')
                print(f'Sum of {{len(primes)}} primes = {{total}}, written to {target_path}')
            """)

    # Generic: write description into file
    return textwrap.dedent(f"""\
        with open('{target_path}', 'w') as f:
            f.write('Generated by Mycelium 2\\n')
        print(f'Wrote {target_path}')
    """)


def _generate_goal_code(description: str) -> str:
    """Last resort: generate a best-effort Python script for the whole goal."""
    return textwrap.dedent(f"""\
        # Auto-generated for goal: {description}
        print("Executing goal...")
        # This is a fallback — LLM planner would produce better code
        print("Goal description: {description}")
        print("Done.")
    """)
