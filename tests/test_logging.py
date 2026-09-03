from __future__ import annotations

from migrator.logging import RunLogger


def test_console_line_never_carries_object_identifier(tmp_path, capsys):
    logger = RunLogger(tmp_path, secrets=["s3cret"], console=True)
    logger.info(
        "10_inventory",
        "page",
        "committed page s3cret",
        object_identifier="/Taxes/2024.pdf",
    )
    out = capsys.readouterr().out
    assert "/Taxes/2024.pdf" not in out
    assert "s3cret" not in out
    assert "[REDACTED]" in out
    human = (tmp_path / "migrate.log").read_text(encoding="utf-8")
    assert "object=/Taxes/2024.pdf" in human
