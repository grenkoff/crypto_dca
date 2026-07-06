from __future__ import annotations

from decimal import Decimal

from core.strategy.reconstruction import Fill, fifo_residual, select_free_lots


def _b(price: str, qty: str) -> Fill:
    return Fill(side="Buy", price=Decimal(price), qty=Decimal(qty))


def _s(price: str, qty: str) -> Fill:
    return Fill(side="Sell", price=Decimal(price), qty=Decimal(qty))


def test_fifo_consumes_oldest_buys_first() -> None:
    fills = [_b("0.031", "100"), _b("0.030", "100"), _s("0.032", "150")]
    # sell 150 eats all of the first lot (100) and 50 of the second
    assert fifo_residual(fills) == [(Decimal("0.030"), Decimal("50"))]


def test_fifo_all_sold_leaves_nothing() -> None:
    fills = [_b("0.031", "100"), _s("0.032", "100")]
    assert fifo_residual(fills) == []


def test_fifo_oversell_ignored() -> None:
    fills = [_b("0.031", "100"), _s("0.032", "500")]
    assert fifo_residual(fills) == []


def test_fifo_multiple_open_lots() -> None:
    fills = [_b("0.0305", "200"), _b("0.0304", "200"), _s("0.031", "100")]
    assert fifo_residual(fills) == [
        (Decimal("0.0305"), Decimal("100")),
        (Decimal("0.0304"), Decimal("200")),
    ]


def test_select_free_lots_cheapest_first_and_trims() -> None:
    residual = [
        (Decimal("0.0307"), Decimal("195")),
        (Decimal("0.0305"), Decimal("787")),
        (Decimal("0.0304"), Decimal("197")),
        (Decimal("0.0306"), Decimal("574")),
    ]
    # free 785 -> take 0.0304 (197) fully, then 0.0305 trimmed to 588
    lots = select_free_lots(residual, Decimal("785"))
    assert lots == [
        (Decimal("0.0304"), Decimal("197")),
        (Decimal("0.0305"), Decimal("588")),
    ]


def test_select_free_lots_zero() -> None:
    assert select_free_lots([(Decimal("0.03"), Decimal("100"))], Decimal("0")) == []
