from src.codex_sources import (
    canonical_codex_surface,
    codex_session_files,
    codex_session_roots,
    isolated_codex_default_model,
)


def test_discovers_standard_and_loop_session_stores(tmp_path):
    standard = tmp_path / ".codex" / "sessions"
    loop = tmp_path / ".loop" / "runs" / "project" / "33" / "codex-home" / "sessions"
    for root, name in ((standard, "standard"), (loop, "loop")):
        day = root / "2026" / "07" / "25"
        day.mkdir(parents=True)
        (day / f"{name}.jsonl").write_text("{}\n")

    assert codex_session_roots(tmp_path) == [standard, loop]
    assert {path.name for path in codex_session_files(tmp_path)} == {
        "standard.jsonl",
        "loop.jsonl",
    }


def test_originator_wins_over_generic_or_structured_source():
    assert canonical_codex_surface("loop", "vscode") == "loop"
    assert canonical_codex_surface("loop", {"subagent": "guardian"}) == "loop"
    assert canonical_codex_surface("codex-tui", "vscode") == "cli"
    assert canonical_codex_surface(None, {"subagent": "guardian"}) == "subagent"


def test_reads_model_only_from_isolated_codex_home(tmp_path):
    isolated = tmp_path / ".loop" / "runs" / "project" / "27" / "codex-home"
    session = isolated / "sessions" / "2026" / "07" / "23" / "session.jsonl"
    session.parent.mkdir(parents=True)
    (isolated / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    assert isolated_codex_default_model(session) == "gpt-5.6-sol"

    standard = tmp_path / ".codex" / "sessions" / "2026" / "07" / "23" / "session.jsonl"
    assert isolated_codex_default_model(standard) is None
