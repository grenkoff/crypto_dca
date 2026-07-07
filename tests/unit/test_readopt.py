from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from asgiref.sync import sync_to_async

from core.exchange.bybit import BybitClient
from core.exchange.types import Balance, Execution, Instrument, Side
from core.services.readopt import (
    READOPT_LEVEL_BASE,
    commit_readopt,
    plan_free_readopt,
)
from core.trading.models import Position, PositionStatus, StrategyConfig


class FakeClient:
    def __init__(self, *, free: Decimal, execs: list[Execution]) -> None:
        self._free = free
        self._execs = execs
        self.placed: list[dict[str, Any]] = []
        self.executions_calls = 0

    async def get_balances(self) -> dict[str, Balance]:
        return {"KAS": Balance(coin="KAS", free=self._free, locked=Decimal(0))}

    async def get_executions(self, symbol: str, *, limit: int = 50) -> list[Execution]:
        self.executions_calls += 1
        return self._execs

    async def place_limit(
        self,
        symbol: str,
        side: Side,
        qty: Decimal,
        price: Decimal,
        *,
        order_link_id: str | None = None,
        post_only: bool = True,
    ) -> str:
        self.placed.append({"side": side, "qty": qty, "price": price, "link": order_link_id})
        return f"ord-{len(self.placed)}"


def _instrument() -> Instrument:
    return Instrument(
        symbol="KASUSDT",
        base_coin="KAS",
        quote_coin="USDT",
        tick_size=Decimal("0.00001"),
        lot_size=Decimal("0.01"),
        min_order_qty=Decimal("0.01"),
        min_order_amt=Decimal("5"),
    )


def _buy(price: str, qty: str, secs: int) -> Execution:
    return Execution(
        exec_id=f"e{secs}",
        order_id=f"o{secs}",
        symbol="KASUSDT",
        side=Side.BUY,
        price=Decimal(price),
        qty=Decimal(qty),
        fee=Decimal(0),
        fee_coin="KAS",
        executed_at=datetime(2026, 7, 6, tzinfo=UTC) + timedelta(seconds=secs),
    )


pytestmark = pytest.mark.django_db(transaction=True)


@sync_to_async
def _config() -> StrategyConfig:
    cfg = StrategyConfig.load()
    cfg.symbol = "KASUSDT"
    cfg.tp_step = Decimal("0.00010")
    cfg.min_profit_quote = Decimal(0)
    cfg.maker_fee = Decimal("0.000625")
    cfg.save()
    return cfg


async def test_plan_early_out_when_free_below_min_notional() -> None:
    # 100 KAS * 0.03 = $3 < $5 min notional -> nothing, and history is never pulled.
    client = FakeClient(free=Decimal("100"), execs=[_buy("0.0300", "100", 1)])
    plan = await plan_free_readopt(
        client=cast(BybitClient, client),
        config=await _config(),
        instrument=_instrument(),
        price=Decimal("0.0300"),
    )
    assert plan == []
    assert client.executions_calls == 0


async def test_plan_reconstructs_entry_and_tp_above_market() -> None:
    # Bought 300 @ 0.0298, none sold -> free 300. TP = entry + tp_step, above market.
    client = FakeClient(free=Decimal("300"), execs=[_buy("0.0298", "300", 1)])
    plan = await plan_free_readopt(
        client=cast(BybitClient, client),
        config=await _config(),
        instrument=_instrument(),
        price=Decimal("0.0297"),
    )
    assert len(plan) == 1
    (p,) = plan
    assert p.level_index == READOPT_LEVEL_BASE
    assert p.entry == Decimal("0.0298")
    # DUST_KEEP trims 0.1% then floors to lot size.
    assert p.qty == Decimal("299.70")
    assert p.tp_price == Decimal("0.02990")  # entry + tp_step
    assert p.tp_price > Decimal("0.0297")  # above market


async def test_plan_floors_tp_above_market_when_entry_below_price() -> None:
    # Entry 0.0295 but market moved up to 0.0300: entry+tp_step would sit below
    # market, so the TP is floored one tick above the market instead.
    client = FakeClient(free=Decimal("300"), execs=[_buy("0.0295", "300", 1)])
    plan = await plan_free_readopt(
        client=cast(BybitClient, client),
        config=await _config(),
        instrument=_instrument(),
        price=Decimal("0.0300"),
    )
    (p,) = plan
    assert p.tp_price == Decimal("0.03001")  # one tick above 0.0300


async def test_plan_skips_sub_minimum_lots_instead_of_absurd_tp() -> None:
    # Free coin is enough overall ($8+), but reconstructs into one ~$5 lot plus two
    # dust lots. The dust must be SKIPPED (left free) — never adopted with a TP
    # priced far above market just to reach the $5 minimum notional.
    execs = [
        _buy("0.0294", "170", 1),
        _buy("0.0295", "100", 2),
        _buy("0.0296", "16", 3),
    ]
    client = FakeClient(free=Decimal("286"), execs=execs)
    plan = await plan_free_readopt(
        client=cast(BybitClient, client),
        config=await _config(),
        instrument=_instrument(),
        price=Decimal("0.0293"),
    )
    assert len(plan) == 1  # only the ~$5 lot survives
    (p,) = plan
    assert p.entry == Decimal("0.0294")
    assert p.tp_price == Decimal("0.02950")  # entry + tp_step, sane — no absurd lift
    assert all(x.qty * x.tp_price >= Decimal("5") for x in plan)


async def test_commit_places_sells_and_writes_positions() -> None:
    client = FakeClient(free=Decimal("300"), execs=[_buy("0.0298", "300", 1)])
    cfg = await _config()
    plan = await plan_free_readopt(
        client=cast(BybitClient, client),
        config=cfg,
        instrument=_instrument(),
        price=Decimal("0.0297"),
    )
    placed = await commit_readopt(
        client=cast(BybitClient, client), symbol="KASUSDT", config=cfg, plan=plan
    )

    assert len(placed) == 1
    assert len(client.placed) == 1
    order = client.placed[0]
    assert order["side"] == Side.SELL
    assert order["qty"] == Decimal("299.70")

    pos = await sync_to_async(Position.objects.get)(level_index=READOPT_LEVEL_BASE)
    assert pos.status == PositionStatus.OPEN
    assert pos.entry_price == Decimal("0.0298")
    assert pos.tp_order_id == "ord-1"
    assert pos.fees_in == Decimal("0.0298") * Decimal("299.70") * cfg.maker_fee


async def test_next_level_increments_over_existing_readopts() -> None:
    await sync_to_async(Position.objects.create)(
        level_index=READOPT_LEVEL_BASE,
        entry_price=Decimal("0.0290"),
        qty=Decimal("100"),
        tp_price=Decimal("0.0291"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )
    client = FakeClient(free=Decimal("300"), execs=[_buy("0.0298", "300", 1)])
    plan = await plan_free_readopt(
        client=cast(BybitClient, client),
        config=await _config(),
        instrument=_instrument(),
        price=Decimal("0.0297"),
    )
    assert plan[0].level_index == READOPT_LEVEL_BASE + 1
