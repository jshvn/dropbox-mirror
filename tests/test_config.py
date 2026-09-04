from __future__ import annotations

import pytest

from migrator.config import ConfigError, load_config

GOOD = """
[mirror]
id = "test"
[dropbox]
expected_account_id = "dbid:abc"
[proton]
expected_destination_uid = "uid-12345678"
"""


def _write(tmp_path, text):
    path = tmp_path / "mirror.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_and_derived_bytes(tmp_path):
    cfg = load_config(_write(tmp_path, GOOD))
    assert cfg.rclone.tps_limit == 10
    assert cfg.budget.batch_gb == 4
    assert cfg.budget.batch_files == 5000
    assert cfg.budget.batch_bytes == 4 * 1024**3
    assert cfg.budget.run_budget_minutes == 165
    assert cfg.budget.ceiling_gb == 4000
    assert cfg.proton.destination == "/my-files/Dropbox"
    assert cfg.reconcile.weekday == 0


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(_write(tmp_path, GOOD + "\n[budget]\nmax_batches = 4\n"))


def test_unknown_table_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown top-level"):
        load_config(_write(tmp_path, GOOD + "\n[safety]\nx = 1\n"))


def test_required_identity_guards(tmp_path):
    with pytest.raises(ConfigError, match="expected_account_id"):
        load_config(_write(tmp_path, GOOD.replace('"dbid:abc"', '""')))
    with pytest.raises(ConfigError, match="expected_destination_uid"):
        load_config(_write(tmp_path, GOOD.replace('"uid-12345678"', '"short"')))


def test_numeric_floors(tmp_path):
    with pytest.raises(ConfigError, match="batch_gb"):
        load_config(_write(tmp_path, GOOD + "\n[budget]\nbatch_gb = 0\n"))
    with pytest.raises(ConfigError, match="listing_floor_ratio"):
        load_config(_write(tmp_path, GOOD + "\n[budget]\nlisting_floor_ratio = 1.5\n"))
    with pytest.raises(ConfigError, match="weekday"):
        load_config(_write(tmp_path, GOOD + "\n[reconcile]\nweekday = 7\n"))
