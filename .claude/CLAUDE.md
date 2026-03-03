# gofer

Python project using `uv` for package management. Source lives in `src/gofer/`.

## Commands

- `uv sync` — install dependencies
- `uv run gofer --help` — CLI usage
- `uv run gofer --config config.yaml` — start polling (config.yaml is gitignored; copy from config.example.yaml)
- `uv run gofer run --interval 30` — start polling with interval override
- `uv run gofer --log-file /path/to/file.log` — log to file (daemon mode)
- `uv run gofer approve PROJ-123` — approve a pending ticket
- `uv run gofer reject PROJ-123` — reject a pending ticket
- `uv run gofer do PROJ` — batch work all your open tickets on project PROJ
- `uv run gofer do --jql 'project=PROJ AND ...'` — batch work with custom JQL
- `uv run gofer do PROJ --max-parallel 2` — override concurrency limit
- `uv run gofer do PROJ --dry-run` — list matching tickets without working them

## Project layout

```
src/gofer/
├── main.py          # Entry point, poll loop, CLI (run/approve/reject/do subcommands)
├── config.py        # EnvSettings (pydantic-settings) + YamlConfig → Settings
├── models.py        # JiraEvent, GateResult (Pydantic v2)
├── events.py        # classify_changes() + build_event_from_issue() — event construction
├── batch.py         # Batch orchestrator: fetch_tickets() + run_batch() for `gofer do`
├── dispatcher.py    # @handles() decorator registry + async dispatch() with error isolation
├── poller.py        # JiraPoller — JQL polling, tracks issue state diffs
├── jira_client.py   # Shared JIRA client singleton + async add_comment() helper
├── session.py       # SessionManager (semaphore-throttled) + SessionResult + singleton accessors
├── worktree.py      # Git worktree create/remove with subprocess timeouts
├── gate.py          # Two-stage complexity gate (heuristics skip Stage 2 if flagged)
├── approval.py      # File-based approval queue (pending_approvals.json) + set_decision()
├── repo_resolver.py # resolve_repo(): component → default fallback repo resolution (returns list)
├── repo_selector.py # select_repos(): Claude-based selection when component maps to multiple repos
├── slack_client.py  # Async Slack webhook poster (httpx) + format helpers
└── handlers/        # @handles-decorated async functions (imported at startup)
    ├── ticket_work.py   # assigned_to_me, status_changed → worktree + gate + Claude → PR + Slack
    ├── mention.py       # mentioned → Claude session → Jira comment reply
    └── comment.py       # commented → Claude session → Jira comment reply (or skip)
```

## Key patterns

- **Decorator-based dispatch**: Handlers register via `@handles("event_type")` in `dispatcher.py`. Importing `handlers/__init__.py` triggers all registrations. Handler exceptions are caught per-handler so one failure doesn't abort the poll batch.
- **Singletons**: `jira_client.py` has `init_jira_client()`/`get_jira_client()`. `session.py` has `init_session_manager()`/`get_session_manager()`. Both initialized once in `main.py` before the poll loop.
- **Config**: Secrets in `.env` (pydantic-settings), mappings in `config.yaml` (PyYAML, gitignored — template at `config.example.yaml`). Both merge into a single `Settings` object via `load_settings()`.
- **Async**: Main loop is async. Jira client and git commands are sync, wrapped with `run_in_executor` or `asyncio.create_subprocess_exec`.
- **Event classification**: `events.classify_changes(issue, previous_state, my_email)` diffs raw Jira issue dicts to produce typed `JiraEvent` objects.
- **Self-reply guards**: All handlers skip comments authored by the agent's own email. Comment handler defers to mention handler when a mention is detected.
- **Response capture**: `SessionResult.response_text` captures the last assistant message text, used by mention/comment handlers to post replies to Jira.
- **Repo resolution**: `repo_resolver.resolve_repo()` resolves component → default fallback, returning a `list[RepoMapping]`. `repo_selector.select_repos()` narrows multi-repo candidates via a lightweight Claude call (skipped for single-repo). All handlers use these instead of direct dict lookups.
- **File-based approval**: `approval.py` writes pending entries to JSON, polls for decisions. CLI `approve`/`reject` subcommands call `set_decision()`.
- **Slack notifications**: `slack_client.post_slack()` is a no-op when `slack` config is absent. Callers don't need conditionals.
- **Config migration**: `YamlConfig` has a `model_validator` that auto-migrates old flat project format (`{ repo, branch }`) to new `ProjectConfig` format. `ProjectConfig` has its own validator that normalizes single `RepoMapping` dicts into one-element lists for both `default` and `components`.
- **Batch mode**: `gofer do` is a one-shot batch runner. `batch.py` fetches tickets via JQL, converts to events with `build_event_from_issue()`, and fires them all through `handle_ticket_work()` via `asyncio.gather()`. The `SessionManager` semaphore handles throttling.

## Conventions

- Python 3.13+, `from __future__ import annotations` in all modules
- Pydantic v2 models with `BaseModel`
- Type hints everywhere
- Logging via `logging.getLogger(__name__)`; PID included in log format
- No test framework yet — verify manually with `uv run gofer`
