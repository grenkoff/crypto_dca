"""Tests for putting coin the book does not cover back to work."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from core.db.models import Position, PositionStatus, StrategyConfig
from core.exchange.types import Balance, Instrument, Side
from core.services import repository
from core.services.adopt import (
    SpareAdopter,
    plan_adoption,
    spare_coin,
)
from core.services.events import NoOpEventBus
from core.services.order_manager import OrderManager
from tests.conftest import add_rows

_INSTRUMENT = Instrument(
    symbol="KASUSDT",
    base_coin="KAS",
    quote_coin="USDT",
    tick_size=Decimal("0.00001"),
    lot_size=Decimal("0.01"),
    min_order_qty=Decimal("0.01"),
    min_order_amt=Decimal("5"),
)
_PRICE = Decimal("0.03570")


def _lot(qty: str, filled: str = "0") -> Position:
    return Position(
        level_index=900,
        entry_price=Decimal("0.03"),
        qty=Decimal(qty),
        filled_qty=Decimal(filled),
        tp_order_id="tp-1",
        tp_price=Decimal("0.0304"),
        status=PositionStatus.OPEN,
        opened_at=datetime.now(tz=UTC),
    )


def test_spare_is_the_wallet_minus_what_the_lots_hold() -> None:
    balances = {
        "KAS": Balance(coin="KAS", free=Decimal("300"), locked=Decimal("100"))
    }
    assert spare_coin(balances, [_lot("100")], "KAS") == Decimal("300")


def test_a_partly_sold_lot_only_holds_what_is_left() -> None:
    balances = {
        "KAS": Balance(coin="KAS", free=Decimal("400"), locked=Decimal("0"))
    }
    # 100 bought, 40 already sold -> the lot still accounts for 60
    assert spare_coin(balances, [_lot("100", "40")], "KAS") == Decimal("340")


def test_spare_is_never_negative() -> None:
    balances = {
        "KAS": Balance(coin="KAS", free=Decimal("10"), locked=Decimal("0"))
    }
    assert spare_coin(balances, [_lot("100")], "KAS") == Decimal(0)


def test_a_missing_coin_reads_as_no_spare() -> None:
    assert spare_coin({}, [], "KAS") == Decimal(0)


def test_spare_splits_into_grid_sized_lots() -> None:
    plan = plan_adoption(
        spare=Decimal("1000"),
        entry_price=_PRICE,
        order_qty_quote=Decimal("7"),
        instrument=_INSTRUMENT,
    )
    # 7 USDT at 0.0357 buys 196.07 KAS, so 1000 makes five of them
    assert plan.lot_qty == Decimal("196.07")
    assert plan.lots == 5
    assert plan.total_qty == Decimal("980.35")
    assert plan.leftover == Decimal("19.65")


def test_less_than_one_lot_is_left_alone() -> None:
    plan = plan_adoption(
        spare=Decimal("100"),
        entry_price=_PRICE,
        order_qty_quote=Decimal("7"),
        instrument=_INSTRUMENT,
    )
    assert plan.lots == 0
    assert plan.leftover == Decimal("100")


def test_nothing_to_adopt_is_not_an_error() -> None:
    for spare, price in (
        (Decimal(0), _PRICE),
        (Decimal("-5"), _PRICE),
        (Decimal("1000"), Decimal(0)),
    ):
        plan = plan_adoption(
            spare=spare,
            entry_price=price,
            order_qty_quote=Decimal("7"),
            instrument=_INSTRUMENT,
        )
        assert plan.lots == 0


pytestmark_db = pytest.mark.db


class _FakeClient:
    """Records placed orders and serves one balance snapshot."""

    def __init__(self, kas: str) -> None:
        self.kas = Decimal(kas)
        self.placed: list[dict[str, Any]] = []

    async def get_balances(self) -> dict[str, Balance]:
        return {
            "KAS": Balance(coin="KAS", free=self.kas, locked=Decimal(0)),
            "USDT": Balance(
                coin="USDT", free=Decimal("10"), locked=Decimal(0)
            ),
        }

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
        self.placed.append({"side": side, "qty": qty, "price": price})
        return f"adopt-{len(self.placed)}"


def _config() -> StrategyConfig:
    return StrategyConfig(
        symbol="KASUSDT",
        grid_mode="percent",
        grid_step=Decimal("0.0011"),
        tp_step=Decimal("0.0066"),
        order_qty_quote=Decimal("7"),
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0.000625"),
        taker_fee=Decimal("0.00075"),
        max_open_orders=50,
        comp_share_min=Decimal("0.20"),
        comp_share_max=Decimal("0.80"),
        comp_hole_offset=Decimal("0"),
    )


def _adopter(client: _FakeClient) -> SpareAdopter:
    om = OrderManager(
        client=client,  # type: ignore[arg-type]
        instrument=_INSTRUMENT,
        config=_config(),
        bus=NoOpEventBus(),
    )
    return SpareAdopter(om, NoOpEventBus())


@pytestmark_db
async def test_sweep_waits_for_the_spare_to_settle_before_booking() -> None:
    # a fill that has not rested its take-profit yet looks like loose
    # coin, so a single sighting must not book anything
    adopter = _adopter(_FakeClient("300"))
    assert await adopter.sweep(_PRICE) == 0
    assert await adopter.sweep(_PRICE) == 0
    assert await adopter.sweep(_PRICE) == 1
    assert len(await repository.open_positions()) == 1


@pytestmark_db
async def test_sweep_forgets_a_spare_that_goes_away() -> None:
    adopter = _adopter(_FakeClient("400"))
    assert await adopter.sweep(_PRICE) == 0
    await add_rows(_lot("400"))
    # the coin now belongs to a lot: the count starts over
    assert await adopter.sweep(_PRICE) == 0
    assert await adopter.sweep(_PRICE) == 0


@pytestmark_db
async def test_an_adopted_lot_rests_a_sell_above_its_entry() -> None:
    client = _FakeClient("300")
    adopter = _adopter(client)
    plan = await adopter.plan(_PRICE)
    assert await adopter.commit(plan, limit=plan.lots) == 1
    order = client.placed[0]
    assert order["side"] == Side.SELL
    assert order["qty"] == plan.lot_qty
    # the profit is the configured 0.66%, snapped up to a buy rung
    assert (order["price"] - _PRICE) / order["price"] >= Decimal("0.0066")
    position = (await repository.open_positions())[0]
    assert position.adopted is True
    assert position.entry_price == _PRICE
    assert position.tp_order_id == "adopt-1"


@pytestmark_db
async def test_adopted_lots_sit_clear_of_every_grid_level() -> None:
    # a grid level index can reach a few thousand; adopted lots are kept
    # far above so the two can never name the same lot
    client = _FakeClient("800")
    adopter = _adopter(client)
    plan = await adopter.plan(_PRICE)
    assert await adopter.commit(plan, limit=plan.lots) == 4
    levels = sorted(p.level_index for p in await repository.open_positions())
    assert levels == [1_000_000, 1_000_001, 1_000_002, 1_000_003]
    assert await repository.next_adopted_level() == 1_000_004
