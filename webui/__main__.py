"""Web dashboard entrypoint: uvicorn bound to the configured host/port.

Run with ``python -m webui``. Bind to the WireGuard interface via
``WEBUI_HOST`` so the dashboard is only reachable over the VPN.
"""

from __future__ import annotations

import uvicorn

from core.config.bootstrap import bootstrap
from core.config.settings import webui_settings


def main() -> None:
    """Configure logging and serve the dashboard."""
    bootstrap()
    settings = webui_settings()
    uvicorn.run(
        "webui.app:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
