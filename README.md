# jira-agent

Polls Jira for ticket events targeting you (assignments, mentions, comments) and dispatches them to handler functions. In later phases, handlers will spawn Claude Code sessions in isolated git worktrees to autonomously implement tickets and create PRs.

## Setup

```bash
uv sync
cp .env.example .env
# Edit .env with your credentials
```

### Required environment variables

| Variable | Description |
|----------|-------------|
| `JIRA_URL` | Jira Cloud URL (e.g., `https://yourorg.atlassian.net`) |
| `JIRA_EMAIL` | Jira account email |
| `JIRA_API_TOKEN` | API token from [id.atlassian.com](https://id.atlassian.com) |
| `ANTHROPIC_API_KEY` | Claude API key |

### Configuration

Edit `config.yaml` to map Jira projects to local repos and configure gate thresholds:

```yaml
poll_interval: 60

projects:
  PROJ:
    repo: /path/to/repo
    branch: main

gates:
  max_complexity: medium
  max_risk: medium
  require_approval_above: medium

concurrency:
  max_parallel_sessions: 3
  session_timeout: 3600
```

## Usage

```bash
# Start polling
uv run jira-agent --config config.yaml

# Override poll interval
uv run jira-agent --interval 30

# Verbose logging
uv run jira-agent -v
```

Stop with `Ctrl+C` — the agent shuts down gracefully.

## Architecture

```
Poll Jira (JQL)
  → Detect events targeting you
  → Classify event type (assigned, status_changed, mentioned, commented, labeled)
  → Dispatch to registered handler (@handles decorator)
  → Handler processes the event
```

### Event types

| Event | Trigger | Handler |
|-------|---------|---------|
| `assigned_to_me` | Ticket assigned to you | `ticket_work` |
| `status_changed` | Ticket status changes | `ticket_work` |
| `mentioned` | You're @mentioned | `mention` |
| `commented` | New comment on watched ticket | `comment` |
| `labeled` | Labels changed | (fallback: `updated`) |
| `updated` | Any other tracked field change | (no handler yet) |

### Project structure

```
src/jira_agent/
├── main.py          # Entry point: poll loop, signal handling, CLI
├── config.py        # .env (secrets) + config.yaml (mappings) → Settings
├── models.py        # JiraEvent, GateResult, TicketContext
├── events.py        # Event classification from issue diffs
├── dispatcher.py    # @handles() decorator + dispatch()
├── poller.py        # JQL polling with change detection
└── handlers/
    ├── ticket_work.py   # assigned_to_me, status_changed (stub)
    ├── mention.py       # mentioned (stub)
    └── comment.py       # commented (stub)
```

## Roadmap

1. **Phase 1** — Core plumbing (done): polling, events, dispatcher, config
2. **Phase 2** — Worktree + Claude Code session management
3. **Phase 3** — Complexity gate (heuristics + Claude judgment)
4. **Phase 4** — Full ticket-to-PR handler workflow
5. **Phase 5** — Polish: error handling, logging, launchd
6. **Phase 6** — Extensions: Slack, daemon mode, multi-repo
