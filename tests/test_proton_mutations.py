from __future__ import annotations

import io
import json
import subprocess

import pytest

from migrator.providers import proton_cli
from migrator.providers.proton_cli import (
    ProtonCLIError,
    ProtonCLIProvider,
    child_cli_path,
)


def _fake_run(responses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code, out, err = responses.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    return run, calls


class FakePopen:
    """A CLI process for the streamed transfer path: canned output, an exit code,
    and optionally a process that never exits until terminated."""

    def __init__(self, code, out, err, *, hang=False):
        self.stdout = io.StringIO(out)
        self.stderr = io.StringIO(err)
        self.returncode = None if hang else code
        self._code = code
        self._hang = hang
        self.terminated = False
        self.pid = 4242

    def wait(self, timeout=None):
        if self._hang and not self.terminated:
            raise subprocess.TimeoutExpired(["proton-drive"], timeout or 0)
        self.returncode = -15 if self.terminated else self._code
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _fake_popen(responses):
    calls = []

    def popen(argv, **kwargs):
        calls.append(argv)
        spec = responses.pop(0)
        return FakePopen(*spec[:3], hang=bool(spec[3]) if len(spec) > 3 else False)

    return popen, calls


def _ticking_clock(step):
    t = [0.0]

    def clock():
        t[0] += step
        return t[0]

    return clock


def test_upload_tree_argv_and_hook(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    popen, calls = _fake_popen([(0, '{"uploaded":1}\n', "")])
    hooks = []
    provider = ProtonCLIProvider(
        cfg, state, logger, popen=popen, after_call=lambda: hooks.append(1)
    )
    out = provider.upload_tree(
        [tmp_path / "A", tmp_path / "B"], "/my-files/Dropbox", "40_batches"
    )
    assert out.startswith('{"uploaded"')
    argv = calls[0]
    assert argv[:3] == ["proton-drive", "filesystem", "upload"]
    assert argv[argv.index("-f") + 1] == "create-new-revision"
    assert argv[argv.index("-d") + 1] == "merge"
    assert "--json" in argv and "--skip-thumbnails" in argv
    assert argv[-3:] == [str(tmp_path / "A"), str(tmp_path / "B"), "/my-files/Dropbox"]
    assert hooks == [1]


def test_upload_tree_partial_failure_exit_code_is_accepted(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    popen, _ = _fake_popen(
        [(1, '{"uploaded":1,"failed":1}\n', "one item could not be uploaded")]
    )
    provider = ProtonCLIProvider(cfg, state, logger, popen=popen)
    out = provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert out.startswith('{"uploaded"')


def test_upload_tree_failure_raises_and_still_hooks(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    popen, _ = _fake_popen([(1, "", "You need to login first")])
    hooks = []
    provider = ProtonCLIProvider(
        cfg, state, logger, popen=popen, after_call=lambda: hooks.append(1)
    )
    with pytest.raises(ProtonCLIError, match="AUTH"):
        provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert hooks == [1]


def test_list_stops_retrying_on_an_auth_failure(state_context):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([(1, "", "You need to login first")] * 8)
    provider = ProtonCLIProvider(cfg, state, logger, run=run, sleep=lambda _: None)
    with pytest.raises(ProtonCLIError, match="AUTH"):
        provider.list_folder("/my-files/Dropbox", "40_batches")
    assert len(calls) == 1


def test_trash_passes_every_path_in_one_call(state_context):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([(0, "", "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    provider.trash(["/my-files/Dropbox/a", "/my-files/Dropbox/b"], "50_trash")
    assert calls[0] == [
        "proton-drive",
        "filesystem",
        "trash",
        "/my-files/Dropbox/a",
        "/my-files/Dropbox/b",
    ]


def test_trash_with_no_paths_does_not_call_run(state_context):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    provider.trash([], "50_trash")
    assert calls == []


def test_empty_trash_argv(state_context):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([(0, "", "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    provider.empty_trash("60_empty_trash")
    assert calls[0] == ["proton-drive", "filesystem", "empty-trash"]


def test_root_uid_matches_expected(state_context):
    cfg, _, state, logger, _ = state_context
    listing = json.dumps(
        [
            {
                "uid": "uid-destination",
                "name": {"ok": True, "value": "Dropbox"},
                "type": "folder",
            }
        ]
    )
    run, calls = _fake_run([(0, listing, "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    assert provider.root_uid("00_probe") == "uid-destination"
    assert calls[0][-1] == "/my-files"


def test_root_uid_mismatch_raises(state_context):
    cfg, _, state, logger, _ = state_context
    listing = json.dumps(
        [{"uid": "other", "name": {"ok": True, "value": "Dropbox"}, "type": "folder"}]
    )
    run, _ = _fake_run([(0, listing, "")])
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    with pytest.raises(ProtonCLIError, match="did not exactly match"):
        provider.root_uid("00_probe")


def test_child_cli_path_escapes_or_uses_uid():
    assert (
        child_cli_path("/my-files/Dropbox", "a/b.txt", "u1", duplicate=False)
        == "/my-files/Dropbox/a\\/b.txt"
    )
    assert (
        child_cli_path("/my-files/Dropbox/", "x", "u1", duplicate=True)
        == "/my-files/Dropbox/u1"
    )


def test_upload_timeout_keeps_the_stderr_tail_as_evidence(
    state_context, tmp_path, monkeypatch
):
    cfg, _, state, logger, _ = state_context
    hooks = []
    popen, _ = _fake_popen([(0, "", "retrying after 429\nstill waiting\n", True)])
    monkeypatch.setattr(proton_cli, "KILL_GRACE_SECONDS", 0.0)
    provider = ProtonCLIProvider(
        cfg,
        state,
        logger,
        popen=popen,
        clock=_ticking_clock(cfg.proton.transfer_timeout_seconds / 2),
        after_call=lambda: hooks.append(1),
    )
    with pytest.raises(ProtonCLIError, match="timed out"):
        provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert hooks == [1]  # the session is written back even when the CLI is killed
    row = state.connection.execute(
        "SELECT message, safe_raw_error FROM events WHERE level='ERROR' ORDER BY id DESC"
    ).fetchone()
    assert "timed out after" in row["message"]
    assert "429" in row["safe_raw_error"]
    command = state.connection.execute(
        "SELECT exit_code, response_category FROM commands ORDER BY id DESC"
    ).fetchone()
    assert (command["exit_code"], command["response_category"]) == (-1, "TIMEOUT")


def test_upload_that_hangs_after_its_summary_is_terminated_and_accepted(
    state_context, tmp_path, monkeypatch
):
    cfg, _, state, logger, _ = state_context
    summary = '{"transferredItems":3,"transferredBytes":12,"skippedItems":0,"failedItems":0}\n'
    popen, _ = _fake_popen([(0, summary, "", True)])
    monkeypatch.setattr(proton_cli, "EXIT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(proton_cli, "KILL_GRACE_SECONDS", 0.0)
    monkeypatch.setenv("PROTON_DRIVE_CACHE_DIR", str(tmp_path))
    (tmp_path / "proton-drive.log").write_text("debug: disposing events manager\n")
    provider = ProtonCLIProvider(
        cfg, state, logger, popen=popen, clock=_ticking_clock(1.0)
    )
    out = provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert out == summary
    row = state.connection.execute(
        "SELECT message, safe_raw_error FROM events WHERE level='WARNING' ORDER BY id DESC"
    ).fetchone()
    assert "did not exit" in row["message"]
    assert "disposing events manager" in row["safe_raw_error"]


def test_download_that_hangs_after_its_summary_is_terminated_and_accepted(
    state_context, tmp_path, monkeypatch
):
    cfg, _, state, logger, _ = state_context
    popen, calls = _fake_popen(
        [(0, "Transfer summary:\n  Downloaded: 1 items (12 B)\n", "", True)]
    )
    monkeypatch.setattr(proton_cli, "EXIT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(proton_cli, "KILL_GRACE_SECONDS", 0.0)
    provider = ProtonCLIProvider(
        cfg, state, logger, popen=popen, clock=_ticking_clock(1.0), sleep=lambda _: None
    )
    provider.download_file("/my-files/Dropbox/a.txt", tmp_path / "dl", "40_batches")
    assert len(calls) == 1  # accepted on the first attempt, no retry
    command = state.connection.execute(
        "SELECT exit_code FROM commands ORDER BY id DESC"
    ).fetchone()
    assert command["exit_code"] == 0
