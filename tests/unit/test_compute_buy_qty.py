from __future__ import annotations

from decimal import Decimal

from core.exchange.types import Instrument
from core.services.order_manager import compute_buy_qty


def _instrument(lot: str = "0.01", min_amt: str = "5") -> Instrument:
    return Instrument(
        symbol="KASUSDT",
        base_coin="KAS",
        quote_coin="USDT",
        tick_size=Decimal("0.00001"),
        lot_size=Decimal(lot),
        min_order_qty=Decimal("0.01"),
        min_order_amt=Decimal(min_amt),
    )


def test_boundary_bumps_one_lot_to_clear_minimum() -> None:
    inst = _instrument()
    # $5 / 0.0287 = 174.216 -> floor 174.21 -> $4.9998 (below min) -> bump to 174.22
    qty = compute_buy_qty(Decimal("5"), Decimal("0.0287"), inst)
    assert qty == Decimal("174.22")
    assert qty * Decimal("0.0287") >= inst.min_order_amt


def test_no_bump_when_already_above_minimum() -> None:
    inst = _instrument()
    qty = compute_buy_qty(Decimal("6"), Decimal("0.03"), inst)
    assert qty == Decimal("200")  # 6 / 0.03 exactly, well above min


def test_exact_five_dollar_order_is_valid() -> None:
    inst = _instrument()
    qty = compute_buy_qty(Decimal("5"), Decimal("0.03"), inst)
    assert qty * Decimal("0.03") >= inst.min_order_amt
