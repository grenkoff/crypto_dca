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


def test_select_picks_second_nearest_tp_above_market() -> None:
    positions = [
        _pos(1, "60000", tp="61000"),  # farthest
        _pos(2, "58000", tp="59000"),  # second-nearest — the victim
        _pos(3, "55000", tp="58000"),  # nearest — left to fill on its own
    ]
    victim = select_compensation_target(
        positions, Decimal("57000"), Decimal("0.01")
    )
    assert victim is not None and victim.id == 2


def test_select_falls_back_to_single_candidate() -> None:
    positions = [
        _pos(1, "60000", tp="61000"),
        _pos(2, "50000", tp="56000"),  # below market — not a candidate
    ]
    victim = select_compensation_target(
        positions, Decimal("57000"), Decimal("0.01")
    )
    assert victim is not None and victim.id == 1


def test_select_skips_tp_at_or_below_market() -> None:
    positions = [_pos(1, "50000", tp="56000"), _pos(2, "55000", tp="56500")]
    victim = select_compensation_target(
        positions, Decimal("60000"), Decimal("0.01")
    )
    assert victim is None


def test_select_returns_none_on_empty() -> None:
    assert (
        select_compensation_target([], Decimal("60000"), Decimal("0.01"))
        is None
    )


def test_compensation_lowers_tp_by_exactly_one_step() -> None:
    target = _pos(1, "60000", qty="0.001", fees_in="0.06", tp="61000")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
        step=Decimal("100"),
    )
    assert decision is not None
    assert decision.new_tp_price == Decimal("60900")  # one step down, no more
    assert decision.new_credit == Decimal("0.50")  # profit booked as credit


def test_compensation_steps_accumulate_credit_across_calls() -> None:
    t1 = _pos(1, "60000", qty="0.001", fees_in="0.06", tp="61000", credit="0")
    d1 = compute_compensation(
        target=t1,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
        step=Decimal("100"),
    )
    assert d1 is not None and d1.new_tp_price == Decimal("60900")
    t2 = _pos(
        1,
        "60000",
        qty="0.001",
        fees_in="0.06",
        tp=str(d1.new_tp_price),
        credit="0.50",
    )
    d2 = compute_compensation(
        target=t2,
        profit_from_other=Decimal("0.50"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
        step=Decimal("100"),
    )
    assert d2 is not None
    assert d2.new_tp_price == Decimal("60800")  # another single step
    assert d2.new_credit == Decimal("1.00")


def test_compensation_step_smaller_than_tick_still_moves_one_tick() -> None:
    # step below tick granularity must still produce a real (one-tick)
    # decrease.
    target = _pos(1, "60000", qty="0.001", fees_in="0", tp="61000")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.10"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
        step=Decimal("0.001"),
    )
    assert decision is not None
    assert decision.new_tp_price == Decimal("60999.99")


def test_compute_compensation_caps_at_market_floor() -> None:
    # Low entry (break-even below market) + credit: a full step would land
    # below market, so it is floored one tick above it.
    target = _pos(
        1, "59000", qty="0.001", fees_in="0", tp="59000.05", credit="0.10"
    )
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.05"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("59000"),
        tick_size=Decimal("0.01"),
        step=Decimal("100"),
    )
    assert decision is not None and decision.new_tp_price == Decimal(
        "59000.01"
    )


def test_compute_compensation_skips_when_credit_insufficient() -> None:
    # TP already far below break-even; the tiny credit can't cover the loss ->
    # skip (the credit floor sits above the current TP), so the profit is kept
    # instead.
    target = _pos(
        1, "60000", qty="0.001", fees_in="0", tp="59000.05", credit="0"
    )
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.10"),
        maker_fee=Decimal("0.001"),
        current_price=Decimal("57000"),
        tick_size=Decimal("0.01"),
        step=Decimal("100"),
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
            step=Decimal("100"),
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
        step=Decimal("100"),
    )
    assert decision is not None
    # Nearest TP (59000) is left alone; the second-nearest (61000) steps down.
    assert decision.target_position_id == 1
    assert decision.new_tp_price == Decimal("60900")


def test_compute_compensation_floors_at_min_notional() -> None:
    # 164.91 KAS: a step from 0.0304 to 0.0303 would put the sell at $4.9976 —
    # below the $5 exchange minimum — so the price is floored to stay
    # placeable.
    target = _pos(1, "0.0302", qty="164.91", fees_in="0", tp="0.0304")
    decision = compute_compensation(
        target=target,
        profit_from_other=Decimal("0.05"),
        maker_fee=Decimal("0.000625"),
        current_price=Decimal("0.0295"),
        tick_size=Decimal("0.00001"),
        step=Decimal("0.0001"),
        min_order_amt=Decimal("5"),
    )
    assert decision is not None
    assert decision.new_tp_price * target.qty >= Decimal("5")
    assert decision.new_tp_price < target.current_tp_price
