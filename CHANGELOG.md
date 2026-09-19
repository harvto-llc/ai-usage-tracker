# Changelog

All notable changes to this project. Dates are release dates; unreleased work is at the top.

## Unreleased

### Added

- Installers for all three desktop platforms, each with its own bundled backend (no Python
  needed):
  - macOS: `ai-cur-desktop-<version>.dmg` holds a universal menu bar app with the backend for
    Apple silicon and Intel inside. The app starts the backend at launch, restarts it if it
    dies, and stops it on quit. Launch at login uses the existing Settings toggle.
  - Windows: `ai-cur-desktop-setup-<version>.exe`, a per-user installer (no admin) with a
    notification-area app, a Start Menu entry and start with Windows. Uninstall removes all
    three.
  - Linux: `ai-cur-desktop_<version>_amd64.deb` and `ai-cur-desktop-<version>-x86_64.AppImage`.
    Each has a tray icon, an XDG autostart entry, and a systemd user service for the backend.
- `aicur-backend`, one frozen backend for all installers. It supervises the local API
  (`127.0.0.1` only) and the collector, and generates `~/.usage-tracker/config` (mode 0600) on
  first run.
- Release workflow: builds and smoke-tests every installer on pull requests and tags. Each
  smoke test must first fail against a build with no backend. Signing runs only when the
  signing secrets exist.
- Environment overrides for packaged installs: `USAGE_TRACKER_DB`, `USAGE_TRACKER_ENV_FILE`,
  `USAGE_TRACKER_API_URL`. Source installs keep their defaults.

### Changed

- The macOS app is now "ai-cur desktop client", bundle id `com.harvto.aicur.desktop`. Settings
  saved under the old id (`com.harvto.UsageMenuBar`) do not carry over.
- The Homebrew cask template is now `ai-cur-desktop` and installs the dmg.

### Fixed

- Checking whether a Claude Code session is still running no longer uses `os.kill(pid, 0)`. On
  Windows that call terminates the process, so the Windows backend would have ended live
  Claude Code sessions. There is now a single liveness check, and a test that scans the source
  tree for any other.
- A collector cycle can no longer outlive the process that started it.

## 0.1.0

- First public release of the local collector, API and menu bar app.
