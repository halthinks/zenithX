# Running Zenith from a host session: the host bridge

This guide says what Zenith is, how to run it through `zenith_harness/host_bridge.py`, how
one real mission ran that way, and what a stock install on a Linux box still needs. Every
step is marked **tried** or **not tried**; nothing here was tried on a box.

## What Zenith is

Zenith is a harness for long agent work. One agent session is the **orchestrator**. It
turns the user's brief into a **contract** (one Markdown file per assertion, each saying
what must be observably true) and a **task list**, and from then on it plans, dispatches
and judges; it does not do the tasks. The runtime (`controller.py`, `coordinator.py`,
`storage.py`) keeps the books:

- A task is `work`, `validate` or `gate`. Every assertion belongs to exactly one work task.
  Each work or validate task is run by a fresh agent that gets a rendered prompt
  (assignment, contract files, skill, project memory) and returns a handoff: `done`,
  `report`, and for a validator one verdict per assertion.
- A gate clears only when each of its assertions was passed by every validator upstream of
  it that was assigned that assertion. Every gate, cleared or failed, stops the runtime with
  an **attention item**; so does a failed work task and any task that asks for attention.
  The orchestrator answers each item with a decision (`continue`, `patch`, `retry`,
  `next_mission`, `abort`), which the runtime records under `decisions/`.
- When nothing is left to run, the orchestrator asks for closure. A **terminal reviewer**, a
  fresh agent given only the brief and the workspace, either finds no gap (the mission is
  `done`) or reports gaps as a new attention item.
- All state is files under `$ZENITH_HOME/projects/<project>/`. `.zenith/` is the record
  every role may read: brief, `AGENTS.md`, `MEMORY.md`, skills, decisions, and per mission
  the contract, attempt reports and closeout. `.zenith-runtime/` holds the runtime's cursors.

In a stock install the orchestrator reaches the runtime through an MCP server
(`zenith-server`), and the runtime starts workers, validators and the reviewer itself
through an ACP adapter (`acp_runner.py`).

## What the bridge is

The controller takes two objects that start agents: a dispatcher and a terminal reviewer.
The bridge supplies its own pair and changes nothing else. Its dispatcher writes the task's
prompt, rendered by Zenith's own renderer, to a file and stops the runtime. The host
session gives the prompt to a fresh agent and hands the agent's result back; the runtime
then picks it up through its own resume path (the attempt file of a task marked running).
The terminal review works the same way. No process runs between commands.

The bridge imports the controller (and through it the coordinator), the dispatcher types,
storage, models, assets, config and the prompt renderer in `acp_runner`. None of them
imports `fastmcp`, and the bridge starts no ACP adapter, so it runs where neither is
installed.

## Using it

`zenith-host <command>` or `python -m zenith_harness.host_bridge <command>`. **Tried**: the
module form from a source checkout (`PYTHONPATH=zenith/src`, Python 3.13 with pydantic,
jinja2, pyyaml and click). **Not tried**: the `zenith-host` console script, because the
package could not be installed where this was written. `ZENITH_HOME` (default `~/.zenith`)
and `ZENITH_MAX_PARALLEL_NODES` (default 4) apply as in a stock install.

1. `start <workspace> <brief.md>` prints the project id. Write the contract files
   (`.zenith/missions/mission-001/contract/<ID>.md`), `AGENTS.md` and skills, then
   `plan <project> <tasks.json>` with `{"tasks": [{"id", "type", "body", "targets",
   "skill", "depends_on"}]}`.
2. `advance <project>` prints either Zenith's envelope (for example state
   `attention_needed`) or `{"host_pending": [rows]}`. A row has `task`, `type`, `targets`,
   `mission`, `spawn_ts`, `prompt` (a file), `has_result` and `in_flight`.
3. Start one fresh agent per row on its prompt file, except a row marked `in_flight` (its
   agent is still at work) or `has_result` (it only needs another `advance`). The prompt
   ends by telling the agent to call `end_node`. That tool does not exist on this path:
   tell the agent to end its reply with the same fields as one JSON object, for work
   `{"done", "report", "request_attention"}`, for validation `{"done", "report", "items":
   [{"item_id", "passed"}], "passed", "request_attention"}`.
4. When an agent finishes, save its JSON to a file, `handoff <project> <task> <file>`, and
   `advance` again. Results may arrive in any order and at any time, also while an
   attention item is open. A result is refused if it is malformed or if its task has no
   agent at work.
5. On `attention_needed`, read the items and answer all of them:
   `decide <project> <decisions.json>` with `[{"item_id", "action", "justification",
   "patch"}]` (`patch` only for that action; write new contract files before a patch that
   adds assertions).
6. When `advance` returns `mission_running` with no rows, `end <project>` prints the
   terminal-review row. Give its prompt to a fresh agent, save `{"done", "report"}` with
   `review <project> <file>`, and `end` again: the mission is `done`, or the reviewer's
   gaps come back as an attention item. `end` is refused while an agent is still at work.

`inspect <project>` shows state and task list at any time. A refused call exits 1 and
prints `tool_error` (Zenith refused) or `host_error` (the bridge refused).

## How mission 001 ran on it

The orchestrator was one Claude Code session. The machine had uv, Node 22 and the `claude`
CLI, but neither `fastmcp` nor the `claude-agent-acp` adapter, and no package could be
installed, so the stock MCP server and ACP launcher were **not tried**. The session ran the
loop above with the script this module was made from, and started each worker and validator
as a fresh background agent of its own. Paths below are under
`$ZENITH_HOME/projects/20261116T084820Z-brief-the-owner-s-words-2026-11-16/`.

| What | Record |
| --- | --- |
| Brief and plan | `.zenith/brief.md`, `.zenith/missions/mission-001/mission.md` |
| Contract | `.zenith/missions/mission-001/contract/` (23 files when this was written), among them `VAL-IMAGE-001.md`, `VAL-CORPUS-001.md` to `VAL-CORPUS-006.md` and, for this bridge and guide, `VAL-HARNESS-001.md` and `VAL-HARNESS-002.md` |
| Contract review | `.zenith/decisions/0002-contract-review-trail.md`: the contract was accepted after four `contract-review` passes by fresh agents (17, 7, 6 and 1 findings, every one applied) |
| A work task, its validator and its gate | attempts `2026-11-16T09-33-21Z-0001__w-image.md` and `2026-11-16T09-40-20Z__v-image.md` (`VAL-IMAGE-001: passed`) in `.zenith/missions/mission-001/attempts/`; gate `g-image` and the decision `continue` in `.zenith/decisions/003-continue.md` |
| Two validators in parallel | attempts `2026-11-16T16-29-37Z-0000__v-corpus-scrutiny.md` and `2026-11-16T16-29-37Z-0001__v-corpus-run.md`, one batch, each passing `VAL-CORPUS-001` to `005` on its own evidence; gate `g-corpus` in `.zenith/decisions/004-patch.md` (`5/5 passed` twice) |
| A patch decision | the same `004-patch.md` with `.zenith/decisions/0003-corpus-gate-and-hardening.md`: the validators' side findings became `VAL-CORPUS-006` with a fix task, a validator and a gate, and one pending task was superseded |
| A task still at work when another finished | `w-image` went out with `w-hooks-check` (`.zenith-runtime/host/2026-11-16T09-11-30Z-0001__w-image.prompt.md`), was put back when `w-hooks-check` finished first, and its attempt is the later `...09-33-21Z-0001__w-image.md` |
| A lost final message | the report in `...16-29-37Z-0000__w-corpus.md` begins by saying the worker's final message was lost in a session interruption and that the report was copied from the worker's own evidence file |

When this was written (2026-11-16, about 17:30 UTC) mission 001 was still running: three
work tasks, three validators and two gates had cleared, two work tasks were at work (one of
them wrote this guide), and the rest were pending. Its closure and terminal review had
**not** happened yet, so `end` and `review` have run only in this module's tests.

### The module is not byte-for-byte the script the mission ran on

Mission 001 ran on a script of about 190 lines kept outside this repository. Porting it
showed five faults. Each was reproduced on the script and is fixed and tested here:

1. Prompts were rendered against the project's parent directory instead of `.zenith/`. The
   memory path in every prompt named a file that does not exist, and the terminal-review
   prompt would have carried an empty user request.
2. `handoff` crashed while an attention item was open.
3. A task still at work when the runtime stopped for attention came back unmarked after the
   decision, as if it needed a second agent.
4. `end` while an agent was at work recorded that task as failed.
5. A result handed in twice was taken as the result of the task's next dispatch.

A mission started with the script was finished with the module on the same files (**tried**
on a scratch mission, not on mission 001).

## A stock install on a Linux box

None of this was tried on a box. "Here" is the machine the mission ran on.

| Step (from `zenith/README.md`) | Status |
| --- | --- |
| Python 3.11+, `uv`, Node.js 22+ | present here: Python 3.13.18, uv 0.11.32, Node 22.23.4 |
| `cd zenith && uv sync` | **not tried** with a package index. `uv sync --offline` was tried here and fails: the wheels are not cached |
| `uv run zenith --help`; console scripts `zenith`, `zenith-server`, `zenith-host` | **not tried** (no install) |
| `npm install -g @agentclientprotocol/claude-agent-acp` | **not tried**; the adapter is not on PATH here |
| `zenith init --workspace-dir <workspace> --agent claude` | **tried from source only** (`python3 -c "from zenith_harness.cli import cli; cli()" init ...`): exit 0; wrote `.mcp.json`, `.claude/agents`, `.claude/skills`, `.agents/skills` and `.claude/orchestrator_prompt.md` |
| The MCP server that `.mcp.json` names (`uv run --project <checkout> zenith-server --mode orchestrator`) | **not tried**: importing `zenith_harness.server` fails here, `fastmcp` is missing |
| Start `claude` in the workspace and have it read `.claude/orchestrator_prompt.md` | **not tried** on the stock path |
| Workers, validators and reviewer started over ACP | **not tried**. The repository's own check for it is `ZENITH_SMOKE_REAL_ACP=claude uv run pytest tests/test_smoke_real_acp.py -s`; here it is skipped |
| A Claude credential on the box | **only the owner can supply it** |

The credential: on the stock path every worker, validator and reviewer is a Claude Code
process that the adapter starts on the box, so the box needs a Claude login or key of its
own. Agents cannot create one. The box account this mission could use holds none today (its
list of secret names was read on 2026-11-16). Put the credential in the box's secret store
or log in on the box; never in a file under git. One thing to know first: `zenith init`
copies `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from the shell into the workspace's
`.mcp.json` when they are set (`RUNTIME_ENV_FORWARD_ALLOWLIST` in `cli.py`; **tried** only
with two non-secret variables of that list, which were copied), so keep that file out of
git.

With the bridge the box needs no adapter, no `fastmcp` and no credential of its own: the
agents are whatever the host session can start, under the host session's own login.

## Tests

`tests/test_host_bridge.py` drives missions through the real controller by bridge commands
only: one work task, two parallel validators, a gate and the terminal review to `done`;
results that arrive around attention stops; and refused calls. From `zenith/`:
`uv run pytest -q tests/test_host_bridge.py`, or without an install
`PYTHONPATH=src python3 -m pytest -q tests/test_host_bridge.py`.
