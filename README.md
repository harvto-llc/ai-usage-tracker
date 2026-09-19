# Usage Tracker (ai-cur desktop client)

## Download

One installer per platform. Each one bundles its own backend (no Python, no
terminal), runs it on `127.0.0.1` only, and starts it with your session.

| Platform | File | Install |
|----------|------|---------|
| macOS 14+ (Apple silicon and Intel) | `ai-cur-desktop-<version>.dmg` | Open the dmg, drag the app to Applications, open it. |
| Windows 10/11 (x64) | `ai-cur-desktop-setup-<version>.exe` | Run it. Installs for your user only, no admin rights. |
| Ubuntu 22.04+ / Debian (x86_64) | `ai-cur-desktop_<version>_amd64.deb` | `sudo apt install ./ai-cur-desktop_<version>_amd64.deb` |
| Other Linux (x86_64) | `ai-cur-desktop-<version>-x86_64.AppImage` | `chmod +x` it and run it once. |

All files are on the [releases page](https://github.com/harvto-llc/ai-usage-tracker/releases),
each with a `.sha256` next to it.

Good to know:

- Builds are unsigned until release signing is set up; those files end in
  `-unsigned`. On macOS, right-click the app and choose Open the first time. On
  Windows, choose "More info" and then "Run anyway" in SmartScreen.
- Linux on stock GNOME: GNOME Shell has no tray area of its own. Install and
  enable the AppIndicator extension (`gnome-shell-extension-appindicator`,
  already on Ubuntu) to see the icon. The client tells you once if it cannot
  show one; the backend keeps collecting either way.
- Browser-based quota refresh for Claude and Codex needs Node.js and Google
  Chrome on your machine. Without them, local activity, tokens and the API
  still work; those quota gauges stay empty.
- Uninstalling leaves your data in `~/.usage-tracker`.

Prefer to run from source? See [Install from source](#install-from-source) below.

## About

Usage Tracker is a local macOS menu bar app and backend for watching AI coding
tool usage across Claude, Codex, and Cursor. It combines local activity scans
with optional subscription quota scraping so the menu bar can show current
usage, model breakdowns, weekly pacing, and quota risk without opening each
provider dashboard.

The app is designed to run on your machine:

- A Python collector scans local Claude and Codex activity every minute.
- Optional subscription access scraping refreshes Claude, Codex, and Cursor
  quota gauges every five minutes.
- A FastAPI server stores snapshots in SQLite and serves the menu bar contract.
- A Swift menu bar app polls the local API, refreshes provider cookies, shows
  usage trends and paid credit state, and can send opt-in alerts for quota
  thresholds, projected exhaustion, and stale credentials.
- A local work ledger attributes sessions to your projects and tasks, and the
  menu bar hosts Work Review and Work Report windows built on it.
- The Work Activity window uses a contentless local index to search
  Claude/Codex sessions and show a read-only evidence timeline without
  persisting transcript bodies outside their original JSONL files.

## What It Tracks

Claude:

- Claude Code CLI JSONL under `~/.claude/projects`
- Claude desktop/Cowork local-agent session JSONL
- Tokens, messages, active hours, model usage, session quota, weekly quota
- Paid extra usage and credit balance state, including exhaustion, when the
  provider exposes them

Codex:

- Local Codex SQLite and session JSONL under `~/.codex`
- Threads, sessions, tokens, model usage, session quota, weekly quota
- Paid credit balance, banked reset usage, and usage by local surface (CLI,
  IDE, web)

Cursor:

- Subscription usage from Cursor's web/API surface
- Requests, limits, reset state, and model breakdown when available

## Work Tracking

The work ledger attributes local Claude and Codex activity to your projects
and tasks, entirely on this machine:

- Register projects from local folders. Git repositories are discovered and
  indexed incrementally so commits, branches, and file paths can back
  attribution.
- Create projects and tasks as work items, and set one as the active item so
  new sessions are tagged live while you work.
- Attribution infers which project or task each session served from session
  content, working folders, and Git history. Inferred links stay reviewable
  and correctable.
- The Work Review window groups related sessions into families, provides an
  assignment picker for quick corrections, and tracks review state.
- The Work Report window rolls up projects, tasks, repositories, Git
  activity, attributed active time, estimated cost, and usage drivers per
  day or week.
- Cost-to-outcome ratios (cost per pull request, per commit, and per changed
  line) are reported for every rollup, next to the pricing coverage that says
  how much of that cost was actually priced. A ratio is shown only when it can
  be stated honestly: it is blank, never zero, when there is nothing to divide
  by, when the cost side is unpriced, or when a rollup has no AI usage at all
  (a zero cost there means the cost was never measured, not that the work was
  free). Cost per changed line counts lines touched rather than lines landed,
  because working-tree edits are observed as you make them.

## Access Modes

The collector has two access modes:

| Mode | Use When | Behavior |
|---|---|---|
| `subscription` | You use Claude/Codex/Cursor logged-in subscription plans. | Local scans plus web quota scraping. |
| `api` | You use Bedrock, Vertex, OpenAI enterprise, or API-key based access. | Local scans only; no web quota scraping. |

Run one collector cycle with:

```bash
python3 -m src.collector --access subscription
python3 -m src.collector --access api
```

`subscription` is the default. Old values `--mode full` and `--mode local`
still work as compatibility aliases, but new installs should use
`--access subscription` or `--access api`.

## Requirements

- macOS 14 or newer for the Swift menu bar app
- Python 3.11 or newer
- Swift 5.9 or newer
- Nothing else: the launchd installer finds your `python3` and `uvicorn` on PATH,
  or set `PYTHON=` and `UVICORN=` when running it.

## Install from source

Clone the repo:

```bash
git clone https://github.com/harvto-llc/ai-usage-tracker.git
cd ai-usage-tracker
```

Install backend dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create local state folders:

```bash
mkdir -p ~/.usage-tracker/logs
```

Create a bearer secret and write both config files:

```bash
SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

cat > .env <<EOF
export USAGE_TRACKER_SECRET=$SECRET
export USAGE_TRACKER_ACCESS=subscription
EOF

cat > ~/.usage-tracker/config <<EOF
USAGE_TRACKER_SECRET=$SECRET
EOF
```

Use `USAGE_TRACKER_ACCESS=api` instead if you do not want subscription quota
scraping.

Optional: copy and edit the plan config:

```bash
cp plans.example.toml ~/.usage-tracker/plans.toml
```

`plans.toml` is used for weekly budget forecasts and self-imposed quotas.

Build the menu bar app:

```bash
cd UsageMenuBar
swift build -c release
cd ..
```

## Run Manually

Start the API:

```bash
set -a
source .env
set +a
uvicorn src.api:app --host 127.0.0.1 --port 8000
```

In another shell, run one collector cycle:

```bash
set -a
source .env
set +a
python3 -m src.collector --access subscription
```

Launch the menu bar app:

```bash
./UsageMenuBar/.build/arm64-apple-macosx/release/UsageMenuBar
```

The menu bar app polls `http://localhost:8000/stats` and
`http://localhost:8000/budget/weekly` using the bearer token from
`~/.usage-tracker/config`. The Work Review, Work Report, and Work Activity
windows use the `/work-ledger/*` endpoints with the same token.

## Provider Access

The menu bar reads Claude and Codex sessions from supported local browsers and
stores refreshed web credentials in the macOS login Keychain under the service
`com.harvto.usage-tracker.web-cookie`. Settings shows the access state for each
provider and includes sign-in and forced-refresh actions.

On platforms without Keychain access, the API keeps the compatibility behavior
of writing provider cookies to mode-`0600` files under `~/.usage-tracker`.
Set `USAGE_TRACKER_KEYCHAIN=0` to select that fallback explicitly. Existing
environment variables and cookie files remain readable during migration.

## Run With launchd

`launchd/*.plist.template` are rendered for your checkout path and your user by the
installer, which also creates the log folder and starts the jobs. Re-run it after
pulling changes; it restarts what is loaded.

```bash
scripts/install-launchd.sh
```

The menu bar job is installed only when `UsageMenuBar/.build/release/UsageMenuBar`
exists, so build the app first if you want it started at login. To remove the jobs:

```bash
scripts/install-launchd.sh --uninstall
```

Logs, all under `~/.usage-tracker/logs/`: `api.log`, `collector.log`, `menubar.log`
and their `.err.log` companions.

Per-project settings are optional and live in `~/.usage-tracker/projects.toml`:

```toml
[repos]      # where a project's checkout is, when a session names no path
my-app = "~/code/my-app"

[aliases]    # fold a worktree or fork folder into its project
my-app-hotfix = "my-app"
```

## Self-Imposed Quotas

Subscription quota gauges come from provider dashboards. API-based setups do
not have those dashboards, so you can define local caps in
`~/.usage-tracker/plans.toml`:

```toml
[claude.self_quota]
window_hours = 5
weekly_days = 7
session_cap_tokens = 44_000_000
weekly_cap_tokens = 300_000_000

[codex.self_quota]
window_hours = 5
weekly_days = 7
session_cap_usd = 10.0
weekly_cap_usd = 75.0
```

Claude self-quota usage is measured from Claude JSONL. Codex self-quota usage
is measured from Codex session JSONL. Caps can be token-based or cost-based,
and pricing can be overridden per model prefix in `plans.toml`.

Scraped subscription quota wins while it is live. Self-imposed quota fills in
when scraping is off or stale.

## Quota Cost Estimates

For Claude and Codex subscriptions, the menu prices locally observed tokens
inside each provider-reported quota window. Claude uses published standard API
rates. Codex uses the published flexible-usage credit card and converts credits
at 25 credits per USD. This is the replacement value of consumed included
usage, not an additional charge.

Provider quota percentages are not converted directly because model, context,
reasoning, tools, and caching all affect quota consumption. Estimates report
partial pricing coverage when an observed model has no published rate. Codex
Spark currently remains unpriced for this reason.

## API

All endpoints except `/health` require:

```text
Authorization: Bearer $USAGE_TRACKER_SECRET
```

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness probe |
| `/stats` | GET | Menu bar payload, cached for 15 seconds |
| `/budget/weekly` | GET | Weekly forecast from `plans.toml` |
| `/cc/report` | POST | Collector ingest |
| `/sentinel/report` | POST | Cookie refresh from the menu bar sentinel |
| `/usage/explanation?provider=claude&period=day` | GET | Local category attribution for Claude or Codex, day or week |
| `/work-ledger/status` | GET | Work ledger diagnostics and index coverage |
| `/work-ledger/settings` | PUT | Update work ledger settings |
| `/work-ledger/repositories` | GET | Discovered local Git repositories |
| `/work-ledger/projects/from-folder` | POST | Register a project from a local folder |
| `/work-ledger/items` | GET, POST | List or create projects and tasks |
| `/work-ledger/items/{item_id}` | DELETE | Archive a work item |
| `/work-ledger/active` | GET, PUT | Read or set the active work item for live tagging |
| `/work-ledger/intervals` | GET | Attributed active-time intervals |
| `/work-ledger/refresh` | POST | Start a background attribution refresh |
| `/work-ledger/sessions` | GET | Sessions with attribution and review state |
| `/work-ledger/events` | GET | Work ledger event log |
| `/work-ledger/reconciliation` | GET | Attribution reconciliation summary |
| `/work-ledger/report?period=week` | GET | Project, task, repository, Git, cost, cost-to-outcome ratios, and usage-driver rollups. Ratio fields are `null`, never `0`, when they cannot be stated honestly; do not coerce `null` to zero |
| `/work-ledger/sessions/{id}/evidence` | GET | Bounded prompt, response, tool, file, command, and test timeline for one local session |
| `/work-ledger/session-search?q=term` | GET | Indexed cross-provider search with bounded fallback for active session files |
| `/work-ledger/session-search/status` | GET | Contentless session-search index health and coverage |

## Command Line

The read-only CLI uses the same local API and bearer token as the menu bar:

```bash
python3 -m src.cli usage
python3 -m src.cli cost --period week --provider codex
python3 -m src.cli history --days 30 --provider claude
python3 -m src.cli explain --period week --provider codex
python3 -m src.cli --json usage --provider codex
```

`usage` reports provider quota windows. `cost`, `history`, and `explain` report locally
observed tokens, estimated replacement value, estimated credits where a rate
exists, and pricing coverage. `explain` also separates instructions, files,
shell, web, connectors, app control, subagents, and model output. Dollar values
are estimates, not billed spend or provider quota debits.

## Release Builds

`.github/workflows/release.yml` builds and smoke-tests all three installers on
every pull request and on `v*` tags, and attaches them to a draft release on
tags. Each platform can also be built by hand:

```bash
# macOS: freeze the backend once per architecture, then build the dmg
python3 packaging/backend/build_backend.py --dist dist/backend-arm64
BACKEND_ARM64=dist/backend-arm64/aicur-backend BACKEND_X86_64=<x86_64 build> \
  VERSION=0.2.0 ./scripts/make_dmg.sh
./scripts/smoke_macos.sh dist/ai-cur-desktop-0.2.0.dmg

# Linux (on Ubuntu): .deb and AppImage
python3 packaging/backend/build_backend.py --dist dist/linux
BACKEND_DIR=dist/linux/aicur-backend VERSION=0.2.0 ./scripts/build_linux.sh
```

Windows uses the same `build_backend.py` (plus `--target windows-tray`) and
`packaging/windows/aicur-desktop.iss` with Inno Setup 6. Signing and
notarization are optional inputs; see `docs/installers.md` for what each script
checks and what has been proven where.

### Menu bar app only (zip)

The older zip packaging builds just the Swift app, without a backend. It is
kept as the release workflow's negative control. Build a versioned local
`.app`, zip archive, update manifest, and Homebrew cask:

```bash
VERSION=0.1.0 BUILD_NUMBER=1 ./scripts/package_macos_app.sh
./scripts/verify_macos_app.sh "dist/Usage Tracker.app"
```

Local builds use an ad-hoc signature. Public release builds should provide a
Developer ID identity and a configured `notarytool` profile:

```bash
SIGNING_IDENTITY="Developer ID Application: Example (TEAMID)" \
NOTARY_PROFILE="usage-tracker-notary" \
VERSION=0.1.0 BUILD_NUMBER=1 ./scripts/package_macos_app.sh
```

The generated cask is written to `dist/homebrew/usage-tracker.rb` and expects
the zip archive at the matching GitHub release URL. The cask installs the menu
app; the local API and collector still need to be configured and running.
Settings checks GitHub Releases for a newer stable version and opens the release
page for user-controlled installation.

Set `RUN_ACCESSIBILITY_TESTS=1` when running the verifier on a Mac whose terminal
has Accessibility permission. The verifier checks app metadata, signature,
launchability, and menu-bar accessibility.

## Development

Run backend tests:

```bash
python3 -m pytest tests/ -q
```

Build the Swift app:

```bash
cd UsageMenuBar
swift build -c release
```

Useful checks before publishing changes:

```bash
python3 -m src.collector --help
python3 -m pytest tests/ -q
cd UsageMenuBar && swift build -c release
```

Generated files such as `.env`, `claude_usage.db`, `__pycache__`, pytest
caches, and Swift `.build` output are intentionally ignored.
