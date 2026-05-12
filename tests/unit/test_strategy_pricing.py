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


def test_grid_step_target_when_profitable_enough() -> None:
    # Percent step that easily covers min_profit
    tp = compute_tp_price(
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),  # 0.001 * 60000 * 0.001
        mode="percent",
        step=Decimal("0.01"),  # 1% — gives ~$0.60 gross
        min_profit_quote=Decimal("0.05"),
        maker_fee=Decimal("0.001"),
        tick_size=Decimal("0.01"),
    )
    # Target = 60600; min ~ 60180 — target wins
    assert tp == Decimal("60600")
    _verify_pnl_at_least(
        tp=tp,
        entry=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        maker_fee=Decimal("0.001"),
        min_profit=Decimal("0.05"),
    )


def test_min_profit_overrides_when_step_too_small() -> None:
    # Tiny step — fees would eat the profit, min_profit forces a higher TP
    tp = compute_tp_price(
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        mode="absolute",
        step=Decimal("1"),  # only $1 gross spread
        min_profit_quote=Decimal("1"),
        maker_fee=Decimal("0.001"),
        tick_size=Decimal("0.01"),
    )
    # Target = 60001; min must give net pnl ≥ $1 → tp ≈ (1 + 60.06)/(0.001*0.999) ≈ 61121
    assert tp > Decimal("61000")
    _verify_pnl_at_least(
        tp=tp,
        entry=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0.06"),
        maker_fee=Decimal("0.001"),
        min_profit=Decimal("1"),
    )


def test_tp_rounds_up_to_tick() -> None:
    tp = compute_tp_price(
        entry_price=Decimal("60000"),
        qty=Decimal("0.001"),
        fees_in=Decimal("0"),
        mode="absolute",
        step=Decimal("12.345"),
        min_profit_quote=Decimal("0"),
        maker_fee=Decimal("0"),
        tick_size=Decimal("0.10"),
    )
    # Raw target = 60012.345 → ceil to 0.10 = 60012.40
    assert tp == Decimal("60012.40")
