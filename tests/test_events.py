import pytest

from algotrading.core.enums import Direction, OrderType
from algotrading.core.events import FillEvent, OrderEvent


def test_direction_sign():
    assert Direction.BUY.sign == 1
    assert Direction.SELL.sign == -1


def test_order_rejects_nonpositive_qty():
    with pytest.raises(ValueError):
        OrderEvent("BTCUSDT", OrderType.MARKET, 0, Direction.BUY)


def test_limit_order_requires_price():
    with pytest.raises(ValueError):
        OrderEvent("BTCUSDT", OrderType.LIMIT, 1, Direction.BUY)


def test_fill_gross_value():
    from datetime import datetime

    f = FillEvent(datetime(2023, 1, 1), "BTCUSDT", Direction.BUY, 2, 100.0, 0.2)
    assert f.gross_value == 200.0
