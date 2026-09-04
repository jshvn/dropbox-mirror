from __future__ import annotations

import requests

from ..config import Config
from ..env import Runtime


class DropboxAuthError(RuntimeError):
    pass


def access_token(
    cfg: Config, runtime: Runtime, *, session: requests.Session | None = None
) -> str:
    missing = [
        name
        for name, value in (
            ("MIRROR_DROPBOX_APP_KEY", runtime.dropbox_app_key),
            ("MIRROR_DROPBOX_APP_SECRET", runtime.dropbox_app_secret),
            ("MIRROR_DROPBOX_REFRESH_TOKEN", runtime.dropbox_refresh_token),
        )
        if not value
    ]
    if missing:
        raise DropboxAuthError(f"required secret is unset: {', '.join(missing)}")
    http = session or requests.Session()
    try:
        response = http.post(
            cfg.dropbox.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": runtime.dropbox_refresh_token,
            },
            auth=(runtime.dropbox_app_key, runtime.dropbox_app_secret),
            timeout=cfg.dropbox.timeout_seconds,
        )
    except requests.RequestException as exc:
        raise DropboxAuthError("Dropbox token refresh failed on the network") from exc
    if response.status_code != 200:
        raise DropboxAuthError(
            f"Dropbox token refresh failed with HTTP {response.status_code}"
        )
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise DropboxAuthError("Dropbox token refresh returned no access token")
    return token
