"""Compensator: compact the TP grid using a share of banked profit.

Each close hands over only the slice of its profit that the account's
load earns; the rest goes to the pocket and is never spent here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal

import structlog

from core.db.models import Position, StrategyConfig
from core.exchange.bybit import BybitClient
from core.exchange.types import Instrument, Side
from core.services import repository
from core.services.balances import BalanceCache
from core.services.events import EventBus
from core.services.order_common import link_id
from core.strategy.compensation import (
    account_load,
    compensation_share,
    plan_hole_fill,
    split_profit,
)
from core.strategy.rounding import min_notional_price
from core.strategy.types import (
    CompensationContext,
    CompensationDecision,
    OpenPosition,
)

log = structlog.get_logger()


@dataclass(frozen=True)
class CompensationOutcome:
    """What one close's profit bought: the moves, the split, the rest."""

    moves: list[dict[str, str]]
    share: Decimal
    pool_left: Decimal


_MAX_MOVES_PER_CLOSE = 12
_MOVE_PAUSE_SECONDS = 0.05


def _as_open(position: Position) -> OpenPosition:
    """The strategy-facing view of a stored position."""
    return OpenPosition(
        id=int(position.id),
        entry_price=position.entry_price,
        qty=position.qty,
        fees_in=position.fees_in,
        current_tp_price=position.tp_price
        if position.tp_price is not None
        else Decimal(0),
        compensation_credit=position.compensation_credit,
        filled_qty=position.filled_qty,
    )


def _retagged(
    positions: list[OpenPosition], decision: CompensationDecision
) -> list[OpenPosition]:
    """The same lots with the moved one carrying its new take-profit."""
    return [
        replace(
            lot,
            current_tp_price=decision.new_tp_price,
            compensation_credit=decision.new_credit,
        )
        if lot.id == decision.target_position_id
        else lot
        for lot in positions
    ]


class Compensator:
    """Pull one TP down onto its empty grid slot, funded by banked profit."""

    def __init__(
        self,
        *,
        client: BybitClient,
        instrument: Instrument,
        config: StrategyConfig,
        bus: EventBus,
        balances: BalanceCache,
    ) -> None:
        self.client = client
        self.instrument = instrument
        self.config = config
        self.bus = bus
        self.balances = balances

    async def apply(
        self,
        *,
        profit: Decimal,
        source_position_id: int,
        current_price: Decimal,
    ) -> CompensationOutcome:
        """Split ``profit``, bank both halves, then spend the budget.

        Reports the moves made along with the split that funded them, so
        the caller can show a close, its compensations and the state of
        the pool as one message.
        """
        positions = [_as_open(p) for p in await repository.open_positions()]
        share = await self._share(positions, current_price)
        budget, pocket = split_profit(profit, share)
        pool = await repository.accrue_split(
            pool_add=budget, pocket_add=pocket
        )
        log.info(
            "compensation.split",
            share=str(share),
            budget=str(budget),
            pocket=str(pocket),
        )
        moves = await self._drain(
            positions, pool, current_price, source_position_id
        )
        return CompensationOutcome(
            moves=moves,
            share=share,
            pool_left=await repository.pending_credit(),
        )

    async def drain_pool(
        self, current_price: Decimal, source_position_id: int
    ) -> list[dict[str, str]]:
        """Spend the banked pool without a fresh close to fund it.

        The trader only ever compensates on a profitable close; this is
        the manual entry point for spending what has already been
        banked.
        """
        positions = [_as_open(p) for p in await repository.open_positions()]
        pool = await repository.pending_credit()
        return await self._drain(
            positions, pool, current_price, source_position_id
        )

    async def _share(
        self, positions: list[OpenPosition], current_price: Decimal
    ) -> Decimal:
        """The share of profit this close may spend on compensation."""
        snapshot = await self.balances.snapshot()
        low = self.config.comp_share_min
        high = self.config.comp_share_max
        if not snapshot:
            return compensation_share(low, low=low, high=high)
        quote = snapshot.get(self.instrument.quote_coin)
        base = snapshot.get(self.instrument.base_coin)
        ratio = account_load(
            positions,
            quote_total=quote.total if quote is not None else Decimal(0),
            base_total=base.total if base is not None else Decimal(0),
            price=current_price,
            maker_fee=self.config.maker_fee,
        )
        return compensation_share(ratio, low=low, high=high)

    async def _nearest_buy(self) -> Decimal:
        """Price of the highest resting buy, or zero when the grid is bare."""
        return await repository.highest_resting_buy()

    def _context(
        self, pool: Decimal, current_price: Decimal, nearest_buy: Decimal
    ) -> CompensationContext:
        """Market and grid context for a compensation decision."""
        return CompensationContext(
            pool=pool,
            maker_fee=self.config.maker_fee,
            current_price=current_price,
            tick_size=self.instrument.tick_size,
            grid_step=self.config.grid_step,
            tp_step=self.config.tp_step,
            nearest_buy_price=nearest_buy,
            min_order_amt=self.instrument.min_order_amt,
        )

    async def _drain(
        self,
        positions: list[OpenPosition],
        pool: Decimal,
        current_price: Decimal,
        source_position_id: int,
    ) -> list[dict[str, str]]:
        """Spend the pool filling gaps nearest market, one lot per gap.

        Each move takes the furthest-stranded lot that fits, so a wall
        walks down in one move instead of a cascade of single steps —
        same cost, a fraction of the traffic. Moves continue while the
        pool funds them; the cap only guards against a runaway burst.
        """
        moves: list[dict[str, str]] = []
        nearest_buy = await self._nearest_buy()
        for _ in range(_MAX_MOVES_PER_CLOSE):
            if pool <= 0:
                return moves
            decision = plan_hole_fill(
                positions,
                self._context(pool, current_price, nearest_buy),
                offset=self.config.comp_hole_offset,
            )
            if decision is None:
                return moves
            move = await self._execute(decision, pool, source_position_id)
            if move is None:
                return moves
            moves.append(move)
            pool -= decision.credit_drawn
            positions = _retagged(positions, decision)
            await asyncio.sleep(_MOVE_PAUSE_SECONDS)
        return moves

    async def _execute(
        self,
        decision: CompensationDecision,
        pool: Decimal,
        source_position_id: int,
    ) -> dict[str, str] | None:
        """Do the exchange move and persist it; report it if applied."""
        symbol = str(self.config.symbol)
        target = await repository.get_position(decision.target_position_id)
        old_tp = target.tp_price
        if not target.tp_order_id:
            log.warning("compensation.target_has_no_tp", id=target.id)
            return None
        if decision.new_tp_price * target.qty < self.instrument.min_order_amt:
            log.warning(
                "compensation.skip_below_min_notional",
                id=target.id,
                new_tp=str(decision.new_tp_price),
            )
            return None
        try:
            await self.client.cancel_order(symbol, target.tp_order_id)
        except Exception as exc:
            log.warning(
                "compensation.cancel_failed", id=target.id, error=str(exc)
            )
            return None
        try:
            new_tp_order_id = await self.client.place_limit(
                symbol,
                Side.SELL,
                target.qty,
                decision.new_tp_price,
                order_link_id=link_id("grid-tp-comp", target.level_index),
            )
        except Exception as exc:
            await self._restore_protection(target, exc)
            return None
        await repository.record_compensation(
            target=target,
            new_tp_price=decision.new_tp_price,
            new_tp_order_id=new_tp_order_id,
            new_credit=decision.new_credit,
            credit_drawn=decision.credit_drawn,
            source_position_id=source_position_id,
            new_pending=pool - decision.credit_drawn,
        )
        log.info(
            "compensation.applied",
            id=target.id,
            new_tp=str(decision.new_tp_price),
            drawn=str(decision.credit_drawn),
        )
        return {
            "target_position": str(target.id),
            "old_tp": str(old_tp) if old_tp is not None else "",
            "new_tp": str(decision.new_tp_price),
            "drawn": str(decision.credit_drawn),
        }

    async def _restore_protection(
        self, target: Position, place_error: Exception
    ) -> None:
        """Re-place a protective sell after a failed compensation placement.

        Priced at the higher of the old TP and the minimum notional price so
        it always clears the exchange minimum — never left naked.
        """
        min_price = min_notional_price(
            self.instrument.min_order_amt,
            target.qty,
            self.instrument.tick_size,
        )
        price = max(target.tp_price or Decimal(0), min_price)
        try:
            order_id = await self.client.place_limit(
                str(self.config.symbol),
                Side.SELL,
                target.qty,
                price,
                order_link_id=link_id("grid-tp-restore", target.level_index),
            )
        except Exception as restore_error:
            log.exception(
                "compensation.restore_failed",
                id=target.id,
                place_error=str(place_error),
                restore_error=str(restore_error),
            )
            return
        await repository.set_tp(
            target=target, tp_price=price, tp_order_id=order_id
        )
        log.error(
            "compensation.restored_after_place_failure",
            id=target.id,
            price=str(price),
            error=str(place_error),
        )
