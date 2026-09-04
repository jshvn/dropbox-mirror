from __future__ import annotations

from pathlib import Path

import pytest

from migrator import crypt, session
from migrator.config import Budget, Config, Dropbox, Mirror, Proton, Rclone, Reconcile
from migrator.env import Runtime
from migrator.logging import RunLogger
from migrator.paths import WorkPaths
from migrator.state import State

TEST_ENV = {
    "MIRROR_R2_BUCKET": "test-bucket",
    "MIRROR_AGE_IDENTITY": "AGE-SECRET-KEY-1TEST",
    "MIRROR_HEALTHCHECK_URL": "https://hc.example/ping/x",
    "MIRROR_DROPBOX_APP_KEY": "app-key",
    "MIRROR_DROPBOX_APP_SECRET": "app-secret",
    "MIRROR_DROPBOX_REFRESH_TOKEN": "refresh-token",
    "RCLONE_CONFIG_DROPBOX_TOKEN": '{"access_token":"rclone-token"}',
    "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": "r2-secret",
}


@pytest.fixture
def runtime_factory():
    def make(tmp_path: Path, **overrides) -> Runtime:
        env = {
            **TEST_ENV,
            "MIRROR_WORK_DIR": str(tmp_path / "run"),
            "MIRROR_CONFIG": str(tmp_path / "mirror.toml"),
            "MIRROR_RUN_EPOCH": "1700000000",
        }
        env.update({k: str(v) for k, v in overrides.items()})
        return Runtime.from_environ(env)

    return make


@pytest.fixture
def config_factory():
    def make(tmp_path: Path, **overrides) -> Config:
        source = tmp_path / "mirror.toml"
        source.write_text("# test\n", encoding="utf-8")
        return Config(
            mirror=Mirror("test"),
            dropbox=overrides.get(
                "dropbox",
                Dropbox(
                    expected_account_id="dbid:test-account",
                    minimum_call_interval_seconds=0,
                    max_attempts=3,
                    initial_backoff_seconds=0,
                ),
            ),
            rclone=overrides.get("rclone", Rclone(retries=2)),
            proton=overrides.get(
                "proton",
                Proton(
                    expected_destination_uid="uid-destination",
                    initial_backoff_seconds=0,
                ),
            ),
            budget=overrides.get("budget", Budget()),
            reconcile=overrides.get("reconcile", Reconcile()),
            source_file=source,
            source_sha256="test-config-hash",
        )

    return make


@pytest.fixture
def state_context(tmp_path, config_factory, runtime_factory):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path)
    paths = WorkPaths.from_runtime(runtime)
    paths.ensure()
    state = State(paths.state_db, cfg.mirror.id)
    state.initialize_migration(cfg.source_file, cfg.source_sha256)
    logger = RunLogger(
        paths.logs, secrets=runtime.secrets(), console=False, sink=state.record_event
    )
    try:
        yield cfg, paths, state, logger, runtime
    finally:
        state.close()


class FakeStore:
    """In-memory stand-in for migrator.store.Store."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get(self, key, target: Path) -> bool:
        if key not in self.objects:
            return False
        target.write_bytes(self.objects[key])
        return True

    def put(self, source: Path, key) -> None:
        self.objects[key] = source.read_bytes()

    def copy(self, s, t):
        self.objects[t] = self.objects[s]

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def probe(self):
        pass


@pytest.fixture
def plain_crypt(monkeypatch):
    # age is exercised in the image build; here encryption is identity so tar bytes are inspectable.
    monkeypatch.setattr(
        crypt,
        "encrypt",
        lambda identity, key_file, source, target, run=None: target.write_bytes(
            source.read_bytes()
        ),
    )
    monkeypatch.setattr(
        crypt,
        "decrypt",
        lambda identity, key_file, source, target, run=None: target.write_bytes(
            source.read_bytes()
        ),
    )
    monkeypatch.setattr(session, "_last_digest", None)
