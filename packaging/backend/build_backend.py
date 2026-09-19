"""Freeze the backend (or the Windows tray) into a PyInstaller one-dir folder.

    python packaging/backend/build_backend.py [--dist DIR] [--work DIR] [--target-arch ARCH]
    python packaging/backend/build_backend.py --target windows-tray [--dist DIR] [--work DIR]

The backend lands in <dist>/aicur-backend/, the tray in <dist>/aicur-tray/ (windowed: no
console; it reports into ~/.usage-tracker/logs/tray.log instead).

Same command on macOS, Windows and Linux. Run it with the Python whose architecture you want
the backend built for (macOS: an arm64 or an x86_64 interpreter); --target-arch is passed to
PyInstaller only as a guard that the interpreter matches.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "packaging" / "backend" / "aicur_backend.py"
TRAY_ENTRY = ROOT / "clients" / "windows_tray.py"

# Files the backend reads next to its own modules at runtime. Each resolves through
# Path(__file__), which PyInstaller maps onto the bundle root.
DATA_FILES = [
    ("src/pricing_catalog.json", "src"),
    ("scripts/fetch_claude_web_usage.mjs", "scripts"),
    ("scripts/fetch_codex_web_analytics.mjs", "scripts"),
]


def pyinstaller_args(dist: Path, work: Path, target_arch: str | None) -> list[str]:
    args = [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name", "aicur-backend",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--paths", str(ROOT),
        "--paths", str(ENTRY.parent),
        "--collect-submodules", "src",
        "--collect-submodules", "uvicorn",
        "--hidden-import", "aicur_config",
    ]
    for source, dest in DATA_FILES:
        path = ROOT / source
        if not path.is_file():
            raise SystemExit(f"missing data file: {source}")
        args += ["--add-data", f"{path}{os.pathsep}{dest}"]
    if target_arch:
        args += ["--target-arch", target_arch]
    args.append(str(ENTRY))
    return args


def tray_args(dist: Path, work: Path) -> list[str]:
    return [
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name", "aicur-tray",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--paths", str(TRAY_ENTRY.parent),
        "--paths", str(ENTRY.parent),
        "--hidden-import", "aicur_config",
        "--hidden-import", "tray_core",
        str(TRAY_ENTRY),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist" / "backend")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "backend")
    parser.add_argument("--target-arch", default=None)
    parser.add_argument("--target", choices=["backend", "windows-tray"], default="backend")
    args = parser.parse_args(argv)

    import PyInstaller.__main__

    if args.target == "windows-tray":
        PyInstaller.__main__.run(tray_args(args.dist, args.work))
        name = "aicur-tray"
    else:
        PyInstaller.__main__.run(pyinstaller_args(args.dist, args.work, args.target_arch))
        name = "aicur-backend"
    exe = args.dist / name / (f"{name}.exe" if os.name == "nt" else name)
    if not exe.is_file():
        print(f"build produced no executable at {exe}", file=sys.stderr)
        return 1
    print(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
