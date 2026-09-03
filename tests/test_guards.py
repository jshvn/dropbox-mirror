from __future__ import annotations

from pathlib import Path

import pytest

from migrator.guards import (
    GuardError,
    contained_path,
    validate_dropbox_base_url,
    validate_dropbox_scope,
)


def test_dropbox_endpoints_and_scope_are_fail_closed():
    validate_dropbox_base_url(
        "https://api.dropboxapi.com/2",
        expected_host="api.dropboxapi.com",
        label="api",
    )
    assert validate_dropbox_scope("Clients/Example") == "Clients/Example"
    with pytest.raises(GuardError):
        validate_dropbox_base_url(
            "https://example.com/2",
            expected_host="api.dropboxapi.com",
            label="api",
        )
    with pytest.raises(GuardError):
        validate_dropbox_scope("../other")


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "a//b", "a/./b"])
def test_contained_path_rejects_unsafe_relative_paths(tmp_path: Path, relative: str):
    with pytest.raises(GuardError):
        contained_path(tmp_path, relative)


def test_contained_path_rejects_symlinked_parent(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(GuardError, match="symlinked parent"):
        contained_path(root, "link/file")
