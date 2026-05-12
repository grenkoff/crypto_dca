from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.db.models import Sum
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from core.trading.models import BotStatus, Position, PositionStatus, StrategyConfig
from web.api.serializers import BotStatusSerializer, PositionSerializer, StrategyConfigSerializer


@api_view(["GET"])
def status(request: Request) -> Response:
    bot = BotStatus.load()
    open_count = Position.objects.filter(status=PositionStatus.OPEN).count()
    data = BotStatusSerializer(bot).data
    data["open_positions"] = open_count
    return Response(data)


@api_view(["GET"])
def positions(request: Request) -> Response:
    status_q = request.query_params.get("status", "open")
    qs = Position.objects.all().order_by("-opened_at")
    if status_q == "open":
        qs = qs.filter(status=PositionStatus.OPEN)
    elif status_q == "closed":
        qs = qs.filter(status=PositionStatus.CLOSED)
    qs = qs[:200]
    return Response(PositionSerializer(qs, many=True).data)


@api_view(["GET"])
def pnl(request: Request) -> Response:
    now = datetime.now(tz=UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    closed = Position.objects.filter(status=PositionStatus.CLOSED)

    def _sum(qs) -> Decimal:  # type: ignore[no-untyped-def]
        return qs.aggregate(s=Sum("realized_pnl"))["s"] or Decimal(0)

    return Response(
        {
            "today": str(_sum(closed.filter(closed_at__gte=today_start))),
            "week": str(_sum(closed.filter(closed_at__gte=week_start))),
            "total": str(_sum(closed)),
        }
    )


@api_view(["GET"])
def config(request: Request) -> Response:
    return Response(StrategyConfigSerializer(StrategyConfig.load()).data)


@api_view(["POST"])
def pause(request: Request) -> Response:
    bot = BotStatus.load()
    bot.paused = True
    bot.save()
    return Response({"paused": True})


@api_view(["POST"])
def resume(request: Request) -> Response:
    bot = BotStatus.load()
    bot.paused = False
    bot.save()
    return Response({"paused": False})
