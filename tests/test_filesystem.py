from __future__ import annotations

import os

from migrator.filesystem import comparison_key, walk_tree


def test_walk_tree_handles_files_unicode_and_symlinks_without_following(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "normal.txt").write_bytes(b"normal")
    (root / "empty").write_bytes(b"")
    (root / "café.txt").write_text("unicode", encoding="utf-8")
    target_dir = root / "target"
    target_dir.mkdir()
    (target_dir / "inside.txt").write_text("inside", encoding="utf-8")
    os.symlink("normal.txt", root / "file-link")
    os.symlink("target", root / "directory-link")
    os.symlink("missing-target", root / "broken-link")

    objects = {item.relative_path: item for item in walk_tree(root)}
    assert objects["normal.txt"].object_type == "file"
    assert objects["empty"].size == 0
    assert objects["café.txt"].sha256
    assert objects["file-link"].object_type == "file_symlink"
    assert objects["directory-link"].object_type == "directory_symlink"
    assert objects["broken-link"].object_type == "broken_symlink"
    assert objects["broken-link"].symlink_target == "missing-target"
    assert "directory-link/inside.txt" not in objects
    assert objects["target/inside.txt"].object_type == "file"


def test_case_and_unicode_comparison_collisions_are_detectable(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    (root / "Readme").write_text("one", encoding="utf-8")
    (root / "README").write_text("two", encoding="utf-8")
    (root / "café").write_text("nfc", encoding="utf-8")
    (root / "cafe\u0301").write_text("nfd", encoding="utf-8")
    keys = [item.comparison_key for item in walk_tree(root)]
    assert keys.count(comparison_key("readme")) == 2
    assert keys.count(comparison_key("café")) == 2
