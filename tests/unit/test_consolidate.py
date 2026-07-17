from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.services.consolidate import PosRow, plan_consolidation


def _row(
    pid: int,
    entry: str,
    *,
    qty: str = "169.78",
    filled: str = "0",
    tp: str = "",
    day: int = 1,
    level: int = 500,
) -> PosRow:
    return PosRow(
        id=pid,
        level_index=level,
        entry=Decimal(entry),
        qty=Decimal(qty),
        filled_qty=Decimal(filled),
        fees_in=Decimal("0"),
        tp_order_id=tp or f"tp-{pid}",
        opened_at=datetime(2026, 7, day, tzinfo=UTC),
    )


_COMMON = dict(
    step=Decimal("0.00005"),
    tp_step=Decimal("0.0001"),
    min_profit_quote=Decimal("0"),
    maker_fee=Decimal("0.000625"),
    tick_size=Decimal("0.00001"),
    min_order_amt=Decimal("5"),
    market_price=Decimal("0.0287"),
)


def test_merges_two_positions_at_same_price() -> None:
    plan = plan_consolidation(
        positions=[_row(342, "0.02945", day=2), _row(341, "0.02945", day=1)],
        **_COMMON,
    )
    assert len(plan) == 1
    g = plan[0]
    assert g.price_key == Decimal("0.02945")
    assert g.survivor_id == 341  # oldest survives
    assert g.absorbed_ids == [342]
    assert g.combined_qty == Decimal("339.56")
    assert g.weighted_entry == Decimal("0.02945")
    assert g.new_tp_price == Decimal(
        "0.02955"
    )  # entry + tp_step, above market
    assert set(g.cancel_order_ids) == {"tp-341", "tp-342"}


def test_single_position_is_not_a_group() -> None:
    assert plan_consolidation(positions=[_row(1, "0.02945")], **_COMMON) == []


def test_excludes_partially_filled_positions() -> None:
    # a position with a partial TP fill is entangled with an in-flight sell ->
    # skip it, leaving only one clean lot at the price, so no group forms
    plan = plan_consolidation(
        positions=[_row(1, "0.02945"), _row(2, "0.02945", filled="80")],
        **_COMMON,
    )
    assert plan == []


def test_weighted_entry_across_prices_rounding_to_same_level() -> None:
    # 0.02944 and 0.02946 both round to the 0.02945 step -> one group, weighted
    # entry
    plan = plan_consolidation(
        positions=[
            _row(1, "0.02944", qty="100"),
            _row(2, "0.02946", qty="100"),
        ],
        **_COMMON,
    )
    assert len(plan) == 1
    g = plan[0]
    assert g.price_key == Decimal("0.02945")
    assert g.combined_qty == Decimal("200")
    assert g.weighted_entry == Decimal("0.02945")


def test_manual_bag_is_never_consolidated() -> None:
    # the manual bag (level 1000..1999) intentionally stacks many lots at one
    # entry with laddered TPs — it must be left untouched even though it shares
    # a price
    bag = [
        _row(i, "0.052", level=1000 + i, tp=f"ladder-{i}") for i in range(5)
    ]
    assert plan_consolidation(positions=bag, **_COMMON) == []


def test_readopt_dupes_merge_but_bag_excluded_in_mixed_input() -> None:
    positions = [
        _row(1, "0.052", level=1000),  # bag lot — excluded
        _row(2, "0.052", level=1001),  # bag lot — excluded
        _row(3, "0.02945", level=3020, day=1),  # readopt dup
        _row(4, "0.02945", level=3021, day=2),  # readopt dup
    ]
    plan = plan_consolidation(positions=positions, **_COMMON)
    assert len(plan) == 1
    assert plan[0].price_key == Decimal("0.02945")
    assert plan[0].survivor_id == 3


def test_tp_floored_at_market_when_recomputed_tp_below_price() -> None:
    # deep underwater lot: entry far below market -> merged sell must not
    # cross, floored one tick above the market price
    common = {**_COMMON, "market_price": Decimal("0.0400")}
    plan = plan_consolidation(
        positions=[_row(1, "0.0290", day=1), _row(2, "0.0290", day=2)],
        **common,
    )
    assert plan[0].new_tp_price == Decimal("0.04001")
