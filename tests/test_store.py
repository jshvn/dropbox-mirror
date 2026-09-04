from __future__ import annotations

import subprocess

import pytest

from migrator.paths import WorkPaths
from migrator.store import Store, StoreError


def _fake(responses):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        code, out, err = responses.pop(0)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)

    return run, calls


def test_get_put_copy_list_argv(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, calls = _fake(
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, '[{"Path":"a.age","Name":"a.age"}]', ""),
        ]
    )
    store = Store(runtime, paths, run=run)
    assert store.get(".state/x", tmp_path / "x") is True
    store.put(tmp_path / "x", ".state/y")
    store.copy(".state/history/h", ".state/state.sqlite.xz.age")
    assert store.list(".state/history/") == [".state/history/a.age"]
    assert calls[0][:3] == ["rclone", "copyto", "r2:test-bucket/.state/x"]
    assert calls[1][1:4] == ["copyto", str(tmp_path / "x"), "r2:test-bucket/.state/y"]
    assert calls[2][1:4] == [
        "copyto",
        "r2:test-bucket/.state/history/h",
        "r2:test-bucket/.state/state.sqlite.xz.age",
    ]
    assert calls[3][1:3] == ["lsjson", "r2:test-bucket/.state/history/"]
    assert all("--config" in argv for argv in calls)


def test_get_missing_returns_false_and_other_errors_raise(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, _ = _fake([(3, "", "directory not found"), (7, "", "boom")])
    store = Store(runtime, paths, run=run)
    assert store.get(".state/missing", tmp_path / "m") is False
    with pytest.raises(StoreError):
        store.get(".state/other", tmp_path / "o")


def test_list_of_absent_prefix_is_empty(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, _ = _fake([(3, "", "not found")])
    assert Store(runtime, paths, run=run).list(".state/history/") == []


def test_probe_raises_when_bucket_unreachable(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    run, _ = _fake([(3, "", "NoSuchBucket")])
    with pytest.raises(StoreError, match="reachable"):
        Store(runtime, paths, run=run).probe()


def test_bucket_required(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path, MIRROR_R2_BUCKET="")
    with pytest.raises(StoreError, match="MIRROR_R2_BUCKET"):
        Store(runtime, WorkPaths.from_runtime(runtime))
