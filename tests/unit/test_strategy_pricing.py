from __future__ import annotations

from decimal import Decimal

from core.strategy.pricing import compute_tp_price


def _verify_pnl_at_least(
    *,
    tp: Decimal,
    entry: Decimal,
    qty: Decimal,
    fees_in: Decimal,
    maker_fee: Decimal,
    min_profit: Decimal,
) -> Decimal:
    pnl = tp * qty * (Decimal(1) - maker_fee) - entry * qty - fees_in
    assert pnl >= min_profit, f"pnl={pnl} below min_profit={min_profit}"
    return pnl


def test_absolute_tp_step_used_when_above_floor() -> None:
    # tp_step comfortably clears the min-profit floor -> target wins
    tp = compute_tp_price(
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        tp_step=Decimal("600"),  # +$600 absolute
        min_profit_quote=Decimal("0.05"),
        maker_fee=Decimal("0.001"),
        tick_size=Decimal("0.01"),
    )
    assert tp == Decimal("60600")
    _verify_pnl_at_least(
        tp=tp,
        entry=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        maker_fee=Decimal("0.001"),
        min_profit=Decimal("0.05"),
    )


def test_min_profit_overrides_when_tp_step_too_small() -> None:
    # tp_step so small the fees would eat it -> floor forces a higher TP
    tp = compute_tp_price(
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        tp_step=Decimal("1"),  # only +$1 absolute
        min_profit_quote=Decimal("1"),
        maker_fee=Decimal("0.001"),
        tick_size=Decimal("0.01"),
    )
    assert tp > Decimal("61000")
    _verify_pnl_at_least(
        tp=tp,
        entry=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        maker_fee=Decimal("0.001"),
        min_profit=Decimal("1"),
    )


def test_breakeven_floor_when_min_profit_zero() -> None:
    # min_profit=0 -> floor is break-even; a below-break-even tp_step is lifted to it
    entry = Decimal("0.03100")
    qty = Decimal("193.54")
    fees_in = entry * qty * Decimal("0.000625")
    tp = compute_tp_price(
        entry_price=entry,
        qty=qty,
        fees_in=fees_in,
        tp_step=Decimal("0.00001"),  # 1 tick — below break-even (~4 ticks)
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0.000625"),
        tick_size=Decimal("0.00001"),
    )
    # Never below break-even, so pnl >= 0
    _verify_pnl_at_least(
        tp=tp,
        entry=entry,
        qty=qty,
        fees_in=fees_in,
        maker_fee=Decimal("0.000625"),
        min_profit=Decimal("0"),
    )


def test_min_notional_floor_lifts_tp_for_small_position() -> None:
    # 164.91 KAS: entry+tp_step = 0.0303 -> notional $4.997 < $5, so TP is lifted
    # until the sell clears the exchange minimum.
    entry = Decimal("0.0302")
    qty = Decimal("164.91")
    tp = compute_tp_price(
        entry_price=entry,
        qty=qty,
        fees_in=Decimal("0"),
        tp_step=Decimal("0.0001"),
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0.000625"),
        tick_size=Decimal("0.00001"),
        min_order_amt=Decimal("5"),
    )
    assert tp * qty >= Decimal("5")
    assert tp >= Decimal("0.03032")  # above the naive 0.0303


def test_no_min_notional_floor_by_default() -> None:
    # default min_order_amt=0 -> unchanged (naive entry+tp_step)
    tp = compute_tp_price(
        entry_price=Decimal("0.0302"),
        qty=Decimal("164.91"),
        fees_in=Decimal("0"),
        tp_step=Decimal("0.0001"),
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0.000625"),
        tick_size=Decimal("0.00001"),
    )
    assert tp == Decimal("0.03030")


def test_tp_rounds_up_to_tick() -> None:
    tp = compute_tp_price(
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0"),
        tp_step=Decimal("12.345"),
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0"),
        tick_size=Decimal("0.10"),
    )
    # Raw target = 60012.345 → ceil to 0.10 = 60012.40
    assert tp == Decimal("60012.40")
