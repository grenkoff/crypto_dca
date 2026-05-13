from __future__ import annotations

from django.http import HttpRequest, JsonResponse


def healthz(_request: HttpRequest) -> JsonResponse:
    """Unauthenticated health check for Railway / uptime monitors.

    Intentionally avoids DB calls so a transient DB blip doesn't fail the check.
    """
    return JsonResponse({"status": "ok"})
