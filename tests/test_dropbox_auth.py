from __future__ import annotations

import pytest

from migrator.providers.dropbox_auth import DropboxAuthError, access_token


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, data=None, auth=None, timeout=None):
        self.calls.append((url, data, auth))
        return self.response


def test_exchanges_refresh_token(config_factory, runtime_factory, tmp_path):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path)
    session = _Session(_Response(200, {"access_token": "sl.new", "expires_in": 14400}))
    assert access_token(cfg, runtime, session=session) == "sl.new"
    url, data, auth = session.calls[0]
    assert url == cfg.dropbox.token_url
    assert data == {"grant_type": "refresh_token", "refresh_token": "refresh-token"}
    assert auth == ("app-key", "app-secret")


def test_rejects_failure_without_leaking_body(
    config_factory, runtime_factory, tmp_path
):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path)
    session = _Session(_Response(400, {"error": "invalid_grant"}))
    with pytest.raises(DropboxAuthError, match="HTTP 400"):
        access_token(cfg, runtime, session=session)


def test_requires_all_three_secrets(config_factory, runtime_factory, tmp_path):
    cfg = config_factory(tmp_path)
    runtime = runtime_factory(tmp_path, MIRROR_DROPBOX_REFRESH_TOKEN="")
    with pytest.raises(DropboxAuthError, match="MIRROR_DROPBOX_REFRESH_TOKEN"):
        access_token(cfg, runtime, session=_Session(None))
