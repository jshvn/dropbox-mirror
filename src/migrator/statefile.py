from __future__ import annotations

import lzma

from . import crypt
from .env import Runtime
from .paths import WorkPaths
from .phases.base import PhaseError
from .state import State
from .store import Store

STATE_KEY = ".state/state.sqlite.xz.age"
HISTORY_PREFIX = ".state/history/"


def fetch(runtime: Runtime, paths: WorkPaths, store: Store) -> str:
    """Restore state.sqlite from R2. A missing object is an empty mirror only on the
    first run ever, which is when the history prefix is empty as well."""
    encrypted = paths.root / "state.sqlite.xz.age"
    try:
        if not store.get(STATE_KEY, encrypted):
            if store.list(HISTORY_PREFIX):
                raise PhaseError(
                    "state object is missing but history exists; a lost state must never "
                    "be mistaken for an empty mirror. Roll back with `task state-rollback`."
                )
            store.probe()  # "fresh" is only believable from a bucket that answers
            return "fresh"
        compressed = paths.root / "state.sqlite.xz"
        try:
            crypt.decrypt(runtime.age_identity, paths.age_key, encrypted, compressed)
            with lzma.open(compressed, "rb") as source:
                paths.state_db.write_bytes(source.read())
        finally:
            compressed.unlink(missing_ok=True)
        return "restored"
    finally:
        encrypted.unlink(missing_ok=True)


def push(
    state: State, runtime: Runtime, paths: WorkPaths, store: Store, label: str
) -> None:
    snapshot = paths.root / "state.snapshot.sqlite"
    compressed = paths.root / "state.sqlite.xz"
    encrypted = paths.root / "state.sqlite.xz.age"
    try:
        state.snapshot_to(snapshot)
        with (
            open(snapshot, "rb") as source,
            lzma.open(compressed, "wb", preset=6) as target,
        ):
            target.write(source.read())
        crypt.encrypt(runtime.age_identity, paths.age_key, compressed, encrypted)
        history_key = f"{HISTORY_PREFIX}{label}.sqlite.xz.age"
        store.put(encrypted, history_key)
        store.copy(
            history_key, STATE_KEY
        )  # server-side; the blob crosses the wire once
    finally:
        for path in (snapshot, compressed, encrypted):
            path.unlink(missing_ok=True)


def rollback(store: Store, history_key: str) -> None:
    store.copy(history_key, STATE_KEY)
