"""scripts/make_dmg.sh refuses bad invocations before it builds anything.

These checks run on any machine with bash: the script validates its inputs before it
touches swift, codesign or hdiutil.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_dmg.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="needs bash")


def fake_backend(root: Path, name: str) -> Path:
    folder = root / name / "aicur-backend"
    folder.mkdir(parents=True)
    exe = folder / "aicur-backend"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return folder


def run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    out = tmp_path / "out"
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "OUTPUT_DIR": str(out),
        "BACKEND_ARM64": str(fake_backend(tmp_path, "arm")),
        "BACKEND_X86_64": str(fake_backend(tmp_path, "x86")),
    }
    base.update(env)
    base = {k: v for k, v in base.items() if v is not None}
    result = subprocess.run(["bash", str(SCRIPT)], env=base, capture_output=True, text=True, timeout=30)
    assert not out.exists(), "an argument error must not create the output directory"
    return result


def test_notary_profile_with_ad_hoc_identity_fails(tmp_path):
    result = run(tmp_path, NOTARY_PROFILE="release", SIGNING_IDENTITY="-")
    assert result.returncode == 1
    assert "NOTARY_PROFILE requires a Developer ID signing identity" in result.stderr


def test_notary_profile_with_default_identity_fails(tmp_path):
    result = run(tmp_path, NOTARY_PROFILE="release")  # SIGNING_IDENTITY defaults to ad hoc
    assert result.returncode == 1
    assert "NOTARY_PROFILE requires" in result.stderr


def test_notary_profile_with_real_identity_passes_that_check(tmp_path):
    result = run(tmp_path, NOTARY_PROFILE="release", SIGNING_IDENTITY="Developer ID Application: X",
                 VERSION="not-a-version")
    assert result.returncode == 1
    assert "NOTARY_PROFILE requires" not in result.stderr
    assert "VERSION is not a version" in result.stderr


@pytest.mark.parametrize("var", ["BACKEND_ARM64", "BACKEND_X86_64"])
def test_each_backend_is_required(tmp_path, var):
    env = {var: ""}
    result = run(tmp_path, **env)
    assert result.returncode == 1
    assert f"{var} is required" in result.stderr


def test_backend_folder_without_executable_fails(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run(tmp_path, BACKEND_X86_64=str(empty))
    assert result.returncode == 1
    assert "has no executable aicur-backend" in result.stderr


@pytest.mark.parametrize("version", ["1.2", "v1.2.3", "1.2.3 ", ""])
def test_bad_version_fails(tmp_path, version):
    result = run(tmp_path, VERSION=version) if version else run(tmp_path, VERSION="x")
    assert result.returncode == 1
    assert "VERSION is not a version" in result.stderr


def test_bad_build_number_fails(tmp_path):
    result = run(tmp_path, BUILD_NUMBER="12a")
    assert result.returncode == 1
    assert "BUILD_NUMBER must be an integer" in result.stderr
