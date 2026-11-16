"""Host bridge: run Zenith's runtime from a host session that starts the agents itself.

Zenith's controller needs a dispatcher (runs one task) and a terminal reviewer. The stock
ones (`acp_runner`) launch agents over ACP and block until each agent ends. The bridge's
dispatcher instead writes the task's rendered prompt to a file and stops the runtime. The
host session gives that prompt to a fresh agent, parks the agent's result with `handoff`
and calls `advance` again, where Zenith's own resume path (the attempt files of tasks
marked running) picks the result up. The terminal reviewer works the same way through
`end` and `review`. Nothing keeps running between commands: all state is Zenith's own files
under ZENITH_HOME, plus `<project>/.zenith-runtime/host/` for prompts and parked results.

Agents finish at different times. `advance` first records every parked result of a running
task, puts running tasks that have no result yet back to pending (their agent is still at
work) and then lets the runtime go on. Such a task comes back in `host_pending` marked
`in_flight`: do not start a second agent for it. A row marked `has_result` only needs
another `advance`. Without this step Zenith's crash recovery would record a failure for a
task whose agent is still working.

Commands (each prints JSON; exit status 1 when the call is refused):

  start <workspace> <brief.md>        new project and its first mission
  plan <project> <tasks.json>         submit the task list (contract files come first)
  advance <project>                   run until the host has work: prints `host_pending`
  handoff <project> <task> <json>     park one agent's result
  decide <project> <decisions.json>   resolve the open attention items
  end <project>                       request closure: prints the terminal-review row
  review <project> <json>             save the terminal review, then `end` again
  inspect <project>                   state and task list

The host's loop, and a mission that ran this way, are described in `docs/host-bridge.md`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .acp_runner import ACPNodeRunner
from .assets import AssetLoader
from .config import HarnessConfig
from .controller import ProjectController, ToolError
from .dispatcher import DispatchRequest, NodeHandoff
from .models import (
    Decision,
    Envelope,
    MissionRunning,
    Task,
    TaskList,
    TerminalReviewHandoff,
    ValidateHandoff,
    WorkHandoff,
)
from .storage import ProjectStore, atomic_write_json, atomic_write_text

COMMANDS: dict[str, tuple[str, ...]] = {
    "start": ("workspace", "brief"),
    "plan": ("project", "tasks"),
    "advance": ("project",),
    "handoff": ("project", "task", "result"),
    "decide": ("project", "decisions"),
    "end": ("project",),
    "review": ("project", "result"),
    "inspect": ("project",),
}


class HostYield(BaseException):
    """Stops the runtime once tasks are marked running and their prompts are written.

    Not an `Exception` on purpose: the coordinator records any `Exception` from a
    dispatcher or reviewer as a failure, and handing work to the host is not one.
    """

    def __init__(self, rows: list[dict[str, Any]]):
        super().__init__(rows)
        self.rows = rows


def host_dir(store: ProjectStore, project_id: str) -> Path:
    """Prompts, parked results and `pending.json`, beside Zenith's own runtime cursors."""
    path = store.zenith_runtime_dir(project_id) / "host"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parked_path(store: ProjectStore, project_id: str, task_id: str) -> Path:
    path = host_dir(store, project_id) / "parked"
    path.mkdir(exist_ok=True)
    return path / f"{task_id}.json"


def to_handoff(task: Task, data: Any) -> NodeHandoff:
    """An agent's result as Zenith's handoff model; a malformed result raises ValueError."""
    payload = {**data, "node_id": task.id} if isinstance(data, dict) else data
    if task.type == "validate":
        return ValidateHandoff.model_validate(payload)
    return WorkHandoff.model_validate(payload)


def take_parked(store: ProjectStore, project_id: str, task: Task, spawn_ts: str) -> NodeHandoff:
    """Read a parked result and mark it used for the attempt `spawn_ts`: its agent is done."""
    path = parked_path(store, project_id, task.id)
    handoff = to_handoff(task, json.loads(path.read_text(encoding="utf-8")))
    path.rename(path.with_name(f"{task.id}.{spawn_ts}.used"))
    write_in_flight(store, project_id, read_in_flight(store, project_id) - {task.id})
    return handoff


def read_in_flight(store: ProjectStore, project_id: str) -> set[str]:
    """Tasks handed to the host whose agents are still at work, as of the last settle."""
    record = host_dir(store, project_id) / "in_flight.json"
    return set(json.loads(record.read_text(encoding="utf-8"))) if record.exists() else set()


def write_in_flight(store: ProjectStore, project_id: str, tasks: set[str]) -> None:
    atomic_write_json(host_dir(store, project_id) / "in_flight.json", sorted(tasks))


class HostDispatcher:
    """`NodeDispatcher` that hands each task's prompt to the host instead of launching an agent."""

    def __init__(self, config: HarnessConfig, store: ProjectStore):
        self.store = store
        # Only the stock runner's prompt rendering is used; constructing it starts nothing.
        self.runner = ACPNodeRunner(config=config, loader=AssetLoader(config))

    def dispatch(self, request: DispatchRequest) -> NodeHandoff:
        return self.dispatch_batch([request])[0]

    def dispatch_batch(self, requests: list[DispatchRequest]) -> list[NodeHandoff]:
        """Return the batch's results when the host has parked all of them, else stop."""
        parked = [parked_path(self.store, r.project_id, r.task.id).exists() for r in requests]
        if all(parked):
            return [take_parked(self.store, r.project_id, r.task, r.spawn_ts) for r in requests]
        raise HostYield(
            [
                {**self._write_prompt(request), "has_result": has_result}
                for request, has_result in zip(requests, parked, strict=True)
            ]
        )

    def _write_prompt(self, request: DispatchRequest) -> dict[str, Any]:
        pid, mid, task = request.project_id, request.mission_id, request.task
        prompt = self.runner._render_prompts(
            task=task,
            mission_id=mid,
            project_bucket=str(self.store.zenith_dir(pid)),
            workspace_dir=str(self.store.workspace_dir(pid)),
            store=self.store,
            project_id=pid,
        )
        path = host_dir(self.store, pid) / f"{request.spawn_ts}__{task.id}.prompt.md"
        atomic_write_text(path, prompt)
        return {
            "task": task.id,
            "type": task.type,
            "targets": task.targets,
            "mission": mid,
            "spawn_ts": request.spawn_ts,
            "prompt": str(path),
        }


class HostTerminalReviewer:
    """`TerminalReviewer` that hands the review prompt to the host and reads its result back."""

    def __init__(self, config: HarnessConfig, store: ProjectStore):
        self.store = store
        self.runner = ACPNodeRunner(config=config, loader=AssetLoader(config))

    def review(self, project_id: str, mission_id: str, spawn_ts: str) -> TerminalReviewHandoff:
        out = host_dir(self.store, project_id)
        result = review_result_path(self.store, project_id, mission_id)
        if result.exists():
            review = TerminalReviewHandoff.model_validate(
                json.loads(result.read_text(encoding="utf-8"))
            )
            result.rename(out / f"terminal-review__{mission_id}__{spawn_ts}.used.json")
            return review
        prompt = self.runner._render_terminal_reviewer_prompts(
            project_bucket=str(self.store.zenith_dir(project_id)),
            workspace_dir=str(self.store.workspace_dir(project_id)),
        )
        path = review_prompt_path(self.store, project_id, mission_id)
        atomic_write_text(path, prompt)
        raise HostYield(
            [
                {
                    "task": "terminal-review",
                    "type": "terminal-review",
                    "mission": mission_id,
                    "prompt": str(path),
                    "result": str(result),
                }
            ]
        )


def review_prompt_path(store: ProjectStore, project_id: str, mission_id: str) -> Path:
    return host_dir(store, project_id) / f"terminal-review__{mission_id}.prompt.md"


def review_result_path(store: ProjectStore, project_id: str, mission_id: str) -> Path:
    return host_dir(store, project_id) / f"terminal-review__{mission_id}.json"


def settle_running(store: ProjectStore, project_id: str) -> set[str]:
    """Before the runtime resumes: record parked results, re-queue tasks still at work.

    Zenith's resume path applies the attempt file of every task marked running and turns a
    running task without one into a failure. So a parked result becomes that attempt file,
    and a running task with no result goes back to pending. Returns the tasks whose agents
    are still at work; the set is kept in `in_flight.json` because the runtime may stop for
    attention before it dispatches such a task again.
    """
    state = store.load_state(project_id)
    if not isinstance(state, MissionRunning):
        return set()
    mid = state.mission_id
    task_state = store.load_task_state(project_id, mid)
    before = read_in_flight(store, project_id)
    in_flight: set[str] = set()
    for task in store.load_task_list(project_id, mid).tasks:
        entry = task_state.tasks.get(task.id)
        if entry is None or entry.status not in ("pending", "running"):
            continue
        if entry.status == "pending":
            if task.id in before:
                in_flight.add(task.id)
            continue
        attempts = store.list_attempts(project_id, mid, node_id=task.id)
        if entry.last_attempt and parked_path(store, project_id, task.id).exists():
            handoff = take_parked(store, project_id, task, entry.last_attempt)
            store.save_attempt(project_id, mid, entry.last_attempt, task.id, handoff)
        elif not attempts or attempts[-1].spawn_ts != entry.last_attempt:
            task_state.set_status(task.id, "pending")
            in_flight.add(task.id)
    store.save_task_state(project_id, mid, task_state)
    write_in_flight(store, project_id, in_flight)
    return in_flight


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _current_mission(store: ProjectStore, project_id: str) -> str:
    mission_id = store.load_project(project_id).current_mission_id
    if mission_id is None:
        raise ValueError(f"project {project_id!r} has no mission")
    return mission_id


def _park(store: ProjectStore, project_id: str, task_id: str, data: Any) -> dict[str, Any]:
    mid = _current_mission(store, project_id)
    task = next((t for t in store.load_task_list(project_id, mid).tasks if t.id == task_id), None)
    if task is None or task.type == "gate":
        raise ValueError(f"{mid} has no work or validate task {task_id!r}")
    status = store.load_task_state(project_id, mid).status_of(task_id)
    at_work = status == "pending" and task_id in read_in_flight(store, project_id)
    if status != "running" and not at_work:
        # Nothing would read it now, and the task's next dispatch would take it as its own.
        raise ValueError(f"task {task_id!r} is {status} with no agent at work: it takes no result")
    handoff = to_handoff(task, data)  # refuse a malformed result while its agent can be asked
    path = parked_path(store, project_id, task_id)
    atomic_write_json(path, handoff.model_dump(mode="json"))
    return {"saved": task_id, "parked": str(path)}


def _save_review(store: ProjectStore, project_id: str, data: Any) -> dict[str, Any]:
    mid = _current_mission(store, project_id)
    if not review_prompt_path(store, project_id, mid).exists():
        raise ValueError(f"no terminal review was requested for {mid}: run `end` first")
    review = TerminalReviewHandoff.model_validate(data)
    path = review_result_path(store, project_id, mid)
    atomic_write_json(path, review.model_dump(mode="json"))
    return {"saved": str(path)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zenith-host",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, operands in COMMANDS.items():
        command = commands.add_parser(name)
        for operand in operands:
            command.add_argument(operand)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = HarnessConfig.discover()
    store = ProjectStore(config)
    controller = ProjectController(
        config, HostDispatcher(config, store), HostTerminalReviewer(config, store), store=store
    )
    in_flight: set[str] = set()
    result: Envelope | dict[str, Any]
    try:
        if args.command == "start":
            brief = Path(args.brief).read_text(encoding="utf-8")
            result = controller.start_project(brief, args.workspace)
        elif args.command == "plan":
            tasks = TaskList.model_validate(_load(args.tasks))
            result = controller.submit_plan(args.project, tasks)
        elif args.command == "advance":
            in_flight = settle_running(store, args.project)
            result = controller.advance_project(args.project)
        elif args.command == "handoff":
            result = _park(store, args.project, args.task, _load(args.result))
        elif args.command == "decide":
            decisions = [Decision.model_validate(row) for row in _load(args.decisions)]
            result = controller.decide_attention(args.project, decisions)
        elif args.command == "end":
            # A task whose agent is still at work blocks closure instead of being failed by it.
            settle_running(store, args.project)
            result = controller.end_mission(args.project)
        elif args.command == "review":
            result = _save_review(store, args.project, _load(args.result))
        else:
            result = controller.inspect_project(args.project)
    except HostYield as pending:
        rows = [{**row, "in_flight": row["task"] in in_flight} for row in pending.rows]
        atomic_write_json(host_dir(store, args.project) / "pending.json", rows)
        result = {"host_pending": rows}
    except ToolError as error:
        details = [str(detail) for detail in error.details] if error.details else None
        print(json.dumps({"tool_error": str(error), "details": details}, indent=1))
        return 1
    except (ValueError, FileNotFoundError) as error:
        print(json.dumps({"host_error": f"{type(error).__name__}: {error}"}, indent=1))
        return 1
    shown = result.model_dump(mode="json") if isinstance(result, Envelope) else result
    print(json.dumps(shown, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
