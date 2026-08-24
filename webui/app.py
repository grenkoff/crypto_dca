"""FastAPI dashboard: status/PnL/positions plus authenticated control.

Served behind the WireGuard VPN (bind host from ``WEBUI_*`` settings). Reads
go through the async DAO; control actions require an admin control token
(``Authorization: Bearer <token>``, issued by the tgbot ``/token``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core.config.settings import redis_settings
from core.services import repository
from core.services.redis_bus import RedisEventBus
from core.services.tokens import hash_token
from webui import queries


def _num(value: Decimal) -> str:
    """Render a Decimal without trailing zeros or scientific notation."""
    return format(value.normalize(), "f")


_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)
_TEMPLATES.env.filters["num"] = _num


def _to_json(view: queries.DashboardView) -> dict[str, Any]:
    """Serialise a dashboard snapshot to JSON-safe primitives."""
    return {
        "symbol": view.symbol,
        "paused": view.paused,
        "open_positions": view.open_positions,
        "started_at": view.started_at.isoformat() if view.started_at else None,
        "last_heartbeat": (
            view.last_heartbeat.isoformat() if view.last_heartbeat else None
        ),
        "pnl": {
            "today": _num(view.pnl_today),
            "last_24h": _num(view.pnl_24h),
            "last_7d": _num(view.pnl_7d),
            "last_30d": _num(view.pnl_30d),
            "all_time": _num(view.pnl_all),
        },
        "deployed": _num(view.deployed),
        "compensations_24h": view.compensations_24h,
        "orders": [
            {
                "level_index": o.level_index,
                "entry_price": _num(o.entry_price),
                "qty": _num(o.qty),
                "tp_price": _num(o.tp_price)
                if o.tp_price is not None
                else None,
            }
            for o in view.orders
        ],
        "generated_at": view.generated_at.isoformat(),
    }


async def _subscribe_events() -> AsyncIterator[dict[str, Any]]:
    """Yield trader events from the Redis channel (empty if no Redis)."""
    url = redis_settings().redis_url
    if not url:
        return
    bus = RedisEventBus(url)
    try:
        async for event in bus.subscribe():
            yield event
    finally:
        await bus.close()


class ConfigUpdate(BaseModel):
    """Optional strategy-config fields to change from the dashboard."""

    grid_step: Decimal | None = None
    tp_step: Decimal | None = None
    order_qty_quote: Decimal | None = None
    min_profit_quote: Decimal | None = None
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    max_open_orders: int | None = None
    comp_share_min: Decimal | None = None
    comp_share_max: Decimal | None = None
    comp_hole_offset: Decimal | None = None
    grid_mode: str | None = None
    symbol: str | None = None


async def _require_admin(authorization: str = Header(default="")) -> str:
    """Resolve the actor from a Bearer control token, or 401."""
    prefix = "Bearer "
    token = (
        authorization[len(prefix) :].strip()
        if authorization.startswith(prefix)
        else ""
    )
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    user = await repository.find_admin_by_token(hash_token(token))
    if user is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return user.label or str(user.chat_id)


def _read_routes(app: FastAPI) -> None:
    """Register the read-only pages, JSON API and event stream."""

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.get("/api/dashboard")
    async def api_dashboard() -> JSONResponse:
        """Return the dashboard snapshot as JSON."""
        return JSONResponse(_to_json(await queries.dashboard_data()))

    @app.get("/api/config")
    async def api_config() -> JSONResponse:
        """Return the current editable strategy config (for the form)."""
        try:
            cfg = await repository.get_config()
        except ValueError as exc:
            raise HTTPException(
                status_code=404, detail="not configured"
            ) from exc
        return JSONResponse(
            {
                "symbol": cfg.symbol,
                "grid_mode": cfg.grid_mode,
                "grid_step": _num(cfg.grid_step),
                "tp_step": _num(cfg.tp_step),
                "order_qty_quote": _num(cfg.order_qty_quote),
                "min_profit_quote": _num(cfg.min_profit_quote),
                "maker_fee": _num(cfg.maker_fee),
                "taker_fee": _num(cfg.taker_fee),
                "max_open_orders": cfg.max_open_orders,
                "comp_share_min": _num(cfg.comp_share_min),
                "comp_share_max": _num(cfg.comp_share_max),
                "comp_hole_offset": _num(cfg.comp_hole_offset),
            }
        )

    @app.get("/api/audit")
    async def api_audit() -> JSONResponse:
        """Return the recent control-audit entries as JSON."""
        rows = await repository.recent_audit()
        return JSONResponse(
            [
                {
                    "actor": r.actor,
                    "action": r.action,
                    "detail": r.detail,
                    "at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Render the server-side dashboard page."""
        return _TEMPLATES.TemplateResponse(
            request, "dashboard.html", {"d": await queries.dashboard_data()}
        )

    @app.get("/fragment", response_class=HTMLResponse)
    async def fragment(request: Request) -> HTMLResponse:
        """Render just the snapshot block (for live in-place refresh)."""
        return _TEMPLATES.TemplateResponse(
            request, "_snapshot.html", {"d": await queries.dashboard_data()}
        )

    @app.websocket("/ws")
    async def ws_events(websocket: WebSocket) -> None:
        """Stream trader events to the client until it disconnects."""
        await websocket.accept()
        try:
            async for event in _subscribe_events():
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            await _close_quietly(websocket)


def _control_routes(app: FastAPI) -> None:
    """Register the authenticated control actions."""

    @app.post("/control/pause")
    async def control_pause(
        actor: str = Depends(_require_admin),
    ) -> dict[str, Any]:
        """Pause the trader (stops placing new buys)."""
        await repository.set_paused(paused=True, actor=actor)
        return {"paused": True}

    @app.post("/control/resume")
    async def control_resume(
        actor: str = Depends(_require_admin),
    ) -> dict[str, Any]:
        """Resume the trader."""
        await repository.set_paused(paused=False, actor=actor)
        return {"paused": False}

    @app.post("/control/config")
    async def control_config(
        body: ConfigUpdate, actor: str = Depends(_require_admin)
    ) -> dict[str, Any]:
        """Apply validated strategy-config changes."""
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="no fields to update")
        try:
            cfg = await repository.update_config(actor=actor, updates=updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "symbol": cfg.symbol,
            "grid_mode": cfg.grid_mode,
            "grid_step": _num(cfg.grid_step),
            "order_qty_quote": _num(cfg.order_qty_quote),
            "max_open_orders": cfg.max_open_orders,
        }


def create_app() -> FastAPI:
    """Build the dashboard FastAPI application."""
    app = FastAPI(title="crypto_dca dashboard", docs_url=None, redoc_url=None)
    _read_routes(app)
    _control_routes(app)
    return app


async def _close_quietly(websocket: WebSocket) -> None:
    """Close a WebSocket, ignoring an already-closed connection."""
    with contextlib.suppress(RuntimeError):
        await websocket.close()


app = create_app()
