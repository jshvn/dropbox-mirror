from __future__ import annotations

from .. import session
from ..providers.proton_cli import ProtonCLIProvider
from ..store import Store
from .base import PhaseContext, PhaseResult

PHASE = "80_empty_trash"


def run(ctx: PhaseContext) -> PhaseResult:
    if not ctx.apply:
        return PhaseResult(status="PLANNED", outputs={"planned": "empty Proton trash"})
    store = Store(ctx.runtime, ctx.paths)
    proton = ProtonCLIProvider(
        ctx.cfg,
        ctx.state,
        ctx.logger,
        after_call=lambda: session.writeback(ctx.runtime, ctx.paths, store),
    )
    proton.root_uid(PHASE)
    proton.empty_trash(PHASE)
    ctx.logger.info(PHASE, "gate", "Proton trash emptied by operator request")
    return PhaseResult(outputs={"emptied": True})
