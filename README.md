# gofer

Polls Jira for ticket events targeting you (assignments, mentions, comments) and autonomously handles them using Claude Code sessions in isolated git worktrees. Ticket work produces branches and PRs. Mentions and comments get replies posted back to Jira.

## Prerequisites

### Jira Cloud API token

The agent authenticates to Jira via [API tokens](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/) (basic auth).

1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Click **Create API token**, give it a label (e.g. `gofer`), and copy the token.
3. The Jira account needs permission to:
   - **Browse projects** — the agent polls with `assignee = currentUser()`
   - **Add comments** — mention/comment handlers reply to tickets
   - **Create issues** is *not* required — the agent only reads and comments

### Anthropic API key

The agent uses the Anthropic API for two things:

- **Claude Code sessions** (Sonnet) — autonomous coding on ticket work, answering mentions/comments
- **Complexity gate** (Haiku) — quick classification call to assess ticket risk

1. Get an API key from https://console.anthropic.com/settings/keys
2. The key must have access to `claude-sonnet-4-6` and `claude-haiku-4-5` models.

### Claude Code CLI

The agent spawns Claude Code as a subprocess via the `claude-code-sdk`. Claude Code must be installed and available on `PATH`:

```bash
npm install -g @anthropic-ai/claude-code
```

Verify it works: `claude --version`

### Slack webhook (optional)

To receive notifications when approval is needed or a session completes:

1. Create a [Slack Incoming Webhook](https://api.slack.com/messaging/webhooks):
   - Go to https://api.slack.com/apps → **Create New App** → **From scratch**
   - Under **Incoming Webhooks**, toggle it on and click **Add New Webhook to Workspace**
   - Choose a channel and copy the webhook URL
2. Add the URL to your `config.yaml` under `slack.webhook_url`. Omit the entire `slack` block to disable.

### Local git repos

Each mapped project needs a locally cloned repo. The agent creates git worktrees for isolation — it does not clone repos itself.

```bash
# Example: clone the repo you want the agent to work on
git clone git@github.com:yourorg/your-repo.git /path/to/repo
```

Repos must have a remote named `origin` for the agent to push branches and create PRs.

## Setup

```bash
uv sync
cp .env.example .env
cp config.example.yaml config.yaml
# Edit .env with your credentials
# Edit config.yaml with your project/repo mappings
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `JIRA_URL` | Jira Cloud instance URL (e.g., `https://yourorg.atlassian.net`) |
| `JIRA_EMAIL` | Email of the Jira account the agent runs as |
| `JIRA_API_TOKEN` | API token from [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `ANTHROPIC_API_KEY` | Anthropic API key from [console.anthropic.com](https://console.anthropic.com/settings/keys) |

### Configuration

Edit your `config.yaml` (copied from `config.example.yaml`) to map Jira projects to local repos and configure gate thresholds:

```yaml
poll_interval: 60

projects:
  PROJ:
    default:
      repo: /path/to/repo
      branch: main
    components:
      frontend:
        repo: /path/to/frontend-repo
        branch: develop

gates:
  require_approval_above: medium

concurrency:
  max_parallel_sessions: 3
  session_timeout: 3600

# Optional — omit to disable Slack notifications
slack:
  webhook_url: "https://hooks.slack.com/services/T000/B000/xxxx"
  channel: "#gofer"

approvals:
  pending_file: pending_approvals.json
  timeout: 3600
```

Projects use a `default` repo mapping with optional `components` for component-level routing. The old flat format (`PROJ: { repo: ..., branch: ... }`) is auto-migrated.

## Usage

```bash
# Start polling (default subcommand)
uv run gofer --config config.yaml

# Override poll interval
uv run gofer run --interval 30

# Verbose logging
uv run gofer -v

# Log to file (for daemon mode)
uv run gofer --log-file /path/to/gofer.log

# Approve a pending ticket (daemon must be running)
uv run gofer approve PROJ-123

# Reject a pending ticket
uv run gofer reject PROJ-123
```

Stop with `Ctrl+C` — the agent shuts down gracefully, cancelling active sessions.

### Batch mode (`gofer do`)

One-shot mode that fetches all your open tickets on a project and works them in parallel:

```bash
# Work all your open tickets on PROJ
uv run gofer do PROJ

# Custom JQL query
uv run gofer do --jql 'project=PROJ AND status="In Progress"'

# Override concurrency limit
uv run gofer do PROJ --max-parallel 2

# Dry run — list matching tickets without working them
uv run gofer do PROJ --dry-run
```

In a TTY, `gofer do` displays a Rich live table showing per-ticket progress. Log output is coordinated through the same Rich console so logs appear above the table without corrupting the display. In non-TTY mode (e.g. piped to a file), plain status lines are printed to stderr instead.

### Running as a launchd daemon

A template plist is provided at `com.datashaman.gofer.plist`. To install:

```bash
# Edit the plist — replace /Users/YOU with your home directory
cp com.datashaman.gofer.plist ~/Library/LaunchAgents/
mkdir -p ~/Library/Logs/gofer

# Load and start
launchctl load ~/Library/LaunchAgents/com.datashaman.gofer.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.datashaman.gofer.plist
```

## Architecture

```
Poll Jira (JQL)
  -> Detect events targeting you
  -> Classify event type (assigned, status_changed, mentioned, commented, labeled)
  -> Dispatch to registered handler (@handles decorator)
  -> Handler processes the event:
     - ticket_work: resolve repo -> worktree -> complexity gate -> approval -> Claude session -> commit/push/PR -> Slack
     - mention: resolve repo -> Claude session -> reply posted as Jira comment
     - comment: resolve repo -> Claude session -> reply posted as Jira comment (or skip if informational)
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

### Multi-repo resolution

Repo mapping is resolved per-event via `resolve_repo()`:

1. **Component match** — `projects[project].components[component]` if the issue has a component
2. **Project default** — `projects[project].default` as fallback
3. **Not configured** — logs a warning and skips the event

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

### Approval flow

When approval is required, the agent writes an entry to `pending_approvals.json` and polls for a decision. In daemon mode (no interactive terminal), use the CLI subcommands:

```bash
uv run gofer approve PROJ-123
uv run gofer reject PROJ-123
```

Approvals time out after `approvals.timeout` seconds (default: 3600) and auto-reject.

### Slack notifications

When `slack.webhook_url` is configured, the agent posts notifications for:
- **Approval needed** — ticket key, complexity, risk, reasons, and approve/reject instructions
- **Session completed** — ticket key, success/failure, turn count, cost

Omit the `slack` block from your `config.yaml` to disable (no errors, just debug logs).

### Self-reply guards

All handlers skip comments authored by the agent's own `JIRA_EMAIL` to prevent infinite reply loops. The comment handler also defers to the mention handler when a comment contains a mention.

### Project structure

```
src/gofer/
├── main.py          # Entry point: poll loop, signal handling, CLI (run/approve/reject)
├── config.py        # .env (secrets) + config.yaml (mappings, gitignored) -> Settings
├── models.py        # JiraEvent, GateResult (Pydantic v2)
├── events.py        # Event classification from issue diffs
├── batch.py         # Batch orchestrator: fetch_tickets() + run_batch() for `gofer do`
├── progress.py      # Rich Live progress table for batch mode + log coordination
├── dispatcher.py    # @handles() decorator + dispatch() with error isolation
├── poller.py        # JQL polling with change detection
├── jira_client.py   # Shared JIRA client singleton + async add_comment()
├── session.py       # SessionManager: semaphore-throttled Claude Code sessions
├── worktree.py      # Git worktree lifecycle (create/remove, with timeouts)
├── gate.py          # Two-stage complexity gate (heuristics + Claude judgment)
├── approval.py      # File-based approval queue (pending_approvals.json)
├── repo_resolver.py # Component -> default repo resolution
├── repo_selector.py # Claude-based selection when component maps to multiple repos
├── slack_client.py  # Async Slack webhook poster + format helpers
└── handlers/
    ├── ticket_work.py   # assigned_to_me, status_changed -> worktree + PR + Slack
    ├── mention.py       # mentioned -> Claude session -> Jira reply
    └── comment.py       # commented -> Claude session -> Jira reply (or skip)
```

## Roadmap

1. ~~**Phase 1** -- Core plumbing: polling, events, dispatcher, config~~
2. ~~**Phase 2** -- Worktree + Claude Code session management~~
3. ~~**Phase 3** -- Complexity gate: heuristics + Claude judgment, terminal approval~~
4. ~~**Phase 4** -- Mention & comment handlers + Jira reply~~
5. ~~**Phase 5** -- Polish: error handling, logging, launchd plist~~
6. ~~**Phase 6** -- Extensions: Slack notifications, daemon-mode approval, multi-repo~~
