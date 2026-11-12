"""Broker arayüzü. Strateji kodu sadece bu arayüzle konuşur (backtest/paper/canlı aynı)."""
from __future__ import annotations

import abc
import itertools
import time
from dataclasses import dataclass, field

_ids = itertools.count(1)


@dataclass
class Order:
    symbol: str
    side: str                 # buy | sell
    qty: int
    price: float | None       # None → piyasa emri
    order_type: str = "limit"  # limit | market
    tif: str = "day"
    client_id: str = field(default_factory=lambda: f"bt-{int(time.time())}-{next(_ids)}")
    broker_id: str | None = None
    status: str = "new"       # new | partial | filled | canceled | rejected
    filled_qty: int = 0
    avg_fill: float = 0.0
    note: str = ""


@dataclass
class BrokerPosition:
    symbol: str
    qty: int
    avg_price: float


class Broker(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def place(self, order: Order) -> Order: ...

    @abc.abstractmethod
    def cancel(self, order: Order) -> Order: ...

    def modify(self, order: Order, price: float | None = None, qty: int | None = None) -> Order:
        self.cancel(order)
        new = Order(order.symbol, order.side, qty or order.qty - order.filled_qty,
                    price if price is not None else order.price, order.order_type, order.tif)
        return self.place(new)

    @abc.abstractmethod
    def positions(self) -> dict[str, BrokerPosition]: ...

    @abc.abstractmethod
    def open_orders(self) -> list[Order]: ...

    def refresh(self) -> None:
        """Emir durumlarını kurumdan günceller (varsa)."""

    def flatten_all(self, last_prices: dict[str, float]) -> None:
        for o in list(self.open_orders()):
            self.cancel(o)
        for sym, p in self.positions().items():
            if p.qty > 0:
                self.place(Order(sym, "sell", p.qty, None, "market", note="flatten"))
