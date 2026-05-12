from __future__ import annotations

from decimal import Decimal

from core.strategy.compensation import (
    compute_compensation,
    plan_compensation,
    select_most_underwater,
)
from core.strategy.types import OpenPosition


def _pos(
    pid: int,
    entry: str,
    qty: str = "0.001",
    fees_in: str = "0",
    tp: str = "0",
) -> OpenPosition:
    return OpenPosition(
        id=pid,
        entry_price=Decimal(entry),
        qty=Decimal(qty),
        fees_in=Decimal(fees_in),
        current_tp_price=Decimal(tp),
    )


def test_select_picks_largest_loss() -> None:
    positions = [
        _pos(1, "60000"),  # underwater by ~$3.06 at 57k
        _pos(2, "58000"),  # underwater by ~$1.06
        _pos(3, "55000"),  # in profit at 57k
    ]
    victim = select_most_underwater(positions, Decimal("57000"), Decimal("0.001"))
    assert victim is not None and victim.id == 1


def test_select_returns_none_when_all_profitable() -> None:
    positions = [_pos(1, "50000"), _pos(2, "55000")]
    victim = select_most_underwater(positions, Decimal("60000"), Decimal("0.001"))
    assert victim is None


def test_select_returns_none_on_empty() -> None:
    assert select_most_underwater([], Decimal("60000"), Decimal("0.001")) is None


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


def test_compute_compensation_skips_when_below_market() -> None:
    target = _pos(1, "60000", qty="0.001", fees_in="0", tp="61000")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("100"),  # huge X — would push tp far below market
        maker_fee=Decimal("0.001"),
        current_price=Decimal("59000"),
        tick_size=Decimal("0.01"),
    )
    assert decision is None


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
