from __future__ import annotations

import json
import subprocess

import pytest

from migrator.providers.dropbox_rclone import (
    DropboxRcloneProvider,
    RcloneError,
    dropbox_remote_path,
)


class RcloneRunner:
    def __init__(self, *, fail=False):
        self.fail = fail

    def __call__(self, argv, **kwargs):
        if "version" in argv:
            return subprocess.CompletedProcess(argv, 0, "rclone v1.75.0\n", "")
        if self.fail:
            partial = [{"Path": "partial.txt", "Size": 1, "IsDir": False}]
            return subprocess.CompletedProcess(
                argv, 1, json.dumps(partial), "Dropbox 504"
            )
        remote = argv[2]
        if remote.endswith(":/"):
            payload = [
                {
                    "Path": "file.txt",
                    "Name": "file.txt",
                    "Size": 3,
                    "IsDir": False,
                    "ID": "id:file",
                    "Hashes": {"dropbox": "lowercase-hash"},
                },
                {
                    "Path": "folder",
                    "Name": "folder",
                    "Size": -1,
                    "IsDir": True,
                    "ID": "id:folder",
                },
            ]
        else:
            payload = []
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def test_dropbox_remote_paths_are_root_namespace_anchored():
    assert dropbox_remote_path("dropbox", "") == "dropbox:/"
    assert dropbox_remote_path("dropbox", "Photos") == "dropbox:/Photos"
    assert (
        dropbox_remote_path("dropbox", "Photos", "Family/image.jpg")
        == "dropbox:/Photos/Family/image.jpg"
    )


def test_lowercase_dropbox_hash_is_required_source_evidence(state_context):
    cfg, paths, state, logger, _runtime = state_context
    inventory = DropboxRcloneProvider(
        cfg, paths, state, logger, run=RcloneRunner(), sleep=lambda _: None
    ).inventory()
    row = state.connection.execute(
        """
        SELECT dropbox_hash FROM rclone_objects
        WHERE inventory_id=? AND path='file.txt'
        """,
        (inventory,),
    ).fetchone()
    assert row["dropbox_hash"] == "lowercase-hash"


def test_failed_504_listing_never_commits_partial_stdout(state_context):
    cfg, paths, state, logger, _runtime = state_context
    with pytest.raises(RcloneError):
        DropboxRcloneProvider(
            cfg, paths, state, logger, run=RcloneRunner(fail=True), sleep=lambda _: None
        ).inventory()
    count = state.connection.execute(
        "SELECT COUNT(*) AS count FROM rclone_objects"
    ).fetchone()["count"]
    assert count == 0


def test_rclone_listing_timeout_never_commits_rows(state_context):
    cfg, paths, state, logger, _runtime = state_context

    def timeout(argv, **kwargs):
        if "version" in argv:
            return subprocess.CompletedProcess(argv, 0, "rclone v1.75.0\n", "")
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

    with pytest.raises(RcloneError):
        DropboxRcloneProvider(
            cfg, paths, state, logger, run=timeout, sleep=lambda _: None
        ).inventory()
    count = state.connection.execute(
        "SELECT COUNT(*) AS count FROM rclone_objects"
    ).fetchone()["count"]
    assert count == 0
