from __future__ import annotations

from decimal import Decimal

from core.strategy.compensation import (
    account_load,
    compensation_share,
    plan_hole_fill,
    plan_market_exit,
    slot_below,
    split_profit,
)
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
    taker_fee: str = "0.00075",
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
        taker_fee=Decimal(taker_fee),
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
    decision = plan_hole_fill(positions, _ctx())
    assert decision is not None
    assert decision.target_position_id == 1
    assert decision.new_tp_price == Decimal("0.02805")
    assert decision.credit_drawn == Decimal("0")  # winner moves for free


def test_a_move_stops_at_the_slot_above_a_taken_one() -> None:
    # contiguous 02795..02810, isolated 02830 -> it slides down onto 02815,
    # the first free slot above the wall, rather than stepping once
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02805"),
        _pos(4, "0.02810"),
        _pos(5, "0.02830"),
    ]
    decision = plan_hole_fill(positions, _ctx())
    assert decision is not None
    assert decision.target_position_id == 5
    assert decision.new_tp_price == Decimal("0.02815")


def test_off_grid_tp_is_pulled_onto_the_lattice() -> None:
    positions = [_pos(1, "0.02836")]
    assert plan_hole_fill(positions, _ctx(nearest_buy="0", pool="0")) is None
    decision = plan_hole_fill(
        positions, _ctx(nearest_buy="0", pool="0.000001")
    )
    assert decision is not None
    assert decision.new_tp_price % Decimal("0.00005") == 0
    assert decision.new_tp_price < Decimal("0.02836")


def test_one_deep_move_costs_the_same_as_walking_there() -> None:
    lot = _pos(1, "0.02900", entry="0.03000", qty="200")
    direct = plan_hole_fill([lot], _ctx(nearest_buy="0", pool="1000"))
    assert direct is not None
    walked = Decimal(0)
    current = lot
    while current.current_tp_price > direct.new_tp_price:
        step = plan_hole_fill([current], _ctx(nearest_buy="0", pool="1000"))
        assert step is not None
        walked += step.credit_drawn
        current = _pos(
            1,
            str(step.new_tp_price),
            entry="0.03000",
            qty="200",
            credit=str(step.new_credit),
        )
    assert abs(direct.credit_drawn - walked) < Decimal("0.0001")


def test_bottom_tp_not_pushed_below_wall_floor() -> None:
    # wall_floor = nearest_buy 0.02760 + tp_step 0.0001 + grid_step 0.00005
    #            = 0.02775; a TP already there cannot move lower
    positions = [_pos(1, "0.02775")]
    assert plan_hole_fill(positions, _ctx(nearest_buy="0.02760")) is None


def test_tp_one_step_above_floor_moves_down_to_the_floor() -> None:
    # floor 0.02775; a TP one grid_step above it settles onto it
    positions = [_pos(1, "0.02780")]
    decision = plan_hole_fill(positions, _ctx(nearest_buy="0.02760"))
    assert decision is not None
    assert decision.new_tp_price == Decimal("0.02775")


def test_underwater_move_draws_credit_and_keeps_pair_positive() -> None:
    victim = _pos(1, "0.02810", entry="0.03000", qty="200")
    decision = plan_hole_fill([victim], _ctx(nearest_buy="0", pool="0.4"))
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
    assert plan_hole_fill([victim], _ctx(pool="0.10", nearest_buy="0")) is None


def test_occupied_slot_below_is_skipped_for_next_victim() -> None:
    # 02800's slot (02795) is occupied; the mover is 02810 into empty 02805
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02810"),
    ]
    decision = plan_hole_fill(positions, _ctx())
    assert decision is not None and decision.target_position_id == 3


def test_no_move_when_pool_nonpositive_or_empty() -> None:
    assert plan_hole_fill([_pos(1, "0.02810")], _ctx(pool="0")) is None
    assert plan_hole_fill([], _ctx()) is None


def test_partially_filled_victim_is_skipped() -> None:
    # 02810 would move into the empty 02805 slot, but it is mid-fill: a
    # replacement sell on its full qty would oversell, so it is never chosen.
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02810", filled="35"),
    ]
    assert plan_hole_fill(positions, _ctx()) is None


def test_a_partly_filled_lot_is_never_the_one_moved() -> None:
    # 02810 is partially filled, so the gap at 02805 is filled by 02815
    positions = [
        _pos(1, "0.02795"),
        _pos(2, "0.02800"),
        _pos(3, "0.02810", filled="35"),
        _pos(4, "0.02815"),
    ]
    decision = plan_hole_fill(positions, _ctx())
    assert decision is not None
    assert decision.target_position_id == 4
    assert decision.new_tp_price == Decimal("0.02805")


def test_share_tracks_load_between_its_bounds() -> None:
    low, high = Decimal("0.20"), Decimal("0.80")
    assert compensation_share(Decimal("0.05"), low=low, high=high) == low
    assert compensation_share(Decimal("0.50"), low=low, high=high) == Decimal(
        "0.50"
    )
    assert compensation_share(Decimal("0.95"), low=low, high=high) == high


def test_share_collapses_to_the_ceiling_when_bounds_cross() -> None:
    assert compensation_share(
        Decimal("0.9"), low=Decimal("0.8"), high=Decimal("0.3")
    ) == Decimal("0.3")


def test_split_always_adds_back_to_the_whole_profit() -> None:
    profit = Decimal("0.123456789012345")
    budget, pocket = split_profit(profit, Decimal("0.37"))
    assert budget + pocket == profit
    assert budget > 0 and pocket > 0


def test_split_gives_a_zero_share_entirely_to_the_pocket() -> None:
    budget, pocket = split_profit(Decimal("5"), Decimal(0))
    assert budget == Decimal(0)
    assert pocket == Decimal("5")


def test_split_leaves_a_loss_out_of_the_budget() -> None:
    budget, pocket = split_profit(Decimal("-2"), Decimal("0.8"))
    assert budget == Decimal(0)
    assert pocket == Decimal("-2")


def test_load_is_zero_for_an_empty_book() -> None:
    assert account_load(
        [],
        quote_total=Decimal("100"),
        base_total=Decimal(0),
        price=Decimal("0.03"),
        maker_fee=Decimal("0.000625"),
    ) == Decimal(0)


def test_load_rises_as_cash_turns_into_lots() -> None:
    lots = [_pos(1, "0.03", entry="0.02", qty="1000")]
    idle = account_load(
        lots,
        quote_total=Decimal("1000"),
        base_total=Decimal("1000"),
        price=Decimal("0.03"),
        maker_fee=Decimal("0.000625"),
    )
    loaded = account_load(
        lots,
        quote_total=Decimal("10"),
        base_total=Decimal("1000"),
        price=Decimal("0.03"),
        maker_fee=Decimal("0.000625"),
    )
    assert idle < loaded <= Decimal(1)


def test_hole_fill_moves_the_dearest_lot_the_pool_affords() -> None:
    cheap = _pos(1, "0.02900", entry="0.02000", qty="200")
    dear = _pos(2, "0.05000", entry="0.04000", qty="200")
    decision = plan_hole_fill(
        [cheap, dear], _ctx(pool="1000", nearest_buy="0")
    )
    assert decision is not None
    assert decision.target_position_id == 2
    assert decision.credit_drawn > 0


def test_a_thin_pool_falls_back_to_a_single_step() -> None:
    dear = _pos(1, "0.05000", entry="0.04000", qty="200")
    rich = plan_hole_fill([dear], _ctx(pool="1000", nearest_buy="0"))
    thin = plan_hole_fill([dear], _ctx(pool="0.0001", nearest_buy="0"))
    assert rich is not None and thin is not None
    # the rich pool reaches the gap by the market; the thin one only nudges
    assert rich.new_tp_price < thin.new_tp_price
    assert thin.new_tp_price == Decimal("0.04995")


def test_hole_fill_needs_a_pool() -> None:
    lot = _pos(1, "0.05000", entry="0.04000", qty="200")
    assert plan_hole_fill([lot], _ctx(pool="0", nearest_buy="0")) is None


def test_the_offset_holds_the_gap_above_market() -> None:
    lot = _pos(1, "0.05000", entry="0.02000", qty="200")
    near = plan_hole_fill([lot], _ctx(pool="1000", nearest_buy="0"))
    far = plan_hole_fill(
        [lot],
        _ctx(pool="1000", nearest_buy="0"),
        offset=Decimal("0.10"),
    )
    assert near is not None and far is not None
    assert far.new_tp_price > near.new_tp_price


def test_hole_fill_never_raises_a_take_profit() -> None:
    lot = _pos(1, "0.02790", entry="0.02000", qty="200")
    decision = plan_hole_fill(
        [lot], _ctx(pool="1000", nearest_buy="0"), offset=Decimal("0.50")
    )
    assert decision is None


def test_hole_fill_leaves_a_partly_sold_lot_alone() -> None:
    lot = _pos(1, "0.05000", entry="0.04000", qty="200", filled="100")
    assert plan_hole_fill([lot], _ctx(pool="1000", nearest_buy="0")) is None


def test_market_exit_sells_top_alone_when_it_clears_the_minimum() -> None:
    # 200 x 0.02768 = 5.54 -> above the 5.00 minimum, no partner needed
    lots = [
        _pos(1, "0.05300", entry="0.05290", qty="200"),
        _pos(2, "0.02800", entry="0.02790", qty="200"),
    ]
    plan = plan_market_exit(lots, _ctx(pool="1000", min_order_amt="5"))
    assert plan is not None
    assert plan.position_ids == (1,)
    assert plan.qty == Decimal("200")
    assert plan.credit_drawn > 0


def test_market_exit_pairs_with_the_smallest_affordable_partner() -> None:
    # top is worth 2.49 at market, so it needs a partner to reach 5.00;
    # the smallest of the eligible lots is the one that comes along
    lots = [
        _pos(1, "0.05300", entry="0.05290", qty="90"),
        _pos(2, "0.02800", entry="0.02790", qty="300"),
        _pos(3, "0.02810", entry="0.02800", qty="120"),
    ]
    plan = plan_market_exit(lots, _ctx(pool="1000", min_order_amt="5"))
    assert plan is not None
    assert plan.position_ids == (1, 3)
    assert plan.qty == Decimal("210")


def test_market_exit_skips_a_partner_the_pool_cannot_cover() -> None:
    # lot 3 is the smallest partner but is itself deep underwater, so a
    # thin pool cannot buy the pair; the planner falls through to lot 2,
    # which is bigger yet costs almost nothing to close
    lots = [
        _pos(1, "0.05300", entry="0.05290", qty="90"),
        _pos(2, "0.02800", entry="0.02790", qty="300"),
        _pos(3, "0.05020", entry="0.05000", qty="100"),
    ]
    plan = plan_market_exit(lots, _ctx(pool="2.5", min_order_amt="5"))
    assert plan is not None
    assert plan.position_ids == (1, 2)


def test_market_exit_returns_none_when_the_pool_is_too_thin() -> None:
    lots = [
        _pos(1, "0.05300", entry="0.05290", qty="90"),
        _pos(2, "0.02800", entry="0.02790", qty="300"),
    ]
    assert (
        plan_market_exit(lots, _ctx(pool="0.001", min_order_amt="5")) is None
    )


def test_market_exit_walks_down_when_the_top_cannot_be_paired() -> None:
    # the two highest are stranded far too deep for this pool; the scan
    # keeps going and retires the cheapest one it can actually afford
    lots = [
        _pos(1, "0.09000", entry="0.08990", qty="60"),
        _pos(2, "0.05300", entry="0.05290", qty="90"),
        _pos(3, "0.02800", entry="0.02790", qty="300"),
    ]
    plan = plan_market_exit(lots, _ctx(pool="0.5", min_order_amt="5"))
    assert plan is not None
    assert plan.position_ids == (3,)


def test_market_exit_never_retires_a_lot_already_in_profit() -> None:
    # market is above this lot's entry, so it will sell at its own
    # take-profit; retiring it would throw the remaining step away
    lots = [_pos(1, "0.02900", entry="0.02700", qty="300")]
    assert plan_market_exit(lots, _ctx(pool="1000", min_order_amt="5")) is None


def test_market_exit_ignores_partially_filled_lots() -> None:
    lots = [_pos(1, "0.05300", entry="0.05290", qty="200", filled="50")]
    assert plan_market_exit(lots, _ctx(pool="1000", min_order_amt="5")) is None


def test_market_exit_draw_covers_fees_so_the_pair_clears_zero() -> None:
    lots = [_pos(1, "0.05300", entry="0.05290", qty="200")]
    ctx = _ctx(pool="1000", min_order_amt="5")
    plan = plan_market_exit(lots, ctx)
    assert plan is not None
    proceeds = (
        Decimal("200") * ctx.current_price * (Decimal(1) - ctx.taker_fee)
    )
    cost = Decimal("0.05290") * Decimal("200")
    assert plan.credit_drawn >= cost - proceeds
