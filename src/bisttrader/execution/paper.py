"""Paper broker: canlı fiyatlarla sanal dolum (maliyet modeli uygulanır)."""
from __future__ import annotations

from ..costs import CostModel
from .base import Broker, BrokerPosition, Order


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, costs: CostModel, cash: float):
        self.costs = costs
        self.cash = cash
        self._pos: dict[str, BrokerPosition] = {}
        self._orders: dict[str, Order] = {}
        self.fills: list[dict] = []
        self.last: dict[str, dict] = {}   # symbol -> {"bid","ask","last","high","low"}

    def on_quote(self, symbol: str, last: float, high: float | None = None, low: float | None = None) -> None:
        self.last[symbol] = {"last": last, "high": high or last, "low": low or last}
        self._match(symbol)

    def place(self, order: Order) -> Order:
        self._orders[order.client_id] = order
        order.broker_id = order.client_id
        self._match(order.symbol)
        return order

    def cancel(self, order: Order) -> Order:
        if order.status in ("new", "partial"):
            order.status = "canceled"
        self._orders.pop(order.client_id, None)
        return order

    def positions(self) -> dict[str, BrokerPosition]:
        return {k: v for k, v in self._pos.items() if v.qty > 0}

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status in ("new", "partial")]

    def _match(self, symbol: str) -> None:
        q = self.last.get(symbol)
        if not q:
            return
        for o in list(self._orders.values()):
            if o.symbol != symbol or o.status not in ("new", "partial"):
                continue
            px = q["last"]
            if o.order_type == "market":
                fill = self.costs.fill(px, o.side, "market")
            elif o.side == "buy" and px <= o.price:
                fill = min(o.price, self.costs.fill(px, "buy", "market"))
            elif o.side == "sell" and px >= o.price:
                fill = max(o.price, self.costs.fill(px, "sell", "market"))
            else:
                continue
            qty = o.qty - o.filled_qty
            com = self.costs.commission(qty * fill)
            pos = self._pos.setdefault(symbol, BrokerPosition(symbol, 0, 0.0))
            if o.side == "buy":
                pos.avg_price = (pos.avg_price * pos.qty + fill * qty) / (pos.qty + qty)
                pos.qty += qty
                self.cash -= qty * fill + com
            else:
                qty = min(qty, pos.qty)
                pos.qty -= qty
                self.cash += qty * fill - com
            o.filled_qty += qty
            o.avg_fill = fill
            o.status = "filled"
            self.fills.append({"symbol": symbol, "side": o.side, "qty": qty, "price": fill, "commission": com,
                               "client_id": o.client_id, "note": o.note})
            self._orders.pop(o.client_id, None)
