"""Orta Vade Bot günlük adımlı backtest motoru (docs/04-orta-vade-bot.md).

- Karar: haftanın son işlem günü kapanışından sonra (skor, rejim, kollar, çıkışlar).
- Günlük: felaket stopu, T2/T3 dilim tetikleri.
- Uygulama: ertesi işlem gününün kapanış fiyatından (BIST kapanış seansı varsayımı).
- Yurt dışı kolu: çekirdek SPY/QQQ + sektör ETF momentum uydusu (tekil ABD hissesi
  seçimi backtestte yok: bugünün büyük şirketleriyle seçim survivorship yanlılığı yaratır).
- Vergi: yurt dışı gerçekleşen TL kazanç FIFO + Yİ-ÜFE endekslemesi, yıl sonunda düşülür.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import get, load_config
from ..costs import ForeignCosts
from ..data.store import DataStore, pivot_close
from ..indicators import rsi
from . import factors as F

CORE_ETFS = {"SPY": 0.5, "QQQ": 0.5}
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC", "SMH"]
CRYPTO = {"BTC-USD": 0.7, "ETH-USD": 0.3}


@dataclass
class BistPos:
    qty: float
    cost: float                # toplam TL maliyet
    peak: float
    t1_date: pd.Timestamp
    t1_price: float
    target_value: float
    tranches: dict = field(default_factory=lambda: {"T1": "done", "T2": "pending", "T3": "pending"})
    trimmed: bool = False
    exit_stage: int = 0
    t1_sue_period: str | None = None

    @property
    def avg(self) -> float:
        return self.cost / self.qty if self.qty > 0 else 0.0


@dataclass
class Lot:
    qty: float
    usd_px: float
    fx: float
    date: pd.Timestamp


class MidtermBacktester:
    def __init__(self, store: DataStore, cfg: dict, cost_cfg: dict | None = None,
                 start: str = "2024-11-11", end: str | None = None, initial_equity: float = 500_000.0,
                 bist_implicit_per_side: float = 0.0005):
        self.store, self.cfg = store, cfg
        self.cost_cfg = cost_cfg or load_config("costs")
        self.fc = ForeignCosts(self.cost_cfg)
        self.start = pd.Timestamp(start)
        self.end = pd.Timestamp(end) if end else None
        self.E0 = float(initial_equity)
        self.bist_cost = bist_implicit_per_side
        self.tax_rate = float(get(self.cost_cfg, "taxes.foreign_equity.assumed_marginal_rate", 0.27))
        self.idx_thr = float(get(self.cost_cfg, "taxes.foreign_equity.indexation_threshold_ufe", 0.10))
        self.div_tax = float(get(self.cost_cfg, "taxes.bist_dividend_withholding", 0.15))
        self.ppf_tax = float(get(self.cost_cfg, "taxes.fund_withholding_default", 0.175))
        self._prepared = False

    # ------------------------------------------------------------------ #
    def prepare(self) -> None:
        bd = self.store.bist_daily()
        self.close = pivot_close(bd, "close").ffill(limit=5)
        self.high = pivot_close(bd, "high").reindex_like(self.close)
        self.low = pivot_close(bd, "low").reindex_like(self.close)
        self.raw = pivot_close(bd, "close_raw").reindex_like(self.close) if "close_raw" in bd else self.close
        self.vol = pivot_close(bd, "volume").reindex_like(self.close).fillna(0)
        if "dividend" in bd:
            div = pivot_close(bd, "dividend").reindex_like(self.close).fillna(0)
            self.div_yield = (div / self.raw).replace([np.inf, -np.inf], 0).fillna(0)
        else:
            self.div_yield = self.close * 0
        self.split = pivot_close(bd, "split").reindex_like(self.close).fillna(0) if "split" in bd else None
        tr = pd.concat([self.high - self.low, (self.high - self.close.shift()).abs(),
                        (self.low - self.close.shift()).abs()]).groupby(level=0).max()
        self.atr20 = tr.reindex_like(self.close).ewm(alpha=1 / 20, adjust=False, min_periods=20).mean()
        self.sma20 = self.close.rolling(20).mean()
        self.sma50 = self.close.rolling(50).mean()
        self.sma200 = self.close.rolling(200).mean()
        self.rsi14 = self.close.apply(lambda s: rsi(s, 14))
        self.adv20 = (self.raw * self.vol).rolling(20, min_periods=15).mean()

        ix = self.store.index_daily()
        self.xu100 = ix[ix["symbol"] == "XU100"].set_index("date")["close"].sort_index()
        g = self.store.global_daily()
        self.g = g.pivot_table(index="date", columns="symbol", values="close").sort_index() if not g.empty else pd.DataFrame()
        self.usdtry = self.g["USDTRY"] if "USDTRY" in self.g else pd.Series(dtype=float)

        meta = self.store.meta()
        self.sectors = meta.set_index("symbol")["sector"] if not meta.empty and "sector" in meta else None
        self.tidy = F.tidy_fundamentals(self.store.fundamentals(), self.store.earnings_dates())

        inf = self.store.inflation()
        if not inf.empty and "ufe_mom" in inf:
            self.ufe_index = (1 + inf["ufe_mom"].fillna(0) / 100).cumprod()
        else:
            self.ufe_index = pd.Series(dtype=float)

        days = self.close.index
        days = days[(days >= self.start) & ((days <= self.end) if self.end is not None else True)]
        self.days = days
        self.ppf = self.store.ppf_daily_returns(days, net_tax=self.ppf_tax)
        self.ppf_full = self.store.ppf_daily_returns(self.close.index, net_tax=self.ppf_tax)
        self.policy = self.store.policy_rate()
        # global seriler işlem günlerine hizalı (ileri doldurma)
        all_idx = self.g.index.union(self.close.index) if not self.g.empty else self.close.index
        self.gd = self.g.reindex(all_idx).ffill().reindex(self.close.index) if not self.g.empty else pd.DataFrame()
        self.fx = self.gd["USDTRY"] if "USDTRY" in self.gd else pd.Series(1.0, index=self.close.index)
        self.xu = self.xu100.reindex(self.xu100.index.union(self.close.index)).ffill().reindex(self.close.index)
        self.universe = [s for s in self.close.columns]
        self._prepared = True

    # ------------------------------------------------------------------ #
    def _ufe_ratio(self, buy: pd.Timestamp, sell: pd.Timestamp) -> float:
        """Alış ayı dahil, satış ayı hariç Yİ-ÜFE artışı (aylık endeks)."""
        if self.ufe_index.empty:
            return 1.0
        a = (buy.to_period("M") - 1).to_timestamp()
        b = (sell.to_period("M") - 1).to_timestamp()
        ia = self.ufe_index.reindex(self.ufe_index.index.union([a])).ffill().get(a, np.nan)
        ib = self.ufe_index.reindex(self.ufe_index.index.union([b])).ffill().get(b, np.nan)
        if pd.isna(ia) or pd.isna(ib) or ia <= 0:
            return 1.0
        return float(ib / ia)

    def run(self, cfg: dict | None = None) -> dict:
        if not self._prepared:
            self.prepare()
        cfg = cfg or self.cfg
        C = self.close
        days = list(self.days)
        if not days:
            raise RuntimeError("tarih aralığında veri yok")
        week_end = pd.Series(days, index=days).groupby(
            [pd.Index(days).isocalendar().year.values, pd.Index(days).isocalendar().week.values]).transform("max")
        is_week_end = {d: (week_end[d] == d) for d in days}

        w_fac = dict(get(cfg, "factors.weights"))
        w_fac["analyst"] = 0.0     # backtestte yok (point-in-time veri yok + LLM look-ahead)
        w_fac["news"] = 0.0
        cand_pct = get(cfg, "entry.candidate_percentile", 0.2)
        sell_pct = get(cfg, "exit.sell_percentile", 0.5)
        tr_sizes = [t["size"] for t in get(cfg, "entry.tranches")]
        min_up = get(cfg, "valuation.min_upside_to_buy", 0.15)
        trail_avg = get(cfg, "exit.trailing_weekly.from_avg_cost_atr", 2.5)
        trail_peak = get(cfg, "exit.trailing_weekly.from_peak_atr", 3.0)
        cat_stop = get(cfg, "exit.catastrophe_stop", -0.15)
        max_single = get(cfg, "sizing.max_single_bist", 0.10)
        max_sector = get(cfg, "sizing.max_sector_bist", 0.25)
        max_pos = get(cfg, "sizing.bist_positions", [8, 15])[1]
        regime_on = get(cfg, "regime_bist.enabled", True)
        weights_by_score = get(cfg, "regime_bist.weights_by_score")
        fixed_bist = get(cfg, "regime_bist.fixed_weight", 0.60)
        crypto_trend = get(cfg, "crypto.trend_enabled", True)
        transition = max(int(get(cfg, "schedule.allocation_transition_weeks", 3)), 1)
        min_adv = get(cfg, "universe_bist.add_bist_all_if_adv20_try_above", 5e7)

        cash = self.E0
        usd_cash = 0.0
        bist: dict[str, BistPos] = {}
        foreign: dict[str, list[Lot]] = {}
        crypto: dict[str, list[Lot]] = {}
        pending: list[dict] = []
        trades: list[dict] = []
        eq_rows: list[tuple] = []
        weekly_log: list[dict] = []
        realized_foreign: dict[int, float] = {}
        tax_paid = 0.0
        cur_t = {"bist": 0.0, "foreign": 0.0, "crypto": 0.0}
        last_scores: pd.DataFrame = pd.DataFrame()
        watch: set[str] = set()

        def fxp(d):
            v = self.fx.get(d, np.nan)
            return float(v) if pd.notna(v) else 1.0

        def gpx(sym, d):
            if sym not in self.gd:
                return np.nan
            return float(self.gd.at[d, sym]) if d in self.gd.index else np.nan

        def lots_value_usd(book, d):
            tot = 0.0
            for s, lots in book.items():
                p = gpx(s, d)
                if pd.notna(p):
                    tot += sum(l.qty for l in lots) * p
            return tot

        def bist_value(d):
            v = 0.0
            for s, p in bist.items():
                px = C.at[d, s]
                v += p.qty * (px if pd.notna(px) else p.avg)
            return v

        def equity(d):
            return cash + bist_value(d) + (usd_cash + lots_value_usd(foreign, d) + lots_value_usd(crypto, d)) * fxp(d)

        def sell_lots(book, sym, qty, px_usd, d, fee_usd, sleeve):
            nonlocal usd_cash
            lots = book.get(sym, [])
            remaining = qty
            gain_tl = 0.0
            fx_now = fxp(d)
            proceeds_total = qty * px_usd
            while remaining > 1e-12 and lots:
                lot = lots[0]
                q = min(lot.qty, remaining)
                cost_tl = q * lot.usd_px * lot.fx
                ratio = self._ufe_ratio(lot.date, d)
                if ratio - 1 >= self.idx_thr:
                    cost_tl *= ratio
                fee_share = fee_usd * (q / qty) if qty > 0 else 0
                gain_tl += (q * px_usd - fee_share) * fx_now - cost_tl
                lot.qty -= q
                remaining -= q
                if lot.qty <= 1e-12:
                    lots.pop(0)
            if not lots:
                book.pop(sym, None)
            usd_cash += proceeds_total - fee_usd
            if sleeve == "foreign":
                realized_foreign[d.year] = realized_foreign.get(d.year, 0.0) + gain_tl
            trades.append({"date": d, "sleeve": sleeve, "symbol": sym, "side": "sell", "qty": qty,
                           "price": px_usd, "fx": fx_now, "value_try": qty * px_usd * fx_now,
                           "fee_try": fee_usd * fx_now, "gain_try": gain_tl, "reason": ""})

        def buy_lots(book, sym, usd_amount, px_usd, d, sleeve):
            nonlocal usd_cash, cash
            if sleeve == "crypto":
                fee = usd_amount * self.fc.crypto_rate
            else:
                fee = self.fc.us_commission_usd
            need = usd_amount + fee
            if usd_cash < need:
                short = need - usd_cash
                tl = short * fxp(d)
                conv = self.fc.fx_buy_usd_cost(tl)
                cash -= tl + conv
                usd_cash += short
                trades.append({"date": d, "sleeve": "fx", "symbol": "USDTRY", "side": "buy", "qty": short,
                               "price": fxp(d), "fx": fxp(d), "value_try": tl, "fee_try": conv, "gain_try": 0.0,
                               "reason": "TL→USD"})
            q = usd_amount / px_usd
            usd_cash -= need
            book.setdefault(sym, []).append(Lot(q, px_usd, fxp(d), d))
            trades.append({"date": d, "sleeve": sleeve, "symbol": sym, "side": "buy", "qty": q, "price": px_usd,
                           "fx": fxp(d), "value_try": usd_amount * fxp(d), "fee_try": fee * fxp(d),
                           "gain_try": 0.0, "reason": ""})

        prev_d = None
        for k, d in enumerate(days):
            # 1) PPF faizi (önceki günden kalan nakit), temettüler
            if prev_d is not None:
                cash *= 1 + float(self.ppf.get(d, 0.0))
            for s, p in bist.items():
                y = self.div_yield.at[d, s] if s in self.div_yield.columns else 0.0
                if y and y > 0 and prev_d is not None:
                    pc = C.at[prev_d, s]
                    cash += p.qty * pc * y * (1 - self.div_tax)

            # 2) bekleyen emirler (bugünün kapanışından)
            todo, pending = pending, []
            for o in todo:
                s = o["symbol"]
                if o["sleeve"] == "bist":
                    px = C.at[d, s] if s in C.columns else np.nan
                    if pd.isna(px) or px <= 0:
                        continue
                    if o["side"] == "buy":
                        val = min(o["value"], max(cash, 0))
                        if val < 1000:
                            continue
                        fill = px * (1 + self.bist_cost)
                        q = math.floor(val / fill)
                        if q <= 0:
                            continue
                        cash -= q * fill
                        if s in bist:
                            bp = bist[s]
                            bp.qty += q
                            bp.cost += q * fill
                            if o.get("tranche"):
                                bp.tranches[o["tranche"]] = "done"
                        else:
                            bist[s] = BistPos(q, q * fill, px, d, px, o["target"],
                                              t1_sue_period=o.get("sue_period"))
                        trades.append({"date": d, "sleeve": "bist", "symbol": s, "side": "buy", "qty": q,
                                       "price": fill, "fx": 1.0, "value_try": q * fill,
                                       "fee_try": q * px * self.bist_cost, "gain_try": 0.0,
                                       "reason": o.get("tranche", "")})
                    else:
                        if s not in bist:
                            continue
                        bp = bist[s]
                        q = bp.qty if o.get("all") else min(bp.qty, math.floor(bp.qty * o.get("frac", 1.0)))
                        if q <= 0:
                            continue
                        fill = px * (1 - self.bist_cost)
                        avg = bp.avg
                        cash += q * fill
                        bp.cost -= q * avg
                        bp.qty -= q
                        trades.append({"date": d, "sleeve": "bist", "symbol": s, "side": "sell", "qty": q,
                                       "price": fill, "fx": 1.0, "value_try": q * fill,
                                       "fee_try": q * px * self.bist_cost, "gain_try": q * (fill - avg),
                                       "reason": o.get("reason", "")})
                        if bp.qty <= 0:
                            del bist[s]
                        elif o.get("reason") == "değerleme_kâr_al":
                            bp.trimmed = True
                elif o["sleeve"] in ("foreign", "crypto"):
                    book = foreign if o["sleeve"] == "foreign" else crypto
                    px = gpx(s, d)
                    if pd.isna(px) or px <= 0:
                        continue
                    if o["side"] == "buy":
                        buy_lots(book, s, o["usd"], px, d, o["sleeve"])
                    else:
                        held = sum(l.qty for l in book.get(s, []))
                        q = min(held, o["usd"] / px) if not o.get("all") else held
                        if q <= 0:
                            continue
                        fee = self.fc.us_commission_usd if o["sleeve"] == "foreign" else q * px * self.fc.crypto_rate
                        sell_lots(book, s, q, px, d, fee, o["sleeve"])
                elif o["sleeve"] == "fx_to_try":
                    amt = min(o["usd"], usd_cash)
                    if amt > 0:
                        tl = amt * fxp(d)
                        fee = self.fc.fx_sell_usd_cost(tl)
                        usd_cash -= amt
                        cash += tl - fee
                        trades.append({"date": d, "sleeve": "fx", "symbol": "USDTRY", "side": "sell", "qty": amt,
                                       "price": fxp(d), "fx": fxp(d), "value_try": tl, "fee_try": fee,
                                       "gain_try": 0.0, "reason": "USD→TL"})

            # 3) güncelle
            for s, p in bist.items():
                px = C.at[d, s]
                if pd.notna(px):
                    p.peak = max(p.peak, px)
            E = equity(d)

            # 4) yıl sonu vergisi
            nxt = days[k + 1] if k + 1 < len(days) else None
            if nxt is None or nxt.year != d.year:
                g = realized_foreign.get(d.year, 0.0)
                tax = max(g, 0.0) * self.tax_rate
                if tax > 0:
                    cash -= tax
                    tax_paid += tax
                    trades.append({"date": d, "sleeve": "tax", "symbol": "GELİR VERGİSİ", "side": "pay", "qty": 0,
                                   "price": 0, "fx": 1.0, "value_try": tax, "fee_try": 0.0, "gain_try": -tax,
                                   "reason": f"{d.year} yurt dışı kazanç"})
                E = equity(d)

            bv, fv_usd, cv_usd = bist_value(d), lots_value_usd(foreign, d), lots_value_usd(crypto, d)
            eq_rows.append((d, E, cash, bv, (fv_usd + usd_cash) * fxp(d), cv_usd * fxp(d), tax_paid))
            prev_d = d
            if nxt is None:
                break

            # 5) günlük kontroller → yarın kapanışta uygulanacak emirler
            self._daily_checks(d, bist, pending, last_scores, cfg, tr_sizes, cat_stop)

            if not is_week_end[d]:
                continue

            # ================= HAFTALIK =================
            # -- skorlar
            liq = self.adv20.loc[d]
            univ = [s for s in self.universe if pd.notna(C.at[d, s]) and liq.get(s, 0) >= min_adv]
            scores = F.compute_scores(d, C, self.raw, self.xu, self.tidy, self.sectors, w_fac, univ)
            rg = self.regime_weights(d, cfg, scores)
            R, reasons = rg["R"], rg["reasons"]
            new_t = {"bist": rg["bist"], "foreign": rg["foreign"], "crypto": rg["crypto"]}
            xu = self.xu.loc[:d].dropna()
            accel = len(xu) > 6 and xu.iloc[-1] / xu.iloc[-6:].max() - 1 < -get(cfg, "regime_bist.accelerate_if_index_drawdown", 0.05)
            for key in cur_t:
                gap = new_t[key] - cur_t[key]
                step = gap if (abs(gap) <= 0.02 or (key == "bist" and gap > 0 and accel) or cur_t[key] == 0 and k < 10) else gap / transition
                cur_t[key] += step
            chosen = []
            if scores.empty:
                weekly_log.append({"date": d, "R": R, "note": "skor yok (veri yetersiz)"})
            else:
                chosen = self._weekly_bist(d, E, scores, bist, pending, cur_t, cfg, C, tr_sizes, cand_pct, sell_pct,
                                           min_up, trail_avg, trail_peak, max_single, max_sector, max_pos)
                last_scores = scores

            # -- yurt dışı kolu
            fx_now = fxp(d)
            target_usd = cur_t["foreign"] * E / fx_now
            comp = self.foreign_composition(d, cfg, target_usd)
            month_first_week = d.day <= 7
            cur_vals = {s: sum(l.qty for l in lots) * gpx(s, d) for s, lots in foreign.items()}
            cur_total = sum(cur_vals.values()) + usd_cash
            sleeve_gap = (target_usd - cur_total) * fx_now / E
            if month_first_week or abs(sleeve_gap) > 0.05 or not foreign:
                for s in set(comp) | set(cur_vals):
                    tgt = comp.get(s, 0) * target_usd
                    diff = tgt - cur_vals.get(s, 0.0)
                    if abs(diff) < self.fc.min_position_usd and not (tgt == 0 and cur_vals.get(s, 0) > 0):
                        continue
                    if diff > 0:
                        pending.append({"sleeve": "foreign", "symbol": s, "side": "buy", "usd": diff})
                    else:
                        pending.append({"sleeve": "foreign", "symbol": s, "side": "sell", "usd": -diff,
                                        "all": tgt == 0})
                excess_usd = cur_total - target_usd
                if excess_usd * fx_now / E > 0.05:
                    pending.append({"sleeve": "fx_to_try", "symbol": "USD", "side": "sell", "usd": excess_usd})

            # -- kripto kolu
            cr_vals = {s: sum(l.qty for l in lots) * gpx(s, d) for s, lots in crypto.items()}
            cr_total = sum(cr_vals.values()) * fx_now / E
            band = get(cfg, "crypto.rebalance_band", 0.02)
            tgt_cr = cur_t["crypto"]
            if cr_total > get(cfg, "crypto.hard_cap", 0.10):
                tgt_cr = min(tgt_cr, get(cfg, "crypto.trim_to", 0.08))
            if abs(cr_total - tgt_cr) > band or (tgt_cr == 0 and cr_total > 0):
                for s, w in CRYPTO.items():
                    if s not in self.gd:
                        continue
                    tgt = tgt_cr * w * E / fx_now
                    diff = tgt - cr_vals.get(s, 0.0)
                    if abs(diff) < 50:
                        continue
                    if diff > 0:
                        pending.append({"sleeve": "crypto", "symbol": s, "side": "buy", "usd": diff})
                    else:
                        pending.append({"sleeve": "crypto", "symbol": s, "side": "sell", "usd": -diff, "all": tgt == 0})

            weekly_log.append({"date": d, "R": R, "reasons": ",".join(reasons), "t_bist": cur_t["bist"],
                               "t_foreign": cur_t["foreign"], "t_crypto": cur_t["crypto"], "n_bist": len(bist),
                               "n_new": len(chosen), "equity": E,
                               "fund_cov": float(scores["ebitda_ttm"].notna().mean()) if "ebitda_ttm" in scores else 0.0})

        eq = pd.DataFrame(eq_rows, columns=["date", "equity", "cash_try", "bist", "foreign", "crypto", "tax_paid"]).set_index("date")
        bench = self.benchmarks(eq.index)
        return {"equity": eq, "trades": pd.DataFrame(trades), "weekly": pd.DataFrame(weekly_log),
                "ppf": self.ppf.reindex(eq.index).fillna(0), "bench": bench, "initial_equity": self.E0}




    def _daily_checks(self, d, bist: dict, pending: list, last_scores: pd.DataFrame, cfg: dict,
                      tr_sizes: list, cat_stop: float) -> None:
        """Felaket stopu ve T2/T3 dilim tetikleri (backtest ve canlı ortak)."""
        C = self.close
        pend_syms = {o["symbol"] for o in pending}
        for s, p in list(bist.items()):
            px = C.at[d, s]
            if pd.isna(px) or s in pend_syms:
                continue
            if px <= p.avg * (1 + cat_stop):
                pending.append({"sleeve": "bist", "symbol": s, "side": "sell", "all": True, "reason": "felaket_stopu"})
                continue
            weeks = (d - p.t1_date).days / 7
            rank = last_scores["rank_pct"].get(s, 1.0) if not last_scores.empty else 1.0
            # T2 (ucuzken)
            if p.tranches.get("T2") == "pending" and len(tr_sizes) > 1:
                if weeks > get(cfg, "entry.tranches")[1].get("valid_weeks", 6):
                    p.tranches["T2"] = "expired"
                else:
                    a = self.atr20.at[d, s]
                    cheap = (px <= self.sma50.at[d, s] - a) or (self.rsi14.at[d, s] <= 35) or (px <= p.t1_price - a)
                    above200 = pd.notna(self.sma200.at[d, s]) and px > self.sma200.at[d, s]
                    if cheap and above200 and rank <= 0.30 and px > p.avg * 0.85:
                        pending.append({"sleeve": "bist", "symbol": s, "side": "buy",
                                        "value": p.target_value * tr_sizes[1], "tranche": "T2", "target": p.target_value})
                        continue
            # T3 (teyit)
            if p.tranches.get("T3") == "pending" and len(tr_sizes) > 2:
                if weeks > get(cfg, "entry.tranches")[2].get("valid_weeks", 10):
                    p.tranches["T3"] = "expired"
                else:
                    conf = (p.tranches.get("T2") == "done" and px > self.sma20.at[d, s]) or \
                           (weeks >= 4 and rank <= 0.20)
                    if not conf and not self.tidy.empty:
                        pit = F.point_in_time(self.tidy, d)
                        if s in pit.index and p.t1_sue_period and pit.at[s, "period"] != p.t1_sue_period \
                                and pd.notna(pit.at[s, "sue"]) and pit.at[s, "sue"] > 0:
                            conf = True
                    if conf:
                        pending.append({"sleeve": "bist", "symbol": s, "side": "buy",
                                        "value": p.target_value * tr_sizes[2], "tranche": "T3", "target": p.target_value})


    def foreign_composition(self, d, cfg: dict, target_usd: float) -> dict:
        """Yurt dışı kolunun hedef bileşimi: SPY/QQQ çekirdeği + momentumlu sektör ETF uydusu."""
        comp: dict = {}
        core_share = get(cfg, "foreign.core_min_share", 0.6)
        sat = []
        moms = {}
        for etf in SECTOR_ETFS:
            if etf in self.gd:
                ser = self.gd[etf].loc[:d].dropna()
                if len(ser) > 253 and ser.iloc[-1] > ser.iloc[-200:].mean():
                    moms[etf] = ser.iloc[-22] / ser.iloc[-127] - 1
        sat = sorted(moms, key=moms.get, reverse=True)[:3]
        sat_share = (1 - core_share) if sat else 0.0
        for e, w in CORE_ETFS.items():
            if e in self.gd:
                comp[e] = w * (1 - sat_share)
        for e in sat:
            comp[e] = sat_share / len(sat)
        # küçük kolda pozisyon sayısını sınırla (her pozisyon ≥ min_position_usd)
        n_allowed = max(1, int(target_usd // self.fc.min_position_usd))
        if len(comp) > n_allowed:
            keep = ["SPY"] + sat[: n_allowed - 1] if n_allowed > 1 else ["SPY"]
            if n_allowed >= 2 and not sat:
                keep = ["SPY", "QQQ"]
            comp = {e: 1.0 / len(keep) for e in keep if e in self.gd}
        return comp

    def regime_weights(self, d, cfg: dict, scores: pd.DataFrame | None = None) -> dict:
        """Kol hedef ağırlıkları (docs/04 §3). Backtest ve canlı planlayıcı aynı fonksiyonu kullanır."""
        C = self.close
        xu = self.xu.loc[:d].dropna()
        R, reasons = 0, []
        if len(xu) > 200:
            if xu.iloc[-1] > xu.iloc[-200:].mean():
                R += 1; reasons.append("XU100>SMA200")
            fxs = self.fx.loc[:d].reindex(xu.index).ffill()
            xu_usd = (xu / fxs).dropna()
            if len(xu_usd) > 200 and xu_usd.iloc[-1] > xu_usd.iloc[-200:].mean():
                R += 1; reasons.append("XU100$>SMA200")
        if (C.loc[d] > self.sma50.loc[d]).mean() > 0.5:
            R += 1; reasons.append("genişlik>%50")
        pr = self.policy.loc[:d] if not self.policy.empty else self.policy
        if len(pr) >= 2 and pr.iloc[-1] <= pr.iloc[-2]:
            R += 1; reasons.append("TCMB indirim/sabit")
        elif len(pr) == 1:
            R += 1
        w_bist = get(cfg, "regime_bist.weights_by_score")[R] if get(cfg, "regime_bist.enabled", True) \
            else get(cfg, "regime_bist.fixed_weight", 0.60)
        # göreceli cazibe: piyasa kazanç getirisi < PPF net getirisi → -10 puan
        if scores is not None and "mcap" in scores and get(cfg, "regime_bist.enabled", True):
            ni = None
            if not self.tidy.empty:
                pit = F.point_in_time(self.tidy, d)
                if "net_income_ttm" in pit:
                    ni = pit["net_income_ttm"].reindex(scores.index)
            if ni is not None:
                m = scores["mcap"].notna() & ni.notna()
                if m.sum() >= 10 and scores.loc[m, "mcap"].sum() > 0:
                    ep = ni[m].sum() / scores.loc[m, "mcap"].sum()
                    ppf_1y = float((1 + self.ppf_full.loc[:d].iloc[-252:]).prod() - 1)
                    if ep < ppf_1y:
                        w_bist -= get(cfg, "regime_bist.relative_value_haircut", 0.10)
                        reasons.append(f"E/P {ep:.1%} < PPF {ppf_1y:.1%}")
        spx = self.gd["SPY"].loc[:d].dropna() if "SPY" in self.gd else pd.Series(dtype=float)
        w_for = get(cfg, "regime_foreign.spx_above_sma200_weight", 0.25)
        if len(spx) > 200 and spx.iloc[-1] < spx.iloc[-200:].mean():
            w_for = get(cfg, "regime_foreign.spx_below_sma200_weight", 0.10)
        fx = self.fx.loc[:d].dropna()
        if len(fx) > 64 and len(pr):
            fx3 = fx.iloc[-1] / fx.iloc[-64] - 1
            if fx3 > pr.iloc[-1] / 100 / 4:
                w_for += get(cfg, "regime_foreign.fx_hedge_bonus", 0.05)
                reasons.append("kur koruması")
        w_cr = 0.0
        btc = self.gd["BTC-USD"].loc[:d].dropna() if "BTC-USD" in self.gd else pd.Series(dtype=float)
        if len(btc) > 200:
            c1 = btc.iloc[-1] > btc.iloc[-200:].mean()
            c2 = btc.iloc[-50:].mean() > btc.iloc[-200:].mean()
            if get(cfg, "crypto.trend_enabled", True):
                w_cr = get(cfg, "crypto.trend_full_weight", 0.07) if (c1 and c2) else (
                    get(cfg, "crypto.trend_half_weight", 0.04) if (c1 or c2) else 0.0)
            else:
                w_cr = get(cfg, "crypto.fixed_weight", 0.05)
        w_cr = min(w_cr, get(cfg, "crypto.hard_cap", 0.10))
        w_for = min(w_for, get(cfg, "sleeves.foreign.max", 0.40))
        w_bist = min(max(w_bist, get(cfg, "sleeves.bist.min", 0.20)), get(cfg, "sleeves.bist.max", 0.80))
        if w_bist + w_for + w_cr > 1:
            w_bist = 1 - w_for - w_cr
        return {"bist": w_bist, "foreign": w_for, "crypto": w_cr, "R": R, "reasons": reasons}

    def _weekly_bist(self, d, E, scores, bist, pending, cur_t, cfg, C, tr_sizes, cand_pct, sell_pct,
                     min_up, trail_avg, trail_peak, max_single, max_sector, max_pos):
        """Haftalık BIST çıkış/azaltım/T1 giriş kararları. Seçilen yeni hisseleri döner."""
        bist_value = lambda dd: sum(p.qty * (C.at[dd, s] if pd.notna(C.at[dd, s]) else p.avg) for s, p in bist.items())
        scores["upside"] = F.fair_value_upside(scores, self.sectors)
        cov = float(scores["ebitda_ttm"].notna().mean()) if "ebitda_ttm" in scores else 0.0
        pend_syms = {o["symbol"] for o in pending}

        # -- BIST çıkışları
        for s, p in list(bist.items()):
            if s in pend_syms:
                continue
            px = C.at[d, s]
            if s not in scores.index or pd.isna(px):
                continue
            row = scores.loc[s]
            a = self.atr20.at[d, s]
            trail = max(p.avg - trail_avg * a, p.peak - trail_peak * a)
            reason = None
            if px < trail:
                reason = "trailing_haftalık"
            elif row["rank_pct"] > sell_pct:
                reason = "skor_bozulması"
            elif pd.notna(row.get("upside")) and cov >= 0.5:
                if row["upside"] <= -0.13 and row.get("z_momentum", 0) < scores["z_momentum"].quantile(0.9):
                    reason = "değerleme_tam"
                elif row["upside"] <= 0 and not p.trimmed:
                    pending.append({"sleeve": "bist", "symbol": s, "side": "sell", "frac": get(cfg, "valuation.trim_at_fv_fraction", 0.5),
                                    "reason": "değerleme_kâr_al"})
                    continue
            weeks = (d - p.t1_date).days / 7
            if reason is None and weeks >= get(cfg, "exit.review_week", 16):
                hold_ret = px / p.t1_price - 1
                ppf_ret = float((1 + self.ppf.loc[p.t1_date:d]).prod() - 1)
                if hold_ret < ppf_ret and row["rank_pct"] > 0.30:
                    reason = "16_hafta_inceleme"
            if reason:
                if reason == "skor_bozulması" and p.exit_stage == 0:
                    p.exit_stage = 1
                    pending.append({"sleeve": "bist", "symbol": s, "side": "sell", "frac": 0.5, "reason": reason})
                else:
                    pending.append({"sleeve": "bist", "symbol": s, "side": "sell", "all": True, "reason": reason})
            elif p.exit_stage == 1 and row["rank_pct"] <= sell_pct:
                p.exit_stage = 0

        # -- kol hedefi düştüyse en zayıflardan azalt
        bv = bist_value(d)
        target_bist = cur_t["bist"] * E
        if bv > target_bist * 1.05:
            excess = bv - target_bist
            for s in scores.reindex(list(bist)).sort_values("score").index:
                if excess <= 0 or s in pend_syms:
                    continue
                v = bist[s].qty * C.at[d, s]
                pending.append({"sleeve": "bist", "symbol": s, "side": "sell", "all": True, "reason": "rejim_azaltım"})
                excess -= v

        # -- BIST girişleri (T1)
        selling = {o["symbol"] for o in pending if o["side"] == "sell"}
        held = [s for s in bist if s not in selling]
        room_value = target_bist - sum(bist[s].qty * C.at[d, s] for s in held)
        cands = scores[(scores["rank_pct"] <= cand_pct)]
        if cov >= 0.5 and "upside" in scores:
            cands = cands[(cands["upside"].isna()) | (cands["upside"] >= min_up)]
        cands = cands[~cands.index.isin(list(bist))]
        new_cands = list(cands.sort_values("score", ascending=False).index)
        slots = max_pos - len(held)
        chosen = []
        sec_expo: dict = {}
        if self.sectors is not None:
            for s in held:
                sec = self.sectors.get(s, "NA")
                sec_expo[sec] = sec_expo.get(sec, 0) + bist[s].qty * C.at[d, s] / E
        for s in new_cands:
            if len(chosen) >= slots or room_value <= 0:
                break
            px = C.at[d, s]
            if not (self.rsi14.at[d, s] < get(cfg, "entry.tranches")[0].get("max_rsi", 70) and
                    px < get(cfg, "entry.tranches")[0].get("max_ext_over_sma50", 1.15) * self.sma50.at[d, s]):
                continue
            chosen.append(s)
        if chosen:
            group = held + chosen
            sub = scores.reindex(group)
            raw_w = sub["score"].clip(lower=0.05) / sub["vol60"].clip(lower=0.15)
            raw_w = raw_w / raw_w.sum() * cur_t["bist"]
            raw_w = raw_w.clip(upper=max_single)
            for s in chosen:
                tv = float(raw_w.get(s, 0)) * E
                sec = self.sectors.get(s, "NA") if self.sectors is not None else "NA"
                if sec_expo.get(sec, 0) + tv / E > max_sector:
                    continue
                first = tv * tr_sizes[0]
                if first > room_value:
                    first = room_value
                if first < 2000:
                    continue
                sue_p = None
                if not self.tidy.empty:
                    pit = F.point_in_time(self.tidy, d)
                    if s in pit.index:
                        sue_p = pit.at[s, "period"]
                pending.append({"sleeve": "bist", "symbol": s, "side": "buy", "value": first,
                                "tranche": "T1", "target": tv, "sue_period": sue_p})
                room_value -= first
                sec_expo[sec] = sec_expo.get(sec, 0) + tv / E

        return chosen

    def benchmarks(self, idx: pd.DatetimeIndex) -> pd.DataFrame:
        b = pd.DataFrame(index=idx)
        xu = self.xu.reindex(idx).ffill()
        b["XU100 (fiyat)"] = xu / xu.iloc[0] * self.E0
        ppf = self.ppf.reindex(idx).fillna(0)
        b["PPF"] = (1 + ppf).cumprod() * self.E0
        b["%60 XU100 / %40 PPF"] = (1 + 0.6 * xu.pct_change().fillna(0) + 0.4 * ppf).cumprod() * self.E0
        fx = self.fx.reindex(idx).ffill()
        b["USD (USDTRY)"] = fx / fx.iloc[0] * self.E0
        return b
