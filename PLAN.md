# gofer

## Objective

Automate the Jira-ticket-to-PR workflow. Poll Jira for events targeting you (assignments, mentions, comments), then spawn a full Claude Code session per ticket in an isolated git worktree that: reads the ticket, researches the codebase, plans the implementation, executes it, commits, pushes, and creates a PR. You review and merge.

## How It Works

```
Poll Jira (JQL)
  → Detect events targeting you
  → Classify event type
  → Dispatch to registered handler (decorator-based)
  → For ticket work:
      1. Create git worktree for the ticket
      2. Spawn Claude Code session (claude-agent-sdk) in that worktree
      3. Claude reads Jira ticket, researches codebase, creates plan
      4. If complex: pause for your approval (terminal prompt)
      5. Execute plan, commit, push, create PR
      6. You review & merge
```

## Complexity Gate

Two-stage gate determines whether plan approval is required:

**Stage 1 — Deterministic heuristics** (auto-require approval if any true):
- Labels: `epic`, `security`, `migration`, `breaking-change`, `infra`, `refactor`
- Components: `auth`, `billing`, `payments`, `permissions`, `data`
- Files likely impacted: `db/migrations/`, `terraform/`, `k8s/`, `infra/`, `policies/`
- Estimated diff > N files or > M LOC
- External dependencies ("depends on X team", "waiting on API")

**Stage 2 — Claude judgment** (after quick repo scan):
- Outputs: `complexity: low|medium|high`, `risk: low|medium|high`
- Gate rule: require approval if complexity != low OR risk != low

## Approval UX

- **Interactive mode**: Terminal prompt — Approve / Request changes / Reject
- **Daemon mode** (later): Write to `pending_approvals.json` + macOS desktop notification; Slack later

## Structure

```
gofer/
├── pyproject.toml
├── .env.example
├── config.yaml                 # Jira project → repo mapping, heuristic thresholds
├── gofer/
│   ├── __init__.py
│   ├── main.py                 # Entry point: poll loop, concurrency control, shutdown
│   ├── config.py               # Load .env + config.yaml, settings model
│   ├── poller.py               # JQL polling + change detection
│   ├── events.py               # JiraEvent model + event classification
│   ├── dispatcher.py           # Decorator-based event → handler routing
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── ticket_work.py      # Main handler: worktree + Claude Code session
│   │   ├── mention.py          # Handle @mentions (triage, quick response)
│   │   └── comment.py          # Handle comment threads
│   ├── gate.py                 # Complexity gate (heuristics + Claude judgment)
│   ├── worktree.py             # Git worktree lifecycle management
│   ├── session.py              # Claude Code SDK session management
│   └── models.py               # Pydantic models (events, config, approvals)
└── README.md
```

## Key Components

### 1. `dispatcher.py` — Decorator-based event routing

```python
from gofer.events import JiraEvent

_handlers: dict[str, Callable] = {}

def handles(*event_types: str):
    def decorator(fn):
        for et in event_types:
            _handlers[et] = fn
        return fn
    return decorator

async def dispatch(event: JiraEvent):
    handler = _handlers.get(event.event_type)
    if handler:
        await handler(event)
```

### 2. `handlers/ticket_work.py` — The main workflow

```python
@handles("assigned_to_me", "status_in_progress")
async def on_ticket_work(event: JiraEvent):
    # 1. Resolve repo from config mapping
    repo = config.resolve_repo(event.project, event.component)

    # 2. Create worktree
    worktree = await create_worktree(repo, event.issue_key)

    # 3. Run complexity gate
    needs_approval = await check_gate(event, worktree.path)

    # 4. Spawn Claude Code session
    async with ClaudeSDKClient(options=ClaudeAgentOptions(
        cwd=worktree.path,
        model="claude-sonnet-4-6",
        system_prompt=build_system_prompt(event),
        permission_mode="bypassPermissions",
        max_turns=50,
        max_budget_usd=10.0,
    )) as client:
        if needs_approval:
            # Plan-only pass
            await client.query(f"Read Jira ticket {event.issue_key} and create a plan. Do NOT execute yet.")
            plan = await collect_response(client)
            approved = await prompt_approval(event.issue_key, plan)
            if not approved:
                return

        # Execute
        await client.query(
            f"Implement Jira ticket {event.issue_key}. "
            f"Commit, push to branch {event.issue_key}, create a PR."
        )
        await collect_response(client)

    # 5. Cleanup worktree (optional, or keep until PR merged)
```

### 3. `poller.py` — JQL polling + change detection

- Uses `jira` library with email + API token auth
- JQL: `assignee = currentUser() AND updated >= "-{interval}m"` (plus variants for mentions, comments)
- Tracks `updated` timestamps to avoid reprocessing
- Returns classified `JiraEvent` objects

### 4. `gate.py` — Complexity gate

- Stage 1: Check labels, components, file paths against heuristic rules from config
- Stage 2: Quick Claude call with ticket context → `{complexity, risk}` structured output
- Returns `bool` — whether plan approval is required

### 5. `worktree.py` — Git worktree lifecycle

- `create_worktree(repo_path, branch_name)` → creates worktree in `.worktrees/`
- `cleanup_worktree(worktree)` → removes after PR merged
- Handles branch naming: `ticket/{ISSUE_KEY}`

### 6. `config.py` — Configuration

Loads from `.env` (secrets) + `config.yaml` (mappings):

```yaml
jira:
  url: https://yourorg.atlassian.net
  project: MYPROJ
  poll_interval_minutes: 5

repos:
  MYPROJ: /Users/you/Projects/my-repo
  MYPROJ/Sync: /Users/you/Projects/sync-repo

gate:
  risky_labels: [epic, security, migration, breaking-change, infra, refactor]
  risky_components: [auth, billing, payments, permissions, data]
  risky_paths: [db/migrations/, terraform/, k8s/, infra/, policies/]
  max_auto_files: 10
  max_auto_loc: 200

concurrency:
  max_active_tickets: 2
```

## Dependencies

```toml
[project]
name = "gofer"
requires-python = ">=3.11"
dependencies = [
    "jira",
    "claude-agent-sdk",
    "pydantic",
    "python-dotenv",
    "pyyaml",
]
```

## Claude Agent SDK Reference

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock

# Spawn a full Claude Code session
async with ClaudeSDKClient(options=ClaudeAgentOptions(
    cwd="/path/to/worktree",
    model="claude-sonnet-4-6",
    system_prompt="You are working on Jira ticket PROJ-123...",
    permission_mode="bypassPermissions",
    allowed_tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
    max_turns=50,
    max_budget_usd=10.0,
)) as client:
    await client.query("Implement the feature described in the ticket.")
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
        elif isinstance(message, ResultMessage):
            print(f"Done. Cost: ${message.total_cost_usd}")
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `JIRA_URL` | Jira Cloud URL (e.g., `https://yourorg.atlassian.net`) |
| `JIRA_EMAIL` | Jira account email |
| `JIRA_API_TOKEN` | API token from id.atlassian.com |
| `ANTHROPIC_API_KEY` | Claude API key |

## Runtime

- **Local first**: Long-running tmux session or macOS launchd agent
- **Concurrency**: 1–2 active tickets max
- **Graceful shutdown**: SIGINT stops polling, waits for active sessions to finish

## Phases

1. **Core plumbing**: Poller, event model, dispatcher, config loading
2. **Worktree + session management**: Create worktrees, spawn Claude Code, collect output
3. **Complexity gate**: Heuristics + Claude judgment, terminal approval prompt
4. **Handlers**: ticket_work (full cycle), mention, comment
5. **Polish**: Error handling, logging, cleanup, launchd plist
6. **Extensions**: Slack notifications, daemon mode, multi-repo mapping with fallback prompts
