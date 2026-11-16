"""Host bridge: missions driven through the real controller, the test playing host and agents.

Every step is a bridge command, as a host session would run it. Nothing is mocked: the
controller, coordinator, storage and prompt rendering are Zenith's own.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from zenith_harness import host_bridge
from zenith_harness.config import HarnessConfig
from zenith_harness.storage import ProjectStore

MID = "mission-001"
BRIEF = "Build the thing and prove it works."


class Host:
    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        self.tmp_path = tmp_path
        self.capsys = capsys
        self.store = ProjectStore(HarnessConfig.discover())
        self.files = 0

    def run(self, *argv: str, code: int = 0) -> dict[str, Any]:
        status = host_bridge.main(list(argv))
        out = json.loads(self.capsys.readouterr().out)
        assert status == code, out
        return out

    def json_file(self, data: object) -> str:
        self.files += 1
        path = self.tmp_path / f"in-{self.files}.json"
        path.write_text(json.dumps(data))
        return str(path)

    def start(self, tasks: list[dict[str, Any]]) -> str:
        workspace = self.tmp_path / "workspace"
        workspace.mkdir()
        brief = self.tmp_path / "brief.md"
        brief.write_text(BRIEF)
        pid = self.run("start", str(workspace), str(brief))["projectId"]
        contract = self.store.ensure_contract_dir(pid, MID)
        for assertion in sorted({target for task in tasks for target in task["targets"]}):
            (contract / f"{assertion}.md").write_text(f"# {assertion}\n\nIt works.\n")
        planned = self.run("plan", pid, self.json_file({"tasks": tasks}))
        assert planned["state"]["state"] == "mission_running"
        return pid

    def rows(self, pid: str, command: str = "advance") -> dict[str, dict[str, Any]]:
        return {row["task"]: row for row in self.run(command, pid)["host_pending"]}

    def handoff(self, pid: str, task: str, code: int = 0, **result: Any) -> dict[str, Any]:
        return self.run("handoff", pid, task, self.json_file(result), code=code)

    def decide(self, pid: str, stopped: dict[str, Any], action: str = "continue") -> dict[str, Any]:
        rows = [{"item_id": item["id"], "action": action} for item in stopped["state"]["items"]]
        return self.run("decide", pid, self.json_file(rows))

    def statuses(self, pid: str) -> dict[str, str]:
        return {
            task_id: entry.status
            for task_id, entry in self.store.load_task_state(pid, MID).tasks.items()
        }

    def reports(self, pid: str) -> dict[str, list[str]]:
        """Every recorded attempt's report, by task."""
        found: dict[str, list[str]] = {}
        for attempt in self.store.list_attempts(pid, MID):
            handoff = self.store.read_attempt(pid, MID, attempt.spawn_ts, attempt.node_id)
            assert handoff is not None
            found.setdefault(attempt.node_id, []).append(handoff.report)
        return found


@pytest.fixture
def host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Host:
    for name in ("ZENITH_PROJECTS_DIR", "ZENITH_PROJECT_BUCKET_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZENITH_HOME", str(tmp_path / "zenith-home"))
    monkeypatch.setenv("ZENITH_MAX_PARALLEL_NODES", "4")
    return Host(tmp_path, capsys)


def _task(task_id: str, task_type: str, targets: list[str], *depends_on: str) -> dict[str, Any]:
    gate = task_type == "gate"
    return {
        "id": task_id,
        "type": task_type,
        "body": "" if gate else f"Assignment of {task_id}.",
        "targets": targets,
        "skill": None if gate else "s",
        "depends_on": list(depends_on),
    }


def _verdict(report: str, targets: list[str]) -> dict[str, Any]:
    items = [{"item_id": target, "passed": True} for target in targets]
    return {"done": True, "report": report, "items": items, "passed": True}


def test_work_two_parallel_validators_gate_and_terminal_review_reach_done(host: Host) -> None:
    targets = ["VAL-001", "VAL-002"]
    pid = host.start(
        [
            _task("w1", "work", targets),
            _task("v1", "validate", targets, "w1"),
            _task("v2", "validate", targets, "w1"),
            _task("g1", "gate", targets, "v1", "v2"),
        ]
    )
    store = host.store

    # The work task reaches the host as a rendered prompt; the runtime stops and records nothing.
    rows = host.rows(pid)
    assert list(rows) == ["w1"]
    prompt = Path(rows["w1"]["prompt"]).read_text()
    assert "Assignment of w1." in prompt
    assert str(store.contract_assertion_path(pid, MID, "VAL-001")) in prompt
    memory = store.zenith_dir(pid) / "MEMORY.md"
    assert memory.exists() and str(memory) in prompt
    assert host.statuses(pid)["w1"] == "running" and host.reports(pid) == {}

    # Its result is picked up by Zenith's resume path; both validators then go out together.
    host.handoff(pid, "w1", done=True, report="built")
    rows = host.rows(pid)
    assert sorted(rows) == ["v1", "v2"]
    assert not any(row["in_flight"] or row["has_result"] for row in rows.values())
    assert "It works." in Path(rows["v2"]["prompt"]).read_text()
    assert host.reports(pid) == {"w1": ["built"]}

    # v1 finishes first: it is recorded, and v2 stays its agent's job instead of failing.
    host.handoff(pid, "v1", **_verdict("v1 saw it work", targets))
    rows = host.rows(pid)
    assert list(rows) == ["v2"] and rows["v2"]["in_flight"]
    assert host.statuses(pid) == {
        "w1": "cleared",
        "v1": "cleared",
        "v2": "running",
        "g1": "pending",
    }
    assert "v2" not in host.reports(pid)

    # Both verdicts in: the gate clears and stops for the orchestrator's decision.
    host.handoff(pid, "v2", **_verdict("v2 saw it work", targets))
    envelope = host.run("advance", pid)
    assert envelope["state"]["state"] == "attention_needed"
    assert "cleared: True" in envelope["state"]["items"][0]["report"]
    assert host.decide(pid, envelope)["state"]["state"] == "mission_running"
    assert host.run("advance", pid)["state"]["state"] == "mission_running"

    # Closure: the terminal reviewer's prompt goes to the host and its result ends the mission.
    review = host.rows(pid, "end")["terminal-review"]
    assert BRIEF in Path(review["prompt"]).read_text()
    assert host.run("inspect", pid)["state"]["state"] == "mission_running"
    host.run("review", pid, host.json_file({"done": True, "report": "no gaps found"}))
    assert host.run("end", pid)["state"] == {"state": "done"}

    assert host.statuses(pid) == dict.fromkeys(["w1", "v1", "v2", "g1"], "cleared")
    assert host.reports(pid) == {
        "w1": ["built"],
        "v1": ["v1 saw it work"],
        "v2": ["v2 saw it work"],
    }
    verdicts = store.load_contract_state(pid, MID).items
    assert {assertion: entry.status for assertion, entry in verdicts.items()} == dict.fromkeys(
        targets, "passed"
    )
    closeout = (store.mission_dir(pid, MID) / "closeout.md").read_text()
    assert "status: done" in closeout and "no gaps found" in closeout

    # The same state through the module's own entry point.
    src = str(Path(host_bridge.__file__).resolve().parents[1])
    path = os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")]))
    shown = subprocess.run(
        [sys.executable, "-m", "zenith_harness.host_bridge", "inspect", pid],
        env={**os.environ, "PYTHONPATH": path},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(shown.stdout)["state"] == {"state": "done"}


def test_results_arriving_around_attention_stops_are_neither_lost_nor_rerun(host: Host) -> None:
    pid = host.start([_task(name, "work", [f"VAL-{name.upper()}"]) for name in "abc"])
    assert sorted(host.rows(pid)) == ["a", "b", "c"]

    # a asks for attention: the runtime stops with b and c still at work.
    host.handoff(pid, "a", done=True, report="a done", request_attention=True)
    stopped = host.run("advance", pid)
    assert stopped["state"]["state"] == "attention_needed"
    assert host.statuses(pid) == {"a": "cleared", "b": "pending", "c": "pending"}

    # b reports while the stop is open. After the decision neither b nor c is a new job.
    host.handoff(pid, "b", done=True, report="b done", request_attention=True)
    host.decide(pid, stopped)
    rows = host.rows(pid)
    assert {name: (row["has_result"], row["in_flight"]) for name, row in rows.items()} == {
        "b": (True, True),
        "c": (False, True),
    }

    # b's own request stops the runtime again; c reports during that stop and is not re-run.
    stopped = host.run("advance", pid)
    assert stopped["state"]["state"] == "attention_needed"
    host.handoff(pid, "c", done=True, report="c done")
    host.decide(pid, stopped)
    assert host.run("advance", pid)["state"]["state"] == "mission_running"

    assert host.statuses(pid) == dict.fromkeys("abc", "cleared")
    assert host.reports(pid) == {"a": ["a done"], "b": ["b done"], "c": ["c done"]}


def test_refused_calls_leave_the_mission_untouched(host: Host) -> None:
    pid = host.start([_task("w1", "work", ["VAL-001"])])
    early = host.run("review", pid, host.json_file({"done": True}), code=1)["host_error"]
    assert "run `end` first" in early
    assert list(host.rows(pid)) == ["w1"]

    # A malformed result and an unknown task are refused before anything is parked.
    assert "done" in host.handoff(pid, "w1", code=1, report="no verdict")["host_error"]
    unknown = host.handoff(pid, "w9", code=1, done=True, report="")["host_error"]
    assert "no work or validate task 'w9'" in unknown
    assert not host_bridge.parked_path(host.store, pid, "w1").exists()

    # Closure while an agent is at work is refused, and the task is not turned into a failure.
    assert "mission_not_ready_to_close" in host.run("end", pid, code=1)["tool_error"]
    assert host.statuses(pid) == {"w1": "pending"} and host.reports(pid) == {}

    # The agent reports a failure and a retry is decided. Until the retry is dispatched no
    # agent is at work, so the old result handed in again is refused instead of answering it.
    host.handoff(pid, "w1", done=False, report="broke")
    failed = host.run("advance", pid)
    assert failed["state"]["state"] == "attention_needed"
    assert host.statuses(pid) == {"w1": "failed"}
    host.decide(pid, failed, action="retry")
    stale = host.handoff(pid, "w1", code=1, done=False, report="broke")["host_error"]
    assert "is pending with no agent at work" in stale
    assert not host.rows(pid)["w1"]["in_flight"]

    host.handoff(pid, "w1", done=True, report="built")
    assert host.run("advance", pid)["state"]["state"] == "mission_running"
    assert host.statuses(pid) == {"w1": "cleared"} and host.reports(pid)["w1"][-1] == "built"
    late = host.handoff(pid, "w1", code=1, done=True, report="again")["host_error"]
    assert "is cleared with no agent at work" in late
    assert host.run("inspect", "no-such-project", code=1)["host_error"].startswith("FileNotFound")


def test_console_script_points_at_the_bridge() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]
    module, _, name = project["scripts"]["zenith-host"].partition(":")
    assert getattr(importlib.import_module(module), name) is host_bridge.main
