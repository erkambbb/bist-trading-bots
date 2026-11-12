"""MatriksIQ dış emir API'si (TCP 18890, JSON + char(11) sonlandırıcı).

Kaynak: Matriks "MatriksIQ dışarıdan emir kabulü API" dokümanı.
- Komutlar: 0 hesaplar, 1 pozisyonlar, 2 emirler, 3 yeni emir, 4 iptal, 5 düzelt, 6 keepalive, 7 hesap bilgisi
- OrderSide: 1 alış, 2 satış · TimeInForce: '0' gün · TransactionType: '1' normal
- OrderType: dokümanda tablo yok; örnekler "2" (limit) kullanıyor. FIX geleneğiyle '1' piyasa varsayılır.

⚠ Canlıya geçmeden önce MatriksIQ test ortamında (veya 1 lot ile) her komut doğrulanmalıdır.
MatriksIQ'nun açık ve aracı kuruma (ör. Midas) giriş yapılmış olması gerekir; API lisansı aktif olmalıdır.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time

from .base import Broker, BrokerPosition, Order

log = logging.getLogger(__name__)
TERM = chr(11)


class MatriksIQClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 18890, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: socket.socket | None = None
        self._buf = ""
        self._lock = threading.Lock()
        self.events: list[dict] = []

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        self._send_raw("SetMessageType0")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send_raw(self, text: str) -> None:
        assert self.sock is not None, "connect() çağrılmadı"
        self.sock.sendall((text + TERM).encode("utf-8"))

    def send(self, payload: dict) -> None:
        with self._lock:
            self._send_raw(json.dumps(payload, ensure_ascii=False))

    def recv(self, wait: float = 2.0) -> list[dict]:
        """Gelen mesajları okur (çağrı başına en fazla `wait` saniye)."""
        assert self.sock is not None
        out = []
        end = time.time() + wait
        while time.time() < end:
            try:
                chunk = self.sock.recv(65536).decode("utf-8", errors="replace")
            except socket.timeout:
                break
            if not chunk:
                break
            self._buf += chunk
            while TERM in self._buf:
                msg, self._buf = self._buf.split(TERM, 1)
                msg = msg.strip()
                if not msg:
                    continue
                try:
                    out.append(json.loads(msg))
                except json.JSONDecodeError:
                    log.warning("JSON çözülemedi: %s", msg[:200])
            if out:
                break
        self.events.extend(out)
        return out

    def request(self, payload: dict, expect: int | None = None, wait: float = 3.0) -> list[dict]:
        self.send(payload)
        got, end = [], time.time() + wait
        while time.time() < end:
            msgs = self.recv(0.5)
            got.extend(msgs)
            if expect is None or any(m.get("ApiCommands") == expect for m in msgs):
                break
        return got

    def keepalive(self) -> None:
        self.send({"ApiCommands": 6, "KeepAliveDate": time.strftime("%Y-%m-%dT%H:%M:%S")})


class MatriksIQBroker(Broker):
    name = "matriks_iq"

    def __init__(self, client: MatriksIQClient, brokage_id: str | None = None, account_id: str | None = None,
                 exchange_id: int = 4, dry_run: bool = True):
        self.c = client
        self.brokage_id, self.account_id = brokage_id, account_id
        self.exchange_id = exchange_id
        self.dry_run = dry_run
        self._orders: dict[str, Order] = {}

    def discover_account(self) -> None:
        msgs = self.c.request({"ApiCommands": 0}, expect=0)
        for m in msgs:
            if m.get("ApiCommands") == 0 and m.get("Accounts"):
                acc = m["Accounts"][0]
                self.brokage_id = self.brokage_id or str(acc.get("BrokageId"))
                ids = acc.get("AccountIdList") or [acc.get("AccountId")]
                self.account_id = self.account_id or str(ids[0])
        if not (self.brokage_id and self.account_id):
            raise RuntimeError("MatriksIQ hesap bilgisi alınamadı (kurum girişi yapıldı mı?)")

    def _base(self) -> dict:
        return {"BrokageId": self.brokage_id, "AccountId": self.account_id, "ExchangeId": self.exchange_id}

    def place(self, order: Order) -> Order:
        payload = {
            **self._base(),
            "Symbol": order.symbol,
            "OrderSide": 1 if order.side == "buy" else 2,
            "OrderQty": float(order.qty),
            "Price": float(order.price or 0.0),
            "OrderType": "1" if order.order_type == "market" else "2",
            "TimeInForce": "0",
            "TransactionType": "1",
            "IncludeAfterSession": False,
            "ApiCommands": 3,
        }
        if self.dry_run:
            log.info("[DRY-RUN] emir: %s", payload)
            order.status = "new"
            order.broker_id = f"dry-{order.client_id}"
            self._orders[order.client_id] = order
            return order
        msgs = self.c.request(payload, expect=3)
        for m in msgs:
            if m.get("ApiCommands") == 3 and m.get("Symbol") == order.symbol:
                order.broker_id = str(m.get("OrderID"))
                st = str(m.get("OrdStatus", "0"))
                order.status = {"0": "new", "1": "partial", "2": "filled", "4": "canceled", "8": "rejected"}.get(st, "new")
                break
        else:
            order.status = "rejected"
            order.note = "yanıt yok"
        self._orders[order.client_id] = order
        return order

    def cancel(self, order: Order) -> Order:
        if self.dry_run or not order.broker_id:
            order.status = "canceled"
            return order
        payload = {**self._base(), "OrderID": order.broker_id, "OrderID2": order.broker_id,
                   "OrderSide": 1 if order.side == "buy" else 2, "Symbol": order.symbol,
                   "LeavesQty": float(order.qty - order.filled_qty), "Quantity": float(order.qty),
                   "ApiCommands": 4}
        self.c.request(payload, expect=4)
        order.status = "canceled"
        return order

    def positions(self) -> dict[str, BrokerPosition]:
        if self.dry_run:
            return {}
        msgs = self.c.request({**self._base(), "ApiCommands": 1}, expect=1)
        out = {}
        for m in msgs:
            for p in m.get("PositionResponseList", []) or []:
                q = int(p.get("QtyAvailable") or p.get("QtyNet") or 0)
                if q:
                    out[p["Symbol"]] = BrokerPosition(p["Symbol"], q, float(p.get("AvgCost") or p.get("Cost") or 0))
        return out

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status in ("new", "partial")]

    def refresh(self) -> None:
        if self.dry_run:
            return
        for m in self.c.recv(0.2):
            if m.get("ApiCommands") == 3 and m.get("OrderID"):
                for o in self._orders.values():
                    if o.broker_id == str(m["OrderID"]):
                        st = str(m.get("OrdStatus", "0"))
                        o.status = {"0": "new", "1": "partial", "2": "filled", "4": "canceled", "8": "rejected"}.get(st, o.status)
                        o.filled_qty = int(m.get("FilledQty") or m.get("CumQty") or o.filled_qty)
                        o.avg_fill = float(m.get("AvgPx") or o.avg_fill)
