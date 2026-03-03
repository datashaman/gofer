# jira-agent

Python project using `uv` for package management. Source lives in `src/jira_agent/`.

## Commands

- `uv sync` — install dependencies
- `uv run jira-agent --help` — CLI usage
- `uv run jira-agent --config config.yaml` — start polling

## Project layout

```
src/jira_agent/
├── main.py          # Entry point, async poll loop, SIGINT handling
├── config.py        # EnvSettings (pydantic-settings) + YamlConfig → Settings
├── models.py        # JiraEvent, GateResult, TicketContext (Pydantic v2)
├── events.py        # classify_changes() — compares issue state diffs
├── dispatcher.py    # @handles() decorator registry + async dispatch()
├── poller.py        # JiraPoller — JQL polling, tracks updated timestamps
└── handlers/        # @handles-decorated async functions (imported at startup)
    ├── ticket_work.py   # assigned_to_me, status_changed
    ├── mention.py       # mentioned
    └── comment.py       # commented
```

## Key patterns

- **Decorator-based dispatch**: Handlers register via `@handles("event_type")` in `dispatcher.py`. Importing `handlers/__init__.py` triggers all registrations.
- **Config**: Secrets in `.env` (pydantic-settings), mappings in `config.yaml` (PyYAML). Both merge into a single `Settings` object via `load_settings()`.
- **Async**: Main loop is async. Jira client is sync, wrapped with `run_in_executor`.
- **Event classification**: `events.classify_changes(issue, previous_state, my_email)` diffs raw Jira issue dicts to produce typed `JiraEvent` objects.

## Conventions

- Python 3.13+, `from __future__ import annotations` in all modules
- Pydantic v2 models with `BaseModel`
- Type hints everywhere
- Logging via `logging.getLogger(__name__)`
- No test framework yet — verify manually with `uv run jira-agent`
