"""Property-based tests for the pure Decimal money math (hypothesis).

These assert invariants that must hold for *any* input — the places where a
rounding or boundary bug would silently cost money — rather than a handful of
hand-picked examples.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from core.exchange.types import Instrument
from core.services.order_manager import compute_buy_qty
from core.strategy.compensation import plan_compensation
from core.strategy.grid import (
    buys_to_prune,
    fundable_targets,
    resting_buy_levels,
)
from core.strategy.pricing import compute_tp_price
from core.strategy.rounding import (
    min_notional_price,
    next_tick_above,
    round_down_to_tick,
    round_up_to_tick,
)
from core.strategy.types import CompensationContext, OpenPosition

# Real exchange ticks/lots are powers of ten; using those keeps the grid
# arithmetic exact and the invariants free of Decimal-precision noise.
TICKS = st.sampled_from([Decimal(10) ** -k for k in range(9)])


def _dec(lo: str, hi: str, places: int = 8) -> st.SearchStrategy[Decimal]:
    return st.decimals(
        min_value=Decimal(lo),
        max_value=Decimal(hi),
        allow_nan=False,
        allow_infinity=False,
        places=places,
    )


def _on_grid(value: Decimal, tick: Decimal) -> bool:
    return value == (value / tick).to_integral_value() * tick


@given(price=_dec("0", "1000000"), tick=TICKS)
def test_round_down_stays_at_or_below_within_one_tick(
    price: Decimal, tick: Decimal
) -> None:
    r = round_down_to_tick(price, tick)
    assert 0 <= r <= price
    assert price - r < tick
    assert _on_grid(r, tick)


@given(price=_dec("0", "1000000"), tick=TICKS)
def test_round_up_stays_at_or_above_within_one_tick(
    price: Decimal, tick: Decimal
) -> None:
    r = round_up_to_tick(price, tick)
    assert r >= price
    assert r - price < tick
    assert _on_grid(r, tick)


@given(price=_dec("0", "1000000"), tick=TICKS)
def test_next_tick_above_is_strictly_above_and_on_grid(
    price: Decimal, tick: Decimal
) -> None:
    r = next_tick_above(price, tick)
    assert r > price
    assert _on_grid(r, tick)


@given(
    min_amt=_dec("0", "10000", places=2),
    qty=_dec("0.001", "1000000"),
    tick=TICKS,
)
def test_min_notional_price_clears_the_minimum(
    min_amt: Decimal, qty: Decimal, tick: Decimal
) -> None:
    r = min_notional_price(min_amt, qty, tick)
    assert _on_grid(r, tick)
    # rounded UP from min_amt/qty, so it never sits below the requirement
    assert r >= min_amt / qty


@given(
    entry_price=_dec("0.00001", "100000"),
    qty=_dec("0.001", "100000"),
    fees_in=_dec("0", "1000", places=2),
    tp_step=_dec("0", "10000"),
    min_profit=_dec("0", "1000", places=2),
    maker_fee=st.sampled_from(
        [Decimal(f) for f in ("0", "0.0001", "0.001", "0.01", "0.1")]
    ),
    tick=TICKS,
    min_order_amt=_dec("0", "100", places=2),
)
def test_tp_price_never_below_entry_and_on_grid(
    entry_price: Decimal,
    qty: Decimal,
    fees_in: Decimal,
    tp_step: Decimal,
    min_profit: Decimal,
    maker_fee: Decimal,
    tick: Decimal,
    min_order_amt: Decimal,
) -> None:
    tp = compute_tp_price(
        entry_price=entry_price,
        qty=qty,
        fees_in=fees_in,
        tp_step=tp_step,
        min_profit_quote=min_profit,
        maker_fee=maker_fee,
        tick_size=tick,
        min_order_amt=min_order_amt,
    )
    assert _on_grid(tp, tick)
    # the position is never sold below its entry cost
    assert tp >= entry_price


@given(
    price=_dec("0.00001", "1000000"),
    tick=TICKS,
    count=st.integers(min_value=1, max_value=20),
    held=st.sets(_dec("0", "1000000"), max_size=10),
)
def test_resting_buy_levels_are_below_market_descending_and_unheld(
    price: Decimal, tick: Decimal, count: int, held: set[Decimal]
) -> None:
    levels = resting_buy_levels(price, tick, count, held)
    prices = [p for _, p in levels]
    assert len(levels) <= count
    assert all(p < price for p in prices)
    assert all(p > 0 for p in prices)
    assert all(_on_grid(p, tick) for p in prices)
    assert all(p not in held for p in prices)
    assert prices == sorted(prices, reverse=True)
    assert len(set(prices)) == len(prices)


@given(
    resting=st.lists(_dec("0", "1000000"), max_size=30),
    targets=st.sets(_dec("0.00001", "1000000"), min_size=1, max_size=20),
)
def test_buys_to_prune_are_exactly_those_below_the_band_bottom(
    resting: list[Decimal], targets: set[Decimal]
) -> None:
    bottom = min(targets)
    prune = buys_to_prune(resting, targets)
    assert all(p in resting for p in prune)
    assert all(p < bottom for p in prune)
    # completeness: nothing at/above the bottom is pruned
    assert all(p >= bottom for p in resting if p not in prune)


@given(
    targets=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10000),
            _dec("0.00001", "1000000"),
        ),
        max_size=30,
    ),
    covered=st.sets(_dec("0", "1000000"), max_size=10),
    budget=_dec("0", "100000", places=2),
    per_order=_dec("0.01", "1000", places=2),
)
def test_fundable_targets_never_exceeds_budget(
    targets: list[tuple[int, Decimal]],
    covered: set[Decimal],
    budget: Decimal,
    per_order: Decimal,
) -> None:
    chosen = fundable_targets(targets, covered, budget, per_order)
    # never plans to spend more free capital than it has
    assert len(chosen) * per_order <= budget
    # only ever picks real, uncovered targets
    assert all((k, p) in targets for k, p in chosen)
    assert all(p not in covered for _, p in chosen)


@given(
    quote=_dec("0.01", "100000", places=2),
    price=_dec("0.00001", "100000"),
    lot=TICKS,
    min_order_amt=_dec("0", "100", places=2),
)
def test_compute_buy_qty_lot_aligned_and_clears_min_when_funded(
    quote: Decimal, price: Decimal, lot: Decimal, min_order_amt: Decimal
) -> None:
    instrument = Instrument(
        symbol="XUSDT",
        base_coin="X",
        quote_coin="USDT",
        tick_size=lot,
        lot_size=lot,
        min_order_qty=lot,
        min_order_amt=min_order_amt,
    )
    qty = compute_buy_qty(quote, price, instrument)
    assert qty >= 0
    assert _on_grid(qty, lot)
    # the sub-min boundary is only permitted when the *request* is sub-min;
    # a properly funded request must always clear the exchange minimum
    if quote >= min_order_amt:
        assert qty * price >= min_order_amt


_COMP_GRID = Decimal("0.00005")
_COMP_TP_STEP = Decimal("0.0001")
_COMP_TICK = Decimal("0.00001")


@st.composite
def _open_positions(draw: st.DrawFn) -> list[OpenPosition]:
    ks = draw(
        st.lists(
            st.integers(min_value=400, max_value=700),
            min_size=1,
            max_size=8,
            unique=True,
        )
    )
    out: list[OpenPosition] = []
    for i, k in enumerate(ks):
        out.append(
            OpenPosition(
                id=i + 1,
                entry_price=draw(_dec("0.01", "0.05", 5)),
                qty=draw(_dec("50", "500", 2)),
                fees_in=Decimal(0),
                current_tp_price=Decimal(k) * _COMP_GRID,
                compensation_credit=draw(_dec("0", "5", 4)),
            )
        )
    return out


@given(
    positions=_open_positions(),
    pool=_dec("0", "10000", 2),
    market=_dec("0.015", "0.04", 5),
    nearest_buy=_dec("0", "0.04", 5),
    maker_fee=st.sampled_from(
        [Decimal(f) for f in ("0", "0.000625", "0.001", "0.01")]
    ),
)
def test_compensation_decision_invariants(
    positions: list[OpenPosition],
    pool: Decimal,
    market: Decimal,
    nearest_buy: Decimal,
    maker_fee: Decimal,
) -> None:
    ctx = CompensationContext(
        pool=pool,
        maker_fee=maker_fee,
        current_price=market,
        tick_size=_COMP_TICK,
        grid_step=_COMP_GRID,
        tp_step=_COMP_TP_STEP,
        nearest_buy_price=nearest_buy,
        min_order_amt=Decimal(0),
    )
    decision = plan_compensation(positions, ctx)
    if decision is None:
        return
    victim = next(p for p in positions if p.id == decision.target_position_id)
    # the new TP stays on the grid, strictly below the old one
    assert _on_grid(decision.new_tp_price, _COMP_GRID)
    assert decision.new_tp_price < victim.current_tp_price
    # credit is drawn only from the pool, and tracked on the position
    assert Decimal(0) <= decision.credit_drawn <= pool
    assert (
        decision.new_credit
        == victim.compensation_credit + decision.credit_drawn
    )
    # the compensated pair is strictly in profit
    realized = (
        decision.new_tp_price * victim.qty * (Decimal(1) - maker_fee)
        - victim.entry_price * victim.qty
        - victim.fees_in
    )
    assert realized + decision.new_credit > 0
    # never below the wall floor (market / nearest_buy + tp_step)
    floor = next_tick_above(market, _COMP_TICK)
    if nearest_buy > 0:
        floor = max(floor, nearest_buy + _COMP_TP_STEP)
    assert decision.new_tp_price >= floor
