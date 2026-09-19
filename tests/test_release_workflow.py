"""The release workflow's gate must cover every build job, derived from the file itself.

`gate` is the one check a pull request can require. GitHub Actions cannot say "all jobs", so
its `needs` is a hand-written list; a build job added later and left out of it would ship
ungated. This test reads the job names from .github/workflows/release.yml and fails unless
`gate` needs every job except itself and `release`, and `release` needs `gate`.

No YAML library is a dependency here, so jobs are read as the two-space keys under `jobs:`
and `needs:` must be written as a flow list on one line; any other shape fails the test
rather than being skipped.
"""

import re
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"

JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
NEEDS = re.compile(r"^    needs:\s*(.*)$")
FLOW_LIST = re.compile(r"^\[([A-Za-z0-9_,\s-]*)\]\s*$")


def parse_jobs(text: str) -> dict[str, list[str] | None]:
    """{job: needs-list or None} for every job under `jobs:`."""
    lines = text.splitlines()
    try:
        start = lines.index("jobs:")
    except ValueError as exc:
        raise AssertionError("no top-level 'jobs:' line") from exc
    jobs: dict[str, list[str] | None] = {}
    current = None
    for line in lines[start + 1:]:
        if line and not line.startswith(" ") and not line.startswith("#"):
            break  # next top-level key
        key = JOB_KEY.match(line)
        if key:
            current = key.group(1)
            jobs[current] = None
            continue
        needs = NEEDS.match(line)
        if needs and current is not None:
            flow = FLOW_LIST.match(needs.group(1))
            if not flow:
                raise AssertionError(f"{current}: write needs as a one-line list, got {needs.group(1)!r}")
            jobs[current] = [n.strip() for n in flow.group(1).split(",") if n.strip()]
    return jobs


def check_gate(jobs: dict[str, list[str] | None]) -> None:
    assert "gate" in jobs and "release" in jobs, f"jobs found: {sorted(jobs)}"
    builds = sorted(j for j in jobs if j not in ("gate", "release"))
    assert builds, "no build jobs found"
    assert sorted(jobs["gate"] or []) == builds, (
        f"gate needs {sorted(jobs['gate'] or [])}, but the build jobs are {builds}")
    assert "gate" in (jobs["release"] or []), f"release needs {jobs['release']}, not gate"


def test_gate_needs_every_build_job_and_release_needs_gate():
    jobs = parse_jobs(WORKFLOW.read_text(encoding="utf-8"))
    assert sorted(jobs) == ["gate", "linux", "macos", "release", "windows"]
    check_gate(jobs)


def test_a_job_added_without_the_gate_fails(tmp_path):
    text = WORKFLOW.read_text(encoding="utf-8")
    planted = text.replace("\n  gate:\n", "\n  freebsd:\n    runs-on: ubuntu-22.04\n    steps:\n"
                           "      - run: echo build\n\n  gate:\n", 1)
    assert planted != text
    jobs = parse_jobs(planted)
    assert "freebsd" in jobs
    with pytest.raises(AssertionError, match="build jobs are"):
        check_gate(jobs)


def test_release_not_behind_the_gate_fails():
    text = WORKFLOW.read_text(encoding="utf-8").replace(
        "    needs: [gate]\n", "    needs: [macos, windows, linux]\n", 1)
    with pytest.raises(AssertionError, match="not gate"):
        check_gate(parse_jobs(text))


def test_a_needs_written_as_a_block_list_is_refused():
    text = WORKFLOW.read_text(encoding="utf-8").replace(
        "    needs: [gate]\n", "    needs:\n      - gate\n", 1)
    with pytest.raises(AssertionError, match="one-line list"):
        parse_jobs(text)
