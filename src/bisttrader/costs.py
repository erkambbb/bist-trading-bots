"""Türkiye işlem maliyet modeli (docs/01-maliyetler-ve-vergiler.md).

Açık maliyet: komisyon (+BSMV) ve kurum ödemiyorsa borsa/Takasbank payı.
Örtük maliyet: fiyat adımı cinsinden yarım spread + kayma; dolum fiyatına yansıtılır.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import load_config


@dataclass(frozen=True)
class TickTable:
    rows: tuple[tuple[float, float], ...]

    def tick(self, price: float) -> float:
        t = self.rows[0][1]
        for lo, step in self.rows:
            if price >= lo:
                t = step
        return t

    def round(self, price: float, side: str) -> float:
        """Alışta yukarı, satışta aşağı yuvarlar (emir fiyatı için)."""
        t = self.tick(price)
        n = price / t
        n = math.ceil(n - 1e-9) if side == "buy" else math.floor(n + 1e-9)
        return round(n * t, 6)


class CostModel:
    def __init__(self, cost_cfg: dict | None = None, scenario: str = "S1_midas_matriks_api",
                 implicit: str = "base"):
        self.cfg = cost_cfg or load_config("costs")
        self.scenario_name = scenario
        sc = self.cfg["scenarios"][scenario]
        bsmv = self.cfg["taxes"]["bsmv_on_commission"]
        bist = self.cfg["bist"]
        rate = sc["commission"] * (1 + bsmv)
        if not sc.get("exchange_fees_paid_by_broker", False):
            rate += (bist["exchange_fee"] + bist["clearing_fee"]) * (1 + bsmv)
        self.explicit_rate = rate
        self.ticks = TickTable(tuple((float(a), float(b)) for a, b in bist["tick_table"]))
        ic = self.cfg["implicit_costs"]
        self.half_spread = float(ic["half_spread_ticks"])
        self.stop_slip = float(ic["stop_entry_slippage_ticks"])
        self.exit_slip = float(ic["marketable_exit_slippage_ticks"])
        # 'optimistic' senaryoda adım bazlı kayma yarıya iner
        self.slip_scale = 0.5 if implicit == "optimistic" else 1.0
        self.cost_multiplier = 1.0

    # --- açık maliyet ---
    def commission(self, notional: float) -> float:
        return abs(notional) * self.explicit_rate * self.cost_multiplier

    # --- örtük maliyet: dolum fiyatı ---
    def fill(self, ref_price: float, side: str, kind: str = "market") -> float:
        """kind: 'stop' (stop tetikli giriş/çıkış), 'market' (marketable limit), 'limit' (pasif)."""
        if kind == "limit":
            return ref_price
        t = self.ticks.tick(ref_price)
        slip = self.stop_slip if kind == "stop" else self.exit_slip
        n_ticks = (self.half_spread + slip) * self.slip_scale * self.cost_multiplier
        adj = n_ticks * t
        return ref_price + adj if side == "buy" else max(ref_price - adj, t)

    def roundtrip_estimate(self, price: float) -> float:
        """Gidiş-dönüş toplam maliyet tahmini (oran). Stop mesafesi alt sınırı için."""
        t = self.ticks.tick(price)
        implicit = ((self.half_spread + self.stop_slip) + (self.half_spread + self.exit_slip)) \
            * self.slip_scale * t / price
        return (2 * self.explicit_rate + implicit) * self.cost_multiplier


class ForeignCosts:
    """Midas yurt dışı + kripto maliyetleri."""

    def __init__(self, cost_cfg: dict | None = None):
        c = cost_cfg or load_config("costs")
        f = c["midas_foreign"]
        self.us_commission_usd = float(f["us_commission_usd"])
        self.fx_spread = float(f["fx_spread_per_conversion"])
        self.fx_bsmv = float(c["taxes"]["bsmv_on_fx_purchase"])
        self.crypto_rate = float(c["midas_crypto"]["commission_tiers_try"][0][1])
        self.crypto_tx_tax = float(c["taxes"]["crypto"]["transaction_tax"])
        self.min_position_usd = float(f["min_position_usd"])

    def fx_buy_usd_cost(self, try_amount: float) -> float:
        """TL→USD dönüşümünde kaybedilen TL (makas + BSMV)."""
        return abs(try_amount) * (self.fx_spread + self.fx_bsmv)

    def fx_sell_usd_cost(self, try_amount: float) -> float:
        return abs(try_amount) * self.fx_spread

    def crypto_cost(self, notional: float, side: str) -> float:
        tax = self.crypto_tx_tax if side == "sell" else 0.0
        return abs(notional) * (self.crypto_rate + tax)
