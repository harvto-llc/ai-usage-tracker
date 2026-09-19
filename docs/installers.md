# Installers: plan, decisions, status

Single source for the installer work on branch `feat/installers` (cut from `origin/main` at
`a1d01a9`). Plan, decisions and status live here and nowhere else; this run does not create
`PLAN.md` or `status.md` because the task charter asks for ONE file.

Nothing here is pushed, tagged or released by the agent. The supervisor pushes after the
founder's OK.

## Deliverables

| | What | State |
|-|------|-------|
| V1 | macOS dmg, bundled backend, CI build + smoke test, unsigned | built and proven locally with a stand-in app; waits for first CI run |
| V2 | Windows tray + Inno Setup per-user installer, CI smoke | not started |
| V3 | Linux tray + `.deb` + AppImage, systemd user unit, CI smoke under xvfb | not started |
| V4 | README downloads first, CHANGELOG, Homebrew cask on the dmg | not started |

## Facts this plan rests on (measured 2026-09-18/19; code cites are to `a1d01a9`)

- `scripts/package_macos_app.sh` zips only the Swift menu bar app. No backend inside.
- This Mac cannot run `swift build` through Xcode (licence not accepted, exit 69) and has no
  signing identity. `DEVELOPER_DIR=/Library/Developer/CommandLineTools` gets past the licence
  gate for `swiftc -typecheck`, `lipo` and PyInstaller, but a full app build still fails there:
  the CommandLineTools have no SwiftUI macro plugin (`external macro implementation type
  'SwiftUIMacros.StateMacro' could not be found`). The Swift app is therefore built only in CI.
- **Refuted: `macos-14` cannot build the app.** `SweetCookieKit` 0.4.0 (pinned in
  `UsageMenuBar/Package.resolved`) declares `swift-tools-version: 6.2`. Swift 6.2 ships with
  Xcode 26, and Xcode 26 does not run on macOS 14 (inference from Apple's Xcode 26 system
  requirements, confirmed or refuted by the first CI run). The workflow uses `macos-15` and
  fails loudly if it finds no `Xcode_26*.app`.
- Rosetta is not installed on this Mac (`Bad CPU type in executable (os error 86)` running an
  x86_64 Python), so the x86_64 backend is built and smoke-tested only in CI.
- The API refuses to start without `USAGE_TRACKER_SECRET` (`src/api.py:23`). The Swift app and
  `src/cli.py` read that secret from `~/.usage-tracker/config` as a `USAGE_TRACKER_SECRET=` line.
- The Swift app hard-codes `127.0.0.1:8000` / `localhost:8000` (`Sentinel.swift:8`,
  `UsageViewModel.swift:68`); the collector posts to `http://localhost:8000/cc/report`
  (`src/collector.py:39`). Port 8000 stays the bundled default.
- The collector is ONE cycle per process; launchd reruns it every 60 s (`StartInterval`).
- The main database is `<repo>/claude_usage.db` (`src/database.py:12`) and the sentinel env file
  is `<repo>/.env` (`src/api.py:688`). Inside a bundle both would resolve inside the signed,
  read-only app, so both need a data-directory override.
- A collector cycle with the API up writes `provider_metric_samples` rows even on a machine with
  no Claude or Codex history (`src/api.py:1349-1366` via `insert_provider_metric_samples`).
  Measured: a frozen `aicur-backend supervise` with an empty HOME wrote 3 rows
  (`claude|partial`, `codex|ok`, `cursor|stale`) within 4 s. That table is the smoke test's
  "collector wrote a row" oracle.

## Refutation check: can PyInstaller bundle the browser-based quota scraping?

Partly, and the part it cannot bundle is optional. Evidence:

- Browser scraping is two dependency-free Node scripts (`scripts/fetch_claude_web_usage.mjs`,
  `scripts/fetch_codex_web_analytics.mjs`, only `node:` built-in imports) that drive the user's
  installed Chrome. Python launches them with `[_find_bin("node"), helper]`
  (`src/pty_scraper.py:1128`, `:1354`).
- PyInstaller can ship the `.mjs` files as data (they resolve through
  `Path(__file__).parent.parent / "scripts"`, which maps to the bundle root when frozen). It
  cannot ship Node or Chrome.
- Both paths are already guarded: they run only when web cookies are configured
  (`claude_web_usage_configured()`, `_codex_web_request_config()`), and failures are caught and
  logged in the collector (`src/collector.py` `_scrape_claude_usage`), not fatal.

Decision (fallback): bundle the `.mjs` helpers; use the system `node` when present; when it is
absent the web quota scrape logs `failed to start` and local scans, activity and the API keep
working. Bundling Node (~40 MB per arch) is deferred and stated as a known limit in the README.

## Architecture (all three platforms)

One PyInstaller one-dir executable, `aicur-backend`, built from
`packaging/backend/aicur_backend.py`, with subcommands:

- `supervise` - the only thing a GUI shell launches. Ensures the config, then runs the API and
  the collector loop as its children, restarts a child that dies (bounded backoff), and stops
  both when it receives SIGTERM/SIGINT or when its parent process disappears.
- `api` - `uvicorn src.api:app` on `127.0.0.1:<port>` (never any other host).
- `collector-loop` - waits for `/health`, then runs `collect-once` as a subprocess every 60 s
  (the same one-cycle-per-process contract launchd uses today).
- `collect-once` - one `src.collector.main()` cycle.

Supervision lives in Python so it is unit-tested here and shared by the macOS app, the Windows
tray and the Linux tray. Deviation from the charter wording, stated plainly: the GUI app's direct
child is the supervisor; the API and collector are the supervisor's children. The smoke test
asserts all four processes (app, supervisor, API, collector) are gone after quit.

Config (`~/.usage-tracker/config`, mode 0600): `USAGE_TRACKER_SECRET=<token_urlsafe(32)>`
generated once, reused afterwards, permissions tightened to 0600 if found looser. Data paths are
passed to the children by environment: `USAGE_TRACKER_DB=~/.usage-tracker/claude_usage.db`,
`USAGE_TRACKER_ENV_FILE=~/.usage-tracker/env`. Source installs keep their repo-relative defaults.

### macOS

- `Usage Tracker.app` renamed for display to "ai-cur desktop client", bundle id
  `com.harvto.aicur.desktop`. OTLP scope `com.amgad.usage-tracker.fleet` unchanged.
- Backend at `Contents/Resources/backend/<arch>/aicur-backend` for `arm64` and `x86_64`; the
  universal Swift binary picks its own arch. pydantic-core ships no universal2 wheel, so the two
  backends are built separately (arm64 native, x86_64 under Rosetta on `macos-15`).
- The app starts `aicur-backend supervise` on launch, stops it on quit and on SIGTERM. Launch at
  login through `SMAppService.mainApp`, toggled from the menu (the existing Settings toggle).
  `AICUR_SMOKE=1` skips the browser-cookie sentinel so the app can run headless in CI; usage
  alerts, the only notification prompt, are off by default.
- `scripts/make_dmg.sh` builds `ai-cur-desktop-<version>.dmg` with `hdiutil` only.

### Decisions and deviations (V1)

- Process tree is app -> supervisor -> {API, collector loop}. The charter says "both children";
  the smoke test asserts all FOUR processes (app, supervisor, API, collector) are gone, after a
  SIGTERM quit and again after a SIGKILL force quit.
- Force quit is covered by two watches: the supervisor polls its ORIGINAL parent pid (a
  supervisor legitimately started with ppid 1 is not treated as orphaned), and the API and
  collector each watch the supervisor, so a SIGKILLed supervisor leaves no listener either.
- The smoke test runs on port 18765 (written into the throwaway config as `USAGE_TRACKER_PORT`),
  not 8000, so it cannot collide with a source install and it exercises the port override.
  Shipped apps use 8000, which the Swift app still hard-codes.
- The on-disk bundle stays `Usage Tracker.app` as the charter names it; the display name is
  "ai-cur desktop client". The bundle id change means settings stored under the old id
  (`com.harvto.UsageMenuBar`) do not carry over.
- The dmg holds two backends (~28 MB each), so it is ~35 MB compressed.
- `scripts/package_macos_app.sh` (zip, no backend) is kept unchanged as the negative control.

## Proven here vs waits for CI

| Claim | Proven here (how) | Waits for CI |
|-------|-------------------|--------------|
| Backend freezes with PyInstaller one-dir, arm64 | `build_backend.py` locally: 28 MB, `Mach-O 64-bit executable arm64`, `.mjs` + pricing catalog inside `_internal/` | x86_64 build under Rosetta |
| Supervisor starts API + collector, restarts, stops | 13 unit tests (`tests/test_aicur_backend.py`); frozen supervisor run by hand: rows written, clean exit on SIGTERM | |
| Config generation 0600 / reuse / tighten | 10 unit tests (`tests/test_aicur_config.py`) | |
| Env overrides redirect; defaults unchanged | `tests/test_install_overrides.py` in fresh interpreters | |
| `make_dmg.sh` argument checks (NOTARY_PROFILE + ad hoc fails first) | 11 tests (`tests/test_make_dmg_args.py`) | |
| `make_dmg.sh` builds a verifiable dmg | local run with a shell stand-in for the Swift binary and the arm64 backend in both slots: `codesign --verify --deep --strict` and `hdiutil verify` pass, 34.8 MB | real universal Swift build, real x86_64 backend |
| Smoke: health + collector row + quit + force quit | `smoke_macos.sh` PASS against that dmg (health 1 s, 3 rows at 3 s, 4 processes gone after SIGTERM and after SIGKILL) | same against the real Swift app, arm64 and `LAUNCH_ARCH=x86_64` |
| Negative control fails with exit 3 | stand-in app without a backend: `SMOKE FAIL (3)` | real `package_macos_app.sh` zip |
| Swift `BackendController` compiles | `swiftc -typecheck` for arm64 and x86_64 (CommandLineTools SDK) | full app build (needs Xcode 26) |
| SIGTERM reaches applicationWillTerminate | no (stand-in is a shell script) | smoke SIGTERM phase against the Swift app |
| Workflow is valid | `actionlint` 1.7.12 with shellcheck 0.11.0: clean | everything it runs |
| Signing / notarization path | no identity here | only when the secrets exist |

## Status log

- 2026-09-19: plan written; repo inspected; refutation check above recorded.
- 2026-09-19: V1 implemented. Local runs caught three instrument defects before they reached CI:
  a read-only `sqlite3 file:...?mode=ro` open fails on the live WAL database ("unable to open
  database file (14)"), `mktemp` under `$TMPDIR` produced a `T//` path that `pgrep -f` never
  matched, and cleanup raced the orphan watchdog on the port. All fixed in `smoke_macos.sh`.
