"""Freeze the backend into a PyInstaller one-dir folder: <dist>/aicur-backend/.

    python packaging/backend/build_backend.py [--dist DIR] [--work DIR] [--target-arch ARCH]

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dist", type=Path, default=ROOT / "dist" / "backend")
    parser.add_argument("--work", type=Path, default=ROOT / "build" / "backend")
    parser.add_argument("--target-arch", default=None)
    args = parser.parse_args(argv)

    import PyInstaller.__main__

    PyInstaller.__main__.run(pyinstaller_args(args.dist, args.work, args.target_arch))
    exe = args.dist / "aicur-backend" / ("aicur-backend.exe" if os.name == "nt" else "aicur-backend")
    if not exe.is_file():
        print(f"build produced no executable at {exe}", file=sys.stderr)
        return 1
    print(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
