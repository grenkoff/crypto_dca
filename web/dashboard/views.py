from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from core.trading.models import BotStatus, Position, PositionStatus, StrategyConfig
from web.dashboard.forms import StrategyConfigForm


@login_required
def index(request: HttpRequest) -> HttpResponse:
    bot = BotStatus.load()
    config = StrategyConfig.load()
    open_positions = Position.objects.filter(status=PositionStatus.OPEN).order_by("level_index")
    closed_recent = Position.objects.filter(status=PositionStatus.CLOSED).order_by("-closed_at")[
        :20
    ]

    now = datetime.now(tz=UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    closed = Position.objects.filter(status=PositionStatus.CLOSED)

    def _sum(qs) -> Decimal:  # type: ignore[no-untyped-def]
        return qs.aggregate(s=Sum("realized_pnl"))["s"] or Decimal(0)

    pnl = {
        "today": _sum(closed.filter(closed_at__gte=today_start)),
        "week": _sum(closed.filter(closed_at__gte=week_start)),
        "total": _sum(closed),
    }

    return render(
        request,
        "dashboard/index.html",
        {
            "bot": bot,
            "config": config,
            "open_positions": open_positions,
            "closed_recent": closed_recent,
            "pnl": pnl,
        },
    )


@login_required
def config(request: HttpRequest) -> HttpResponse:
    instance = StrategyConfig.load()
    if request.method == "POST":
        form = StrategyConfigForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            return redirect("dashboard:config")
    else:
        form = StrategyConfigForm(instance=instance)
    return render(request, "dashboard/config.html", {"form": form})
