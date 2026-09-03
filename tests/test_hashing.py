from __future__ import annotations

import io

from migrator.hashing import DROPBOX_BLOCK_SIZE, hash_stream


def test_empty_file_hashes():
    result = hash_stream(io.BytesIO(b""))
    assert result.size == 0
    assert (
        result.dropbox_content_hash
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert result.sha256 == result.dropbox_content_hash


def test_single_block_published_algorithm_vector():
    result = hash_stream(io.BytesIO(b"abc"))
    assert result.sha256 == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert result.dropbox_content_hash == (
        "4f8b42c22dd3729b519ba6f68d2da7cc5b2d606d05daed5ad5128cc03e6c6358"
    )


def test_exact_and_multiple_dropbox_blocks():
    exact = hash_stream(io.BytesIO(b"a" * DROPBOX_BLOCK_SIZE))
    assert exact.dropbox_content_hash == (
        "907a506cf5e706bda5c7a29b43c9c65d8344bd2fa2f22339b359c214812af5a1"
    )
    extra = hash_stream(io.BytesIO(b"a" * DROPBOX_BLOCK_SIZE + b"b"))
    assert extra.dropbox_content_hash == (
        "565546ad93383e225e7cf808fb4d527a54dec54826a5c34a24c1f19a03c62583"
    )
