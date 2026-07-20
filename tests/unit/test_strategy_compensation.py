from __future__ import annotations

from decimal import Decimal

from core.strategy.compensation import plan_compensation, slot_below
from core.strategy.types import CompensationContext, OpenPosition


def _pos(
    pid: int,
    tp: str,
    entry: str = "0.02000",
    qty: str = "200",
    fees_in: str = "0",
    credit: str = "0",
    filled: str = "0",
) -> OpenPosition:
    return OpenPosition(
        id=pid,
        entry_price=Decimal(entry),
        qty=Decimal(qty),
        fees_in=Decimal(fees_in),
        current_tp_price=Decimal(tp),
        compensation_credit=Decimal(credit),
        filled_qty=Decimal(filled),
    )


def _ctx(
    *,
    pool: str = "1000",
    market: str = "0.02768",
    nearest_buy: str = "0.02785",
    grid_step: str = "0.00005",
    tp_step: str = "0.0001",
    tick: str = "0.00001",
    maker_fee: str = "0.000625",
    min_order_amt: str = "0",
) -> CompensationContext:
    return CompensationContext(
        pool=Decimal(pool),
        maker_fee=Decimal(maker_fee),
        current_price=Decimal(market),
        tick_size=Decimal(tick),
        grid_step=Decimal(grid_step),
        tp_step=Decimal(tp_step),
        nearest_buy_price=Decimal(nearest_buy),
        min_order_amt=Decimal(min_order_amt),
    )


def test_slot_below_on_grid_steps_one_grid_step() -> None:
    assert slot_below(Decimal("0.02810"), Decimal("0.00005")) == Decimal(
        "0.02805"
    )


def test_slot_below_off_grid_snaps_to_lattice() -> None:
    # 0.02836 is off the 0.00005 lattice -> pulled down to 0.02835
    assert slot_below(Decimal("0.02836"), Decimal("0.00005")) == Decimal(
        "0.02835"
    )


def test_fills_nearest_hole_above_the_wall() -> None:
    # wall_floor = 0.02785 + 0.0001 = 0.02795; contiguous 02795/02800, gap
    # 02805, then 02810. The bottom two can't move (at floor / slot occupied);
    # 02810 drops into the hole 02805.
    positions = [
        _pos(3, "0.02795"),
        _pos(2, "0.02800"),
        _pos(1, "0.02810"),
    ]
    decision = plan_compensation(positions, _ctx())
    assert decision is not None
    assert decision.target_position_id == 1
    assert decision.new_tp_price == Decimal("0.02805")
    assert decision.credit_drawn == Decimal("0")  # winner moves for free


def test_isolated_tp_steps_down_one_grid_step() -> None:
    # contiguous 02795..02810, isolated 02830 -> it steps to 02825
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02805"),
        _pos(4, "0.02810"),
        _pos(5, "0.02830"),
    ]
    decision = plan_compensation(positions, _ctx())
    assert decision is not None
    assert decision.target_position_id == 5
    assert decision.new_tp_price == Decimal("0.02825")


def test_off_grid_tp_is_pulled_onto_the_lattice() -> None:
    positions = [_pos(1, "0.02836")]
    decision = plan_compensation(positions, _ctx(nearest_buy="0"))
    assert decision is not None
    assert decision.new_tp_price == Decimal("0.02835")


def test_bottom_tp_not_pushed_below_wall_floor() -> None:
    # wall_floor = nearest_buy 0.02760 + tp_step 0.0001 + grid_step 0.00005
    #            = 0.02775; a TP already there cannot move lower
    positions = [_pos(1, "0.02775")]
    assert plan_compensation(positions, _ctx(nearest_buy="0.02760")) is None


def test_tp_one_step_above_floor_moves_down_to_the_floor() -> None:
    # floor 0.02775; a TP one grid_step above it settles onto it
    positions = [_pos(1, "0.02780")]
    decision = plan_compensation(positions, _ctx(nearest_buy="0.02760"))
    assert decision is not None
    assert decision.new_tp_price == Decimal("0.02775")


def test_underwater_move_draws_credit_and_keeps_pair_positive() -> None:
    victim = _pos(1, "0.02810", entry="0.03000", qty="200")
    decision = plan_compensation([victim], _ctx(nearest_buy="0"))
    assert decision is not None
    assert decision.new_tp_price == Decimal("0.02805")
    assert decision.credit_drawn > 0
    realized = (
        decision.new_tp_price * victim.qty * (Decimal(1) - Decimal("0.000625"))
        - victim.entry_price * victim.qty
        - victim.fees_in
    )
    assert realized + decision.new_credit > 0  # pair strictly in profit


def test_underwater_move_skipped_when_pool_too_small() -> None:
    victim = _pos(1, "0.02810", entry="0.03000", qty="200")
    assert (
        plan_compensation([victim], _ctx(pool="0.10", nearest_buy="0")) is None
    )


def test_occupied_slot_below_is_skipped_for_next_victim() -> None:
    # 02800's slot (02795) is occupied; the mover is 02810 into empty 02805
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02810"),
    ]
    decision = plan_compensation(positions, _ctx())
    assert decision is not None and decision.target_position_id == 3


def test_no_move_when_pool_nonpositive_or_empty() -> None:
    assert plan_compensation([_pos(1, "0.02810")], _ctx(pool="0")) is None
    assert plan_compensation([], _ctx()) is None


def test_partially_filled_victim_is_skipped() -> None:
    # 02810 would move into the empty 02805 slot, but it is mid-fill: a
    # replacement sell on its full qty would oversell, so it is never chosen.
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02810", filled="35"),
    ]
    assert plan_compensation(positions, _ctx()) is None


def test_partial_fill_still_occupies_its_slot() -> None:
    # 02810 is partially filled (never a victim), but its TP still blocks
    # 02815 from descending onto the occupied 02810 slot.
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02810", filled="35"),
        _pos(4, "0.02815"),
    ]
    assert plan_compensation(positions, _ctx()) is None
