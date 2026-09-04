from __future__ import annotations

import json

import pytest

from migrator import runner
from migrator.phases.base import PhaseContext, PhaseError, PhaseResult


def _register(monkeypatch, fn):
    monkeypatch.setitem(
        runner.PHASES, "probe", runner.PhaseDefinition(99, "probe", "99_probe", fn)
    )


def _prepare(state_context):
    _cfg, paths, state, _, runtime = state_context
    state.start_run(
        start_epoch=1,
        hour_utc=0,
        weekday=0,
        budget_minutes=1,
        host="t",
        reconcile=False,
    )
    state.close()
    (paths.root / "mirror.toml").write_text("", encoding="utf-8")
    return runtime


def test_run_phase_records_pass_with_run_id(state_context, monkeypatch):
    runtime = _prepare(state_context)
    seen = {}

    def probe(ctx: PhaseContext) -> PhaseResult:
        seen["run_id"] = ctx.run_id
        seen["apply"] = ctx.apply
        return PhaseResult(outputs={"files": 3})

    _register(monkeypatch, probe)
    monkeypatch.setattr(runner, "load_config", lambda _: state_context[0])
    assert runner.run_phase("probe", apply=True, runtime=runtime) == "PASS"
    assert seen == {"run_id": 1, "apply": True}
    from migrator.state import State

    state = State(state_context[1].state_db, "test")
    row = state.latest_phase(99)
    assert row["status"] == "PASS"
    assert json.loads(row["outputs_json"]) == {"files": 3}
    assert json.loads(row["inputs_json"])["run_id"] == 1
    state.close()


def test_run_phase_records_fail_and_reraises(state_context, monkeypatch):
    runtime = _prepare(state_context)

    def probe(ctx: PhaseContext) -> PhaseResult:
        raise PhaseError("listing refused: /Taxes/secret.pdf")

    _register(monkeypatch, probe)
    monkeypatch.setattr(runner, "load_config", lambda _: state_context[0])
    with pytest.raises(PhaseError):
        runner.run_phase("probe", apply=False, runtime=runtime)
    from migrator.state import State

    state = State(state_context[1].state_db, "test")
    assert state.latest_phase(99)["status"] == "FAIL"
    state.close()


@pytest.mark.parametrize(("status", "code"), [("PASS", 0), ("PLANNED", 0), ("FAIL", 2)])
def test_main_maps_phase_status_to_exit_code(state_context, monkeypatch, status, code):
    from migrator.__main__ import main

    runtime = _prepare(state_context)
    monkeypatch.setitem(
        runner.PHASES, "probe", runner.PhaseDefinition(99, "probe", "99_probe", None)
    )
    monkeypatch.setattr(
        "migrator.__main__.run_phase", lambda command, *, apply, runtime: status
    )
    monkeypatch.setattr("migrator.__main__.Runtime.from_environ", lambda: runtime)
    assert main(["probe"]) == code


def test_main_hides_error_text_unless_verbose(state_context, monkeypatch, capsys):
    from migrator.__main__ import main

    runtime = _prepare(state_context)

    def probe(ctx: PhaseContext) -> PhaseResult:
        raise PhaseError("contains /Taxes/secret.pdf")

    _register(monkeypatch, probe)
    monkeypatch.setattr(runner, "load_config", lambda _: state_context[0])
    monkeypatch.setattr("migrator.__main__.Runtime.from_environ", lambda: runtime)
    assert main(["probe"]) == 1
    err = capsys.readouterr().err
    assert "PhaseError" in err and "secret.pdf" not in err
