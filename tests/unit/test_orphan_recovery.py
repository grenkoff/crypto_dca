from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from core.db.models import Position, PositionStatus
from core.exchange.bybit import BybitClient
from core.exchange.types import Order, OrderStatus, Side
from core.services import repository
from core.services.order_common import level_from_link
from core.services.reconciliation import reconcile_once
from tests.conftest import add_rows

pytestmark = pytest.mark.db


class _FakeClient:
    """Serves a fixed order book and records cancellations."""

    def __init__(self, orders: list[Order]) -> None:
        self.orders = orders
        self.cancelled: list[str] = []

    async def get_open_orders(self, symbol: str) -> list[Order]:
        return self.orders

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.cancelled.append(order_id)


def _order(order_id: str, link: str, *, age_seconds: int = 600) -> Order:
    stamp = datetime.now(tz=UTC) - timedelta(seconds=age_seconds)
    return Order(
        order_id=order_id,
        symbol="KASUSDT",
        side=Side.SELL,
        price=Decimal("0.0304"),
        qty=Decimal("165"),
        status=OrderStatus.NEW,
        created_at=stamp,
        updated_at=stamp,
        order_link_id=link,
    )


async def _lot(level: int, tp_order_id: str) -> Position:
    added = await add_rows(
        Position(
            level_index=level,
            entry_price=Decimal("0.03"),
            qty=Decimal("165"),
            tp_order_id=tp_order_id,
            tp_price=Decimal("0.0304"),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(tz=UTC),
        )
    )
    return added[0]


def _run(client: _FakeClient) -> Any:
    return reconcile_once(cast(BybitClient, cast(Any, client)), "KASUSDT")


def test_level_is_read_from_our_link_ids() -> None:
    assert level_from_link("grid-tp-comp-606-1787581174946") == 606
    assert level_from_link("grid-buy-562-1787249089366") == 562
    assert level_from_link("manual-order") is None
    assert level_from_link("") is None


async def test_a_stray_order_is_handed_back_to_its_unprotected_lot() -> None:
    lot = await _lot(606, "")
    client = _FakeClient([_order("stray-1", "grid-tp-606-1")])
    await _run(client)
    assert client.cancelled == []
    restored = await repository.get_position(lot.id)
    assert restored.tp_order_id == "stray-1"


async def test_a_duplicate_is_cancelled_when_the_lot_is_covered() -> None:
    await _lot(606, "live-tp")
    client = _FakeClient(
        [
            _order("live-tp", "grid-tp-606-1"),
            _order("stray-2", "grid-tp-comp-606-2"),
        ]
    )
    await _run(client)
    assert client.cancelled == ["stray-2"]


async def test_a_freshly_placed_order_is_left_alone() -> None:
    await _lot(606, "live-tp")
    client = _FakeClient(
        [
            _order("live-tp", "grid-tp-606-1"),
            _order("stray-3", "grid-tp-606-2", age_seconds=5),
        ]
    )
    await _run(client)
    assert client.cancelled == []


async def test_an_order_we_did_not_place_is_never_touched() -> None:
    client = _FakeClient([_order("manual-1", "someone-elses-order")])
    await _run(client)
    assert client.cancelled == []
