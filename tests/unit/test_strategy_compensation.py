from __future__ import annotations

from decimal import Decimal

from core.strategy.compensation import (
    compute_compensation,
    plan_compensation,
    select_compensation_target,
)
from core.strategy.types import OpenPosition


def _pos(
    pid: int,
    entry: str,
    qty: str = "0.001",
    fees_in: str = "0",
    tp: str = "0",
    credit: str = "0",
) -> OpenPosition:
    return OpenPosition(
        id=pid,
        entry_price=Decimal(entry),
        qty=Decimal(qty),
        fees_in=Decimal(fees_in),
        current_tp_price=Decimal(tp),
        compensation_credit=Decimal(credit),
    )


def test_select_picks_highest_tp() -> None:
    positions = [
        _pos(1, "60000", tp="61000"),  # tail — highest TP, hardest to fill
        _pos(2, "58000", tp="59000"),
        _pos(3, "55000", tp="56000"),
    ]
    victim = select_compensation_target(positions, Decimal("57000"), Decimal("0.01"))
    assert victim is not None and victim.id == 1


def test_select_is_sticky_on_in_progress() -> None:
    # id=2 has a lower TP but is already in progress (credit>0) -> keep working it.
    positions = [
        _pos(1, "60000", tp="61000", credit="0"),
        _pos(2, "58000", tp="59000", credit="0.50"),
    ]
    victim = select_compensation_target(positions, Decimal("57000"), Decimal("0.01"))
    assert victim is not None and victim.id == 2


def test_select_skips_tp_at_or_below_market() -> None:
    positions = [_pos(1, "50000", tp="56000"), _pos(2, "55000", tp="56500")]
    victim = select_compensation_target(positions, Decimal("60000"), Decimal("0.01"))
    assert victim is None


def test_select_returns_none_on_empty() -> None:
    assert select_compensation_target([], Decimal("60000"), Decimal("0.01")) is None


def test_compensation_accumulates_credit() -> None:
    # Same position compensated twice: second TP is lower than the first.
    t1 = _pos(1, "60000", qty="0.001", fees_in="0.06", tp="61000", credit="0")
    d1 = compute_compensation(
        target=t1,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
    )
    assert d1 is not None and d1.new_credit == Decimal("0.50")
    # Feed the accumulated credit back in and apply another profit.
    t2 = _pos(1, "60000", qty="0.001", fees_in="0.06", tp=str(d1.new_tp_price), credit="0.50")
    d2 = compute_compensation(
        target=t2,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
    )
    assert d2 is not None
    assert d2.new_credit == Decimal("1.00")
    assert d2.new_tp_price < d1.new_tp_price  # walked further down toward market


def test_compensation_caps_one_tick_above_market() -> None:
    # Huge accumulated credit would drive TP below market -> capped just above it.
    target = _pos(1, "60000", qty="0.001", fees_in="0.06", tp="61000", credit="100")
    d = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
    )
    assert d is not None and d.new_tp_price == Decimal("57000.01")


def test_compute_compensation_drops_tp_to_breakeven() -> None:
    # Position bought 0.001 BTC at 60000, no entry fees, current TP at 61000
    target = _pos(1, "60000", qty="0.001", fees_in="0.06", tp="61000")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.50"),  # X = $0.50
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
    )
    assert decision is not None
    assert decision.target_position_id == 1
    # raw = (60000 * 0.001 + 0.06 - 0.5) / (0.001 * 0.999)
    #     = (60 + 0.06 - 0.5) / 0.000999 = 59.56 / 0.000999 ≈ 59619.62
    # ceil to 0.01 = 59619.62
    assert decision.new_tp_price == Decimal("59619.62")
    assert decision.new_tp_price < target.current_tp_price
    assert decision.new_tp_price > Decimal("57000")


def test_compute_compensation_caps_at_market_floor() -> None:
    target = _pos(1, "60000", qty="0.001", fees_in="0", tp="61000")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("100"),  # huge X — would push tp far below market
        maker_fee=Decimal("0.001"),
        current_price=Decimal("59000"),
        tick_size=Decimal("0.01"),
    )
    # No longer skipped: TP is capped one tick above market so the order rests and fills.
    assert decision is not None and decision.new_tp_price == Decimal("59000.01")


def test_compute_compensation_skips_when_no_improvement() -> None:
    # Existing TP is already lower than what compensation could justify
    target = _pos(1, "60000", qty="0.001", fees_in="0.06", tp="59500")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.10"),  # tiny X
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
    )
    assert decision is None


def test_compute_compensation_skips_when_profit_nonpositive() -> None:
    target = _pos(1, "60000", qty="0.001", fees_in="0", tp="61000")
    assert (
        compute_compensation(
            target=target,
            profit_from_other=Decimal("0"),
            maker_fee=Decimal("0.001"),
            current_price=Decimal("57000"),
            tick_size=Decimal("0.01"),
        )
        is None
    )


def test_plan_compensation_end_to_end() -> None:
    positions = [
        _pos(1, "60000", qty="0.001", fees_in="0.06", tp="61000"),
        _pos(2, "58000", qty="0.001", fees_in="0.058", tp="59000"),
    ]
    decision = plan_compensation(
        open_positions=positions,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
    )
    assert decision is not None
    # Position 1 is more underwater, so it should be selected
    assert decision.target_position_id == 1
