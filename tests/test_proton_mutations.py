from __future__ import annotations

import json
import subprocess

import pytest

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


def test_upload_tree_argv_and_hook(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    run, calls = _fake_run([(0, '{"uploaded":1}\n', "")])
    hooks = []
    provider = ProtonCLIProvider(
        cfg, state, logger, run=run, after_call=lambda: hooks.append(1)
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
    run, _ = _fake_run(
        [(1, '{"uploaded":1,"failed":1}\n', "one item could not be uploaded")]
    )
    provider = ProtonCLIProvider(cfg, state, logger, run=run)
    out = provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert out.startswith('{"uploaded"')


def test_upload_tree_failure_raises_and_still_hooks(state_context, tmp_path):
    cfg, _, state, logger, _ = state_context
    run, _ = _fake_run([(1, "", "You need to login first")])
    hooks = []
    provider = ProtonCLIProvider(
        cfg, state, logger, run=run, after_call=lambda: hooks.append(1)
    )
    with pytest.raises(ProtonCLIError, match="AUTH"):
        provider.upload_tree([tmp_path / "A"], "/my-files/Dropbox", "40_batches")
    assert hooks == [1]


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
