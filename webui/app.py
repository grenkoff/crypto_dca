"""FastAPI dashboard: read-only status, PnL and open positions.

Served behind the WireGuard VPN (bind host from ``WEBUI_*`` settings). All
data is read through the async DAO; no control actions live here yet.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

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


def create_app() -> FastAPI:
    """Build the dashboard FastAPI application."""
    app = FastAPI(title="crypto_dca dashboard", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @app.get("/api/dashboard")
    async def api_dashboard() -> JSONResponse:
        """Return the dashboard snapshot as JSON."""
        return JSONResponse(_to_json(await queries.dashboard_data()))

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        """Render the server-side dashboard page."""
        return _TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"d": await queries.dashboard_data()},
        )

    return app


app = create_app()
