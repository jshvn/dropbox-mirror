from __future__ import annotations

import io
import tarfile

import pytest
from conftest import FakeStore

from migrator import session
from migrator.paths import WorkPaths
from migrator.phases.base import PhaseError


def _ready(runtime_factory, tmp_path):
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    return runtime, paths


def test_restore_requires_object(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    with pytest.raises(PhaseError, match="session"):
        session.restore(runtime, paths, FakeStore())


def test_seal_then_restore_recovers_files(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    laptop = tmp_path / "pd"
    laptop.mkdir()
    (laptop / "auth-session.json").write_text('{"session":1}', encoding="utf-8")
    (laptop / "clientUid.json").write_text('{"uid":"c"}', encoding="utf-8")
    (laptop / "proton-drive.log").write_text("noise", encoding="utf-8")
    store = FakeStore()
    session.seal(runtime, paths, store, laptop)
    with tarfile.open(
        fileobj=io.BytesIO(store.objects[session.SESSION_KEY])
    ) as archive:
        names = archive.getnames()
    assert sorted(names) == ["auth-session.json", "clientUid.json"]
    session.restore(runtime, paths, store)
    assert (paths.session / "auth-session.json").read_text(
        encoding="utf-8"
    ) == '{"session":1}'
    assert (paths.session / "auth-session.json").stat().st_mode & 0o777 == 0o600


def test_writeback_only_when_session_changed(runtime_factory, tmp_path, plain_crypt):
    runtime, paths = _ready(runtime_factory, tmp_path)
    (paths.session / "auth-session.json").write_text("v1", encoding="utf-8")
    (paths.session / "clientUid.json").write_text("c", encoding="utf-8")
    store = FakeStore()
    assert session.writeback(runtime, paths, store) is True
    assert session.writeback(runtime, paths, store) is False
    (paths.session / "auth-session.json").write_text("v2-rotated", encoding="utf-8")
    assert session.writeback(runtime, paths, store) is True


def test_writeback_with_missing_session_file_is_noop(
    runtime_factory, tmp_path, plain_crypt
):
    runtime, paths = _ready(runtime_factory, tmp_path)
    assert session.writeback(runtime, paths, FakeStore()) is False
