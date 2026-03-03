# jira-agent

Polls Jira for ticket events targeting you (assignments, mentions, comments) and autonomously handles them using Claude Code sessions in isolated git worktrees. Ticket work produces branches and PRs. Mentions and comments get replies posted back to Jira.

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

# Log to file (for daemon mode)
uv run jira-agent --log-file /path/to/jira-agent.log
```

Stop with `Ctrl+C` — the agent shuts down gracefully, cancelling active sessions.

### Running as a launchd daemon

A template plist is provided at `com.datashaman.jira-agent.plist`. To install:

```bash
# Edit the plist — replace /Users/YOU with your home directory
cp com.datashaman.jira-agent.plist ~/Library/LaunchAgents/
mkdir -p ~/Library/Logs/jira-agent

# Load and start
launchctl load ~/Library/LaunchAgents/com.datashaman.jira-agent.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.datashaman.jira-agent.plist
```

## Architecture

```
Poll Jira (JQL)
  -> Detect events targeting you
  -> Classify event type (assigned, status_changed, mentioned, commented, labeled)
  -> Dispatch to registered handler (@handles decorator)
  -> Handler processes the event:
     - ticket_work: worktree -> complexity gate -> Claude session -> commit/push/PR
     - mention: Claude session -> reply posted as Jira comment
     - comment: Claude session -> reply posted as Jira comment (or skip if informational)
```

### Event types

| Event | Trigger | Handler | Behavior |
|-------|---------|---------|----------|
| `assigned_to_me` | Ticket assigned to you | `ticket_work` | Worktree + gate + Claude session -> PR |
| `status_changed` | Ticket status changes | `ticket_work` | Same as above |
| `mentioned` | You're @mentioned in a comment | `mention` | Claude session -> Jira comment reply |
| `commented` | New comment (not a mention) | `comment` | Claude session -> Jira comment reply (or skip) |
| `labeled` | Labels changed | (no handler) | Logged at debug level |
| `updated` | Any other tracked field change | (no handler) | Logged at debug level |

### Complexity gate

Two-stage gate for ticket work determines whether operator approval is required:

**Stage 1 -- Deterministic heuristics** (auto-require approval if any match):
- Risky labels: `epic`, `security`, `migration`, `breaking-change`, `infra`, `refactor`
- Risky components: `auth`, `billing`, `payments`, `permissions`, `data`
- Risky paths in description: `db/migrations/`, `terraform/`, `k8s/`, `infra/`, `policies/`
- External dependency keywords: `depends on`, `waiting on`, `blocked by`, `external`

If heuristics flag the ticket, Stage 2 is skipped (saves API cost).

**Stage 2 -- Claude judgment** (quick Haiku call):
- Outputs: `complexity: low|medium|high`, `risk: low|medium|high`
- Approval required if either exceeds `require_approval_above` threshold from config

### Self-reply guards

All handlers skip comments authored by the agent's own `JIRA_EMAIL` to prevent infinite reply loops. The comment handler also defers to the mention handler when a comment contains a mention.

### Project structure

```
src/jira_agent/
├── main.py          # Entry point: poll loop, signal handling, CLI
├── config.py        # .env (secrets) + config.yaml (mappings) -> Settings
├── models.py        # JiraEvent, GateResult (Pydantic v2)
├── events.py        # Event classification from issue diffs
├── dispatcher.py    # @handles() decorator + dispatch() with error isolation
├── poller.py        # JQL polling with change detection
├── jira_client.py   # Shared JIRA client singleton + async add_comment()
├── session.py       # SessionManager: semaphore-throttled Claude Code sessions
├── worktree.py      # Git worktree lifecycle (create/remove, with timeouts)
├── gate.py          # Two-stage complexity gate (heuristics + Claude judgment)
├── approval.py      # Terminal approval prompt for complex tickets
└── handlers/
    ├── ticket_work.py   # assigned_to_me, status_changed -> worktree + PR
    ├── mention.py       # mentioned -> Claude session -> Jira reply
    └── comment.py       # commented -> Claude session -> Jira reply (or skip)
```

## Roadmap

1. ~~**Phase 1** -- Core plumbing: polling, events, dispatcher, config~~
2. ~~**Phase 2** -- Worktree + Claude Code session management~~
3. ~~**Phase 3** -- Complexity gate: heuristics + Claude judgment, terminal approval~~
4. ~~**Phase 4** -- Mention & comment handlers + Jira reply~~
5. ~~**Phase 5** -- Polish: error handling, logging, launchd plist~~
6. **Phase 6** -- Extensions: Slack notifications, daemon-mode approval, multi-repo
