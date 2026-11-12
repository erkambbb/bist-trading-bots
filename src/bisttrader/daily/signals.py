"""Günlük Bot karar fonksiyonları (docs/03-gunluk-bot.md).

Bu fonksiyonlar saf (yan etkisiz) tutulur; backtest motoru ve canlı döngü aynı
fonksiyonları çağırır.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import get
from ..costs import CostModel
from ..indicators import atr, ema


# --------------------------------------------------------------------------- #
# Günlük istatistikler (önceki kapanışa kadar)
# --------------------------------------------------------------------------- #
def compute_daily_stats(daily: pd.DataFrame) -> pd.DataFrame:
    """Her (tarih, hisse) için O GÜNÜN kapanışı itibarıyla istatistikler.

    Gün d'nin kararları için d-1 satırı kullanılmalıdır (`prev_stats`).
    """
    out = []
    for sym, g in daily.sort_values("date").groupby("symbol"):
        g = g.set_index("date")
        a = atr(g["high"], g["low"], g["close"], 14)
        turnover = g["close"] * g["volume"]
        df = pd.DataFrame({
            "symbol": sym,
            "close": g["close"],
            "atr_pct": a / g["close"],
            "adv20_try": turnover.rolling(20, min_periods=15).mean(),
            "corp_action": (g.get("dividend", pd.Series(0, index=g.index)).fillna(0) != 0),
        })
        out.append(df)
    if not out:
        return pd.DataFrame()
    res = pd.concat(out).reset_index().rename(columns={"index": "date"})
    return res


def index_regime_table(index_daily: pd.DataFrame, global_daily: pd.DataFrame,
                       index_symbol: str = "XU100") -> pd.DataFrame:
    """Gün d için rejim girdileri (d-1 kapanışına göre)."""
    ix = index_daily[index_daily["symbol"] == index_symbol].set_index("date").sort_index()
    if ix.empty:
        return pd.DataFrame()
    e20 = ema(ix["close"], 20)
    t = pd.DataFrame({
        "ix_close": ix["close"],
        "ix_ema20": e20,
        "ix_ema20_slope": e20.diff(),
    })
    if not global_daily.empty:
        g = global_daily.pivot_table(index="date", columns="symbol", values="close")
        if "VIX" in g:
            t["vix"] = g["VIX"].reindex(t.index).ffill()
        if "SPX" in g:
            spx = g["SPX"].ffill()
            t["spx_ret"] = spx.pct_change().reindex(t.index).ffill()
    # d günü için önceki günün değerleri
    return t.shift(1)


# --------------------------------------------------------------------------- #
# Rejim
# --------------------------------------------------------------------------- #
@dataclass
class Regime:
    mode: str                     # normal | cautious | off
    reasons: list[str] = field(default_factory=list)


def decide_regime(row: pd.Series | None, xu_gap: float | None, cfg: dict) -> Regime:
    if not get(cfg, "regime.enabled", True):
        return Regime("normal", ["rejim filtresi kapalı"])
    reasons: list[str] = []
    off_gap = get(cfg, "regime.off_if.xu100_open_gap_below", -0.03)
    if xu_gap is not None and not math.isnan(xu_gap) and xu_gap < off_gap:
        return Regime("off", [f"XU100 açılış gap {xu_gap:.2%}"])
    if row is None or pd.isna(row.get("ix_ema20")):
        return Regime("normal", ["rejim verisi yok"])
    cautious = False
    if not (row["ix_close"] > row["ix_ema20"] and row["ix_ema20_slope"] > 0):
        cautious = True
        reasons.append("XU100 < EMA20 veya eğim ≤ 0")
    vix = row.get("vix", np.nan)
    if pd.notna(vix) and vix > get(cfg, "regime.cautious_if.vix_above", 25):
        cautious = True
        reasons.append(f"VIX {vix:.1f}")
    spx = row.get("spx_ret", np.nan)
    if pd.notna(spx) and spx < get(cfg, "regime.cautious_if.spx_futures_premarket_below", -0.01):
        cautious = True
        reasons.append(f"S&P önceki gün {spx:.2%}")
    return Regime("cautious" if cautious else "normal", reasons)


# --------------------------------------------------------------------------- #
# Evren
# --------------------------------------------------------------------------- #
def passes_universe(prev: pd.Series, cfg: dict, costs: CostModel) -> bool:
    px = prev["close"]
    if not (px > 0) or pd.isna(prev["atr_pct"]) or pd.isna(prev["adv20_try"]):
        return False
    if costs.ticks.tick(px) / px > get(cfg, "universe.max_tick_to_price", 0.0008):
        return False
    if prev["adv20_try"] < get(cfg, "universe.min_adv20_try", 3e8):
        return False
    lo, hi = get(cfg, "universe.atr_pct_range", [0.02, 0.07])
    if not (lo <= prev["atr_pct"] <= hi):
        return False
    return True


# --------------------------------------------------------------------------- #
# Seçim
# --------------------------------------------------------------------------- #
def selection_score(rvol: float, news: bool, gap: float, atr_pct: float, cfg: dict) -> float:
    w_news = get(cfg, "selection.score_weights.news_flag", 0.3)
    w_gap = get(cfg, "selection.score_weights.gap_norm", 0.2)
    g_norm = gap / atr_pct if atr_pct > 0 else 0.0
    return math.log(max(rvol, 1e-9)) + w_news * float(news) + w_gap * min(max(g_norm, 0.0), 1.0)


def select_candidates(or_table: pd.DataFrame, regime: Regime, cfg: dict) -> pd.DataFrame:
    """or_table sütunları: symbol, rvol, gap, atr_pct, or_open, or_close, or_vwap, news."""
    if or_table.empty or regime.mode == "off":
        return or_table.iloc[0:0]
    thr = get(cfg, "selection.rvol_threshold", 2.0)
    if regime.mode == "cautious":
        thr *= 1.5
        n = get(cfg, "selection.max_candidates.cautious", 2)
    else:
        n = get(cfg, "selection.max_candidates.normal", 4)
    t = or_table.copy()
    m = t["rvol"] >= thr
    if get(cfg, "selection.require_bullish_or_candle", True):
        m &= t["or_close"] > t["or_open"]
    if get(cfg, "selection.require_above_vwap", True):
        m &= t["or_close"] > t["or_vwap"]
    max_gap = get(cfg, "selection.max_gap_atr_without_news", 2.0)
    m &= ~((t["gap"] > max_gap * t["atr_pct"]) & (~t["news"].astype(bool)))
    t = t[m]
    if t.empty:
        return t
    t["score"] = [selection_score(r.rvol, bool(r.news), r.gap, r.atr_pct, cfg) for r in t.itertuples()]
    return t.sort_values("score", ascending=False).head(n)


# --------------------------------------------------------------------------- #
# Stop ve boyut
# --------------------------------------------------------------------------- #
def stop_distance(entry: float, atr_pct: float, cfg: dict, costs: CostModel,
                  structural_stop: float | None = None) -> float | None:
    """Stop mesafesi (oran). None → işlem yapılmaz."""
    k = get(cfg, "strategy_orb.stop.k_atr", 0.35)
    tick = costs.ticks.tick(entry)
    d_min = max(get(cfg, "strategy_orb.stop.min_cost_multiple", 6.7) * costs.roundtrip_estimate(entry),
                get(cfg, "strategy_orb.stop.min_ticks", 4) * tick / entry)
    if k == "structural":
        d = (entry - structural_stop) / entry if structural_stop is not None else 0.0
    else:
        d = float(k) * atr_pct
    d = max(d, d_min)
    if d > get(cfg, "strategy_orb.stop.max_pct", 0.02):
        return None
    return d


def position_qty(equity: float, entry: float, stop: float, adv20_try: float, open_notional: float,
                 cfg: dict, cautious: bool, risk_scale: float = 1.0) -> int:
    r = get(cfg, "risk.risk_per_trade_cautious" if cautious else "risk.risk_per_trade", 0.005) * risk_scale
    per_share_risk = max(entry - stop, 1e-9)
    by_risk = r * equity / per_share_risk
    cap_pct = get(cfg, "risk.max_position_pct", 0.20)
    by_cap = cap_pct * equity / entry
    by_liq = get(cfg, "risk.liquidity_fraction_adv", 0.005) * adv20_try / entry
    gross_left = get(cfg, "risk.gross_exposure_cap", 0.80) * equity - open_notional
    by_gross = max(gross_left, 0.0) / entry
    q = math.floor(min(by_risk, by_cap, by_liq, by_gross))
    return max(q, 0)
