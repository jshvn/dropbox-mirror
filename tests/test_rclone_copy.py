from __future__ import annotations

import subprocess

import pytest

from migrator.providers.dropbox_rclone import DropboxRcloneProvider, RcloneError


def _run_returning(code, stderr=""):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, stdout="", stderr=stderr)

    return run, calls


def test_copy_accepts_vanished_exit_codes(state_context, tmp_path):
    cfg, paths, state, logger, _ = state_context
    run, calls = _run_returning(3)
    provider = DropboxRcloneProvider(cfg, paths, state, logger, run=run)
    listing = tmp_path / "files.txt"
    listing.write_text("A/b.txt\n", encoding="utf-8")
    code = provider.copy_files_from(
        listing, tmp_path / "staging", tmp_path / "copy.jsonl"
    )
    assert code == 3
    argv = calls[0]
    assert "--files-from-raw" in argv and "--no-traverse" in argv
    assert argv[argv.index("--tpslimit") + 1] == "10"
    assert argv[argv.index("--config") + 1] == str(paths.rclone_config)
    row = state.connection.execute("SELECT response_category FROM commands").fetchone()
    assert row["response_category"] == "EXIT_3"


def test_copy_rejects_other_failures(state_context, tmp_path):
    cfg, paths, state, logger, _ = state_context
    run, _ = _run_returning(7, "Fatal error")
    provider = DropboxRcloneProvider(cfg, paths, state, logger, run=run)
    listing = tmp_path / "files.txt"
    listing.write_text("A/b.txt\n", encoding="utf-8")
    with pytest.raises(RcloneError):
        provider.copy_files_from(listing, tmp_path / "staging", tmp_path / "copy.jsonl")
