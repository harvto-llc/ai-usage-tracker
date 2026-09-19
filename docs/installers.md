# Installers: plan, decisions, status

Single source for the installer work on branch `feat/installers` (cut from `origin/main` at
`a1d01a9`). Plan, decisions and status live here and nowhere else; this run does not create
`PLAN.md` or `status.md` because the task charter asks for ONE file.

Nothing here is pushed, tagged or released by the agent. The supervisor pushes after the
founder's OK.

## Deliverables

| | What | State |
|-|------|-------|
| V1 | macOS dmg, bundled backend, CI build + smoke test, unsigned | Oss PASS + supervisor ACCEPT at `b04d453`; **proven in CI** (run 3 `35425765321` at `d572df9`) |
| V2 | Windows tray + Inno Setup per-user installer, CI smoke | FAILED review at `2c760a9`, fixed; Oss PASS + supervisor ACCEPT at `536acab`; **proven in CI** (run 3 `35425765321` at `d572df9`) |
| V3 | Linux tray + `.deb` + AppImage, systemd user unit, CI smoke under xvfb | Oss PASS at `8211f3e`; **proven in CI** (run 2 and run 3 `35425765321` at `d572df9`) |
| V4 | README downloads first, CHANGELOG, Homebrew cask on the dmg | Oss PASS at `8211f3e` |

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

### Windows (V2)

- `clients/windows_tray.py` owns the backend like the Mac app does: it starts
  `backend\aicur-backend.exe supervise --parent-pid <tray pid>`. There is no SIGTERM on Windows,
  so Quit stops the supervisor with TerminateProcess and the API and collector exit through
  their own parent watch (OpenProcess(SYNCHRONIZE) + WaitForSingleObject, never
  `os.kill(pid, 0)`, which terminates the process on Windows).
- Ported from Codex's unmerged tray: the Win32 layer only. Its menu drove a web dashboard and
  a browser entry-code handoff that this repository does not have, so the menu here is the
  quota summary, Refresh, Open logs folder, Start at login (the HKCU Run value) and Quit.
  Codex's acceptance harness is not ported.
- `AICUR_SMOKE=1` lets the tray run without an icon when the session has no notification area
  (a CI runner); without it the tray refuses to start invisibly, as Codex's did.
- `aicur-tray.exe --quit` posts WM_CLOSE to the running tray's window. The installer uses it
  before replacing or deleting files, then `taskkill /F /T` as a backstop.
- The installer is per user (`PrivilegesRequired=lowest`) under
  `%LOCALAPPDATA%\Programs\ai-cur desktop client`. Uninstall removes the folder, the Start
  Menu entry and the Run value, and leaves `~\.usage-tracker` (data and config) in place, as
  `scripts/install-launchd.sh --uninstall` does.
- Negative control on Windows: the same `.iss` with `/DNoBackend` (tray, no backend). The smoke
  test must exit 3 on it, and it runs before the real installer is built.
- Authenticode secrets are named `WINDOWS_CERT_PFX` (base64 .pfx) and `WINDOWS_CERT_PASSWORD`;
  the charter did not name them.

### Linux (V3)

- The backend runs as the systemd user unit `aicur-backend.service`
  (`ExecStart=... aicur-backend supervise`, `Restart=on-failure`), so its parent is the user's
  systemd manager and `systemctl --user stop` stops everything (KillMode control-group, plus
  the children's own parent watch). The `.deb` enables it for every user with
  `systemctl --global enable`; the AppImage writes it into `~/.config/systemd/user` on first
  run (`--install`) with `ExecStart="<AppImage>" backend supervise`.
- The tray (`clients/linux_tray.py`) runs on `/usr/bin/python3` with the distribution's
  `python3-gi`, not frozen: the `.deb` depends on `python3-gi`, `gir1.2-gtk-3.0` and
  `gir1.2-ayatanaappindicator3-0.1`. The launcher names `/usr/bin/python3` explicitly because a
  venv or toolcache `python3` earlier on PATH has no `gi` (this would have broken the CI smoke,
  where setup-python's interpreter is first on PATH).
- Scope differs by package: the `.deb` is a system install and its postinst enables the user
  unit for every account on the machine (each user's backend starts at their next login);
  the AppImage is per user and touches only `~/.config`.
- The tray does not own the backend on Linux; on start it runs
  `systemctl --user start aicur-backend.service` if the unit is inactive. Quit quits the tray.
- The recorded GNOME defect: stock GNOME Shell has no tray. The `.deb` description says so; at
  start the tray asks the session bus whether anything owns `org.kde.StatusNotifierWatcher`,
  and when nothing does it shows a plain dialog once (marker `~/.usage-tracker/no-tray-host-warned`)
  and exits 3 instead of running invisibly. The CI smoke asserts that exit and message under
  xvfb, where no tray host exists; the visible-icon path is not exercised by CI.
- Ported from Codex's unmerged `clients/linux_tray.py`: the Ayatana-then-legacy binding
  fallback, the StatusNotifierWatcher probe, the refusal to run invisibly, and the delayed
  republish of menu and label. Its dashboard menu is not ported.
- `appimagetool` is pinned to release 1.9.1 by SHA-256
  `ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0` (two independent
  downloads matched) and the build fails on a mismatch.
- The `.deb` maintainer field is a placeholder (`maintainer@harvto.invalid`); the founder should
  name a real address before a public release.
- Negative control on Linux too: a `.deb` whose backend is a stub that exits at once must fail
  the smoke with exit 3; it runs before the real packages are built.
- Codex quota on Windows (found while sweeping, fixed in `309e61d`): the app-server pipe was
  read with `select()`, which accepts only sockets on Windows, so Codex quota never loaded
  there. Now a reader thread.

### Docs (V4)

- `README.md` opens with the four downloads, what "unsigned" means per platform, the GNOME note,
  the Node/Chrome limit from the refutation check above, and where data stays after uninstall.
  The source install follows under "Install from source".
- `CHANGELOG.md` is new.
- The Homebrew cask template is now cask `ai-cur-desktop`, installs the dmg, quits the app on
  uninstall, and zaps `~/.usage-tracker` and the new preferences plist. `make_dmg.sh` renders it
  to `dist/homebrew/ai-cur-desktop.rb`. It points at `ai-cur-desktop-<version>.dmg`, the signed
  name; an `-unsigned` CI artifact is not meant for the cask.

### CI run 3 (`35425765321`, at `d572df9`): all three platforms green

macos, windows and linux all succeeded; release skipped (not a tag). Every negative control
failed with exactly exit 3 first. The macOS smoke counted 4 processes once it matched on the
work-dir name, which confirms the path-spelling cause of run 2's "3 of 4" (not an exited
collector: collector-loop is long-lived). Commits after `d572df9` (structural process-tree
check on macOS, the `gate` job, Windows strict mode) are instrument hardening and need their
own green run.

### CI run 2 (`35425372263`, at `8211f3e`)

- linux: success. The stub-backend `.deb` failed the smoke with exit 3 as required, then the
  `.deb` and AppImage smokes passed. Those rows below are proven by CI.
- macos: the negative control passed (zip, exit 3) and the dmg built. The real Swift app then
  answered `/health` in 2 s and the collector wrote 3 rows in 3 s. The smoke then failed its
  process count (3 of 4): an instrument fault, fixed in the next commit, not an app fault.
- windows: the negative control passed and the installer built. The smoke got through
  install, Start Menu entry and Run key, `/health`, 3 collector rows, and the planted Claude
  session still alive after a full refresh (listed as `running`), with tray, supervisor, API
  and collector all present. It then hit a PowerShell variable-name clash (`$tray` vs `$Tray`),
  also an instrument fault, fixed. Quit, force quit and uninstall have not run yet.

### CI run 1 (`35424382569`, at `ea48810`): failed before any build (archived record)

AS MEASURED by the supervisor and re-read here from `gh run view --log-failed`:

- macos, Unit tests: 4 failed, 752 passed. Pre-existing tests in `tests/test_pty_scraper.py`
  asserted Pacific wall-clock strings (`'Apr 11 2:00 AM' == 'Apr 10 7:00 PM'` on the UTC
  runner). Reproduced here with `TZ=UTC`. A zone sweep found four more such tests
  (`Asia/Tokyo`, `Pacific/Kiritimati`, `Pacific/Pago_Pago`). Fixed in `a9bb7fc`: each test pins
  its own zone through the `local_timezone` fixture. The full suite passes under default, UTC,
  Asia/Tokyo, America/Los_Angeles, Pacific/Kiritimati, Pacific/Pago_Pago, Asia/Kolkata and
  Australia/Lord_Howe (766 each). On Windows, which has no `time.tzset`, those eight tests
  skip; the Windows job does not run them.
- windows, Unit tests: `tests/conftest.py` imported `pwd`, so no test ran. Fixed in `29b8c7b`:
  the real home comes from `pwd` where it exists and from `USERPROFILE` otherwise, and the
  sandbox moves `USERPROFILE` as well as `HOME`. Simulated here by blocking `pwd`: the Windows
  job's test subset passes (72). `58640fa` adds `tests/test_process_liveness.py` to that
  subset; it had been missing.
- Nothing was built or smoke-tested, so every "waits for CI" row below still waits.

Can a run look green with builds unrun? No. Every build and smoke step is unconditional, and
the workflow has no `continue-on-error` (checked by grep: the only `if:` lines are Authenticode
signing, which needs its secrets, and the release job, which needs a tag). A failed step
stops its job and turns the run red. The `release` job shows "skipped" on every pull request
by design (tags only) and on a tag whose builds failed; in both cases the run is red, not green.

What the Windows backend does without the pieces the scrapers expect (`src/pty_scraper.py`
has no pty: the name is historical, and it imports nothing POSIX-only):

- Codex quota: `codex app-server` over a pipe. It works on Windows since `309e61d`, if `codex`
  is on PATH; otherwise it is skipped.
- Cursor quota: plain HTTPS; same on every platform.
- Claude web quota and Codex analytics: Node plus Chrome. The helpers' Chrome search list
  (`scripts/fetch_*.mjs`) has macOS paths and PATH names only, so on Windows they fail to find
  Chrome, the collector logs the failure, and those gauges stay empty. Local activity, tokens
  and the API are unaffected. Known limit; README says Node and Chrome are needed.
- Reset-time formatting uses `%-d` (POSIX-only); on Windows it raises ValueError, which is
  caught, and the fallback formatter is used.

### V2 review FAIL at `2c760a9` (archived record; fixed in `ec4a84f`, `a00917f`)

AS RAISED by the supervisor: `src/session_runtime.py` `_process_is_running` called
`os.kill(pid, 0)` on every platform. On Windows that is TerminateProcess, and the PIDs come from
`~/.claude/sessions/*.json`, the user's live Claude Code sessions. The probe runs on every
`GET /work-ledger/sessions` and every enabled work-ledger refresh, so the Windows backend as
built at `2c760a9` would have killed the user's sessions. I had fixed the probe only in the file
I wrote.

Fix: `src/process_liveness.pid_alive` is the one probe; `session_runtime` and the supervisor
import it. `tests/test_process_liveness.py` runs on any OS with `sys.platform` patched to
`win32` and `os.kill` recorded (4 of its tests fail on the unfixed tree), and its source-tree
guard allows exactly one `os.kill(<pid>, 0)` in `src/`, `clients/`, `packaging/`: the POSIX
branch of `pid_alive`. The Windows smoke plants a live dummy process as a Claude session before
the backend starts, runs a full refresh and a session listing, and fails (exit 7) if the dummy
dies, or (exit 2) if the planted session never shows up.

Sweep for the same class (grep over `src/`, `clients/`, `packaging/`, `scripts/` at `a00917f`):

- `os.kill(<pid>, 0)`: only `src/process_liveness.py` (POSIX branch), enforced by the guard test.
- `os.killpg`: none.
- Signals sent to PIDs read from disk: none. `session_runtime` is the only code that reads PIDs
  from disk, and it now only probes them.
- `Popen.terminate()` / `.kill()`: `packaging/backend/aicur_backend.py` (its own children),
  `clients/tray_core.py` (its own supervisor), `src/pty_scraper.py` (its own `codex` child). All
  target the caller's own child; on Windows that is TerminateProcess, which is the intent. The
  only consequence is that a Windows quit is not graceful: the supervisor is terminated and its
  API and collector exit through their own parent watch within about a second.
- `signal.signal(SIGTERM/SIGINT)` in the supervisor: valid on Windows (never delivered by the
  tray, harmless).
- `child.kill("SIGKILL")` in `scripts/fetch_*.mjs`: Node's own Chrome child; on Windows Node
  maps it to TerminateProcess of that child.

## Proven here vs waits for CI

| Claim | Proven here (how) | Proven in CI (run 3 `35425765321` at `d572df9`, unless noted) |
|-------|-------------------|--------------|
| Backend freezes with PyInstaller one-dir, arm64 | `build_backend.py` locally: 28 MB, `Mach-O 64-bit executable arm64`, `.mjs` + pricing catalog inside `_internal/` | arm64 and x86_64 (Rosetta, setup-python x64) both built; `file` checked each arch |
| Supervisor starts API + collector, restarts, stops | 17 unit tests (`tests/test_aicur_backend.py`); frozen supervisor run by hand: rows written, clean exit on SIGTERM | |
| Config generation 0600 / reuse / tighten | 10 unit tests (`tests/test_aicur_config.py`) | |
| Env overrides redirect; defaults unchanged | `tests/test_install_overrides.py` in fresh interpreters | |
| `make_dmg.sh` argument checks (NOTARY_PROFILE + ad hoc fails first) | 11 tests (`tests/test_make_dmg_args.py`) | |
| `make_dmg.sh` builds a verifiable dmg | local run with a shell stand-in for the Swift binary and the arm64 backend in both slots: `codesign --verify --deep --strict` and `hdiutil verify` pass, 34.8 MB | universal Swift build with Xcode 26 on `macos-15` plus both real backends; dmg built and smoke-tested |
| Smoke: health + collector row + quit + force quit | `smoke_macos.sh` PASS against that dmg (health 1 s, 3 rows at 3 s, 4 processes gone after SIGTERM and after SIGKILL) | real Swift app, arm64: health 2 s, 3 rows 3 s, 4 processes, all gone after SIGTERM and after SIGKILL; x86_64 under Rosetta: health 13 s, 3 rows 14 s, same |
| Negative control fails with exit 3 | stand-in app without a backend: `SMOKE FAIL (3)` | macOS zip, Windows no-backend installer, Linux stub .deb: each `SMOKE FAIL (3)`, each step green only on exactly 3 |
| Swift `BackendController` compiles | `swiftc -typecheck` for arm64 and x86_64 (CommandLineTools SDK) | full universal app build |
| SIGTERM reaches applicationWillTerminate | no (stand-in is a shell script) | yes: SIGTERM phase passes against the real app (all four processes gone) |
| Workflow is valid | `actionlint` 1.7.12 with shellcheck 0.11.0: clean | all three jobs ran to green |
| Signing / notarization path | no identity here | NOT proven: no secrets configured; runs only when they exist |
| Tray core: config, /stats probe, summary, menu, backend start/stop | 29 unit tests (`tests/test_tray_core.py`); `windows_tray.py --check` against a live local API printed the real summary | |
| Windows tray neutral helpers: Run key, menu ids/flags, v4 word decoding | 7 unit tests (`tests/test_windows_tray.py`, fake winreg) | same tests pass on `windows-latest` |
| Win32 tray (window, icon, menu, --quit) | nothing: no Windows host here | tray starts, owns the backend, `--quit` and force kill leave nothing (icon itself not inspected) |
| Inno Setup script compiles; per-user install, shortcut, Run key, clean uninstall | nothing | installed; Start Menu entry and Run key present; uninstall removed folder, entry and Run key |
| `smoke_windows.ps1` parses | no `pwsh` here | parse step passed |
| No liveness probe signals a process on Windows | `tests/test_process_liveness.py` (7 tests; 4 fail on the unfixed tree), source-tree guard with a planted violation | |
| A planted live Claude session survives a refresh | POSIX run of the same endpoints: refresh `ok`, `claude_runtime` sessions 1, dummy listed `running` and alive | Windows: `dummy Claude session pid 4668 still alive after refresh; listed as running` (also run 2) |
| `.deb`: apt install, global user-unit enable, autostart valid, tray starts the service, /health, collector row, stop leaves nothing, apt remove cleans | emulated Ubuntu 22.04 container: install, symlink, `--check`, exit 3, removal (not /health) | yes, `ubuntu-22.04` with a real user systemd (runs 2 and 3) |
| AppImage: `--install` writes and starts the user unit, /health, row, stop, `--uninstall` cleans | nothing (appimagetool does not run under emulation here) | yes (runs 2 and 3) |
| GNOME with no tray host: tray refuses with the plain AppIndicator message (exit 3) | container, under xvfb | yes, in both Linux smokes |
| Nothing listens beyond 127.0.0.1 | code: `LOOPBACK` only, no `--host` option (unit test) | Linux smoke checks every listener on the port |

Rows with an empty CI cell are unit tests; they also pass in CI (the full suite on macOS, the
installer subsets on Windows and Linux).

## Status log

- 2026-09-19: plan written; repo inspected; refutation check above recorded.
- 2026-09-19: V1 implemented. Local runs caught three instrument defects before they reached CI:
  a read-only `sqlite3 file:...?mode=ro` open fails on the live WAL database ("unable to open
  database file (14)"), `mktemp` under `$TMPDIR` produced a `T//` path that `pgrep -f` never
  matched, and cleanup raced the orphan watchdog on the port. All fixed in `smoke_macos.sh`.
