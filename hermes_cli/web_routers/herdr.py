"""Dashboard routes for the Herdr close/reopen feature (minimal: list + reopen).

``hermes_cli.herdr_pane_lifecycle`` / ``herdr_session_store`` own the real logic; these are thin
executor-offloaded wrappers (the ``herdr`` CLI subprocess call can block), same shape as
``web_routers/git.py``.
"""

import asyncio

from fastapi import APIRouter, HTTPException

from hermes_cli.web_models import HerdrReopenBody

router = APIRouter()


@router.get("/api/herdr/closed")
async def herdr_closed_sessions_route():
    from hermes_cli.herdr_session_store import list_records

    return {"records": await asyncio.to_thread(list_records)}


@router.post("/api/herdr/reopen")
async def herdr_reopen_route(body: HerdrReopenBody):
    from hermes_cli.herdr_pane_lifecycle import PaneLifecycleError, reopen

    try:
        return await asyncio.to_thread(reopen, body.target, machine_id=body.machine)
    except PaneLifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
