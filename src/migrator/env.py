from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _int(environ: Mapping[str, str], name: str) -> int | None:
    raw = environ.get(name, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Runtime:
    work_dir: Path
    config_path: Path
    run_epoch: int | None
    budget_override: int | None
    reconcile: bool
    verbose: bool
    r2_bucket: str
    age_identity: str
    healthcheck_url: str
    dropbox_app_key: str
    dropbox_app_secret: str
    dropbox_refresh_token: str
    rclone_dropbox_token: str
    r2_secret: str
    host: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> Runtime:
        env = os.environ if environ is None else environ
        run_id = env.get("GITHUB_RUN_ID", "")
        return cls(
            work_dir=Path(env.get("MIRROR_WORK_DIR", ".run")),
            config_path=Path(env.get("MIRROR_CONFIG", "config/mirror.toml")),
            run_epoch=_int(env, "MIRROR_RUN_EPOCH"),
            budget_override=_int(env, "RUN_BUDGET_MIN"),
            reconcile=env.get("RECONCILE", "").lower() == "true",
            verbose=env.get("MIRROR_VERBOSE", "") == "1",
            r2_bucket=env.get("MIRROR_R2_BUCKET", ""),
            age_identity=env.get("MIRROR_AGE_IDENTITY", ""),
            healthcheck_url=env.get("MIRROR_HEALTHCHECK_URL", ""),
            dropbox_app_key=env.get("MIRROR_DROPBOX_APP_KEY", ""),
            dropbox_app_secret=env.get("MIRROR_DROPBOX_APP_SECRET", ""),
            dropbox_refresh_token=env.get("MIRROR_DROPBOX_REFRESH_TOKEN", ""),
            rclone_dropbox_token=env.get("RCLONE_CONFIG_DROPBOX_TOKEN", ""),
            r2_secret=env.get("RCLONE_CONFIG_R2_SECRET_ACCESS_KEY", ""),
            host=f"github:{run_id}" if run_id else socket.gethostname(),
        )

    def secrets(self) -> list[str]:
        values = (
            self.age_identity,
            self.healthcheck_url,
            self.dropbox_app_secret,
            self.dropbox_refresh_token,
            self.rclone_dropbox_token,
            self.r2_secret,
        )
        return [value for value in values if value]
