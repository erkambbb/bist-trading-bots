"""Günlük Bot bar tabanlı backtest motoru.

Dolum kuralları (docs/05-backtest-plani.md §3):
- Stop-limit alış: bar yüksek ≥ tetik → dolum = max(tetik, açılış) + kayma; limit aşılırsa
  emir limitte bekler ve fiyat limitin 1 adım altına inerse dolar.
- Stop çıkış: bar düşük ≤ stop → min(stop, açılış) − kayma.
- Aynı barda hem stop hem TP görülürse önce stop (kötümser).
- Giriş barında: düşük ≤ stop ve kapanış < giriş ise stop (yarı-kötümser).
- Kapanış penceresi (17:30–17:55): bar içi fiyat, açılış→kapanış doğrusal enterpolasyonla
  yaklaşık bulunur (1 saatlik barlarda kaba yaklaşım).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import get
from ..costs import CostModel
from ..data.store import DataStore
from ..indicators import cumulative_vwap, rolling_atr_np
from . import signals as S


def _hm(s: str) -> pd.Timedelta:
    h, m = s.split(":")
    return pd.Timedelta(hours=int(h), minutes=int(m))


@dataclass
class DayBars:
    times: np.ndarray                 # bar başlangıçları (pd.Timestamp, tz-aware)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    vwap: np.ndarray
    batr: np.ndarray
    pos: dict = field(default_factory=dict)  # time -> index


@dataclass
class Position:
    symbol: str
    strategy: str
    qty: int
    entry: float
    stop: float
    init_stop: float
    tp1: float | None
    entry_time: pd.Timestamp
    atr_pct: float
    hh: float
    tp1_done: bool = False
    exit_next_open: bool = False
    hold_momentum: bool = False
    realized: float = 0.0
    costs: float = 0.0
    qty0: int = 0
    exits: list = field(default_factory=list)


@dataclass
class Pending:
    symbol: str
    qty: int
    trigger: float
    limit: float
    valid_until: pd.Timestamp
    atr_pct: float
    stop_pct: float
    structural: float
    resting: bool = False
    adv20: float = 0.0


class DailyBacktester:
    def __init__(self, store: DataStore, cfg: dict, costs: CostModel, interval: str,
                 start: str | None = None, end: str | None = None, initial_equity: float = 500_000.0,
                 symbols: list[str] | None = None, reserve_ppf_tax: float = 0.175):
        self.store, self.cfg, self.costs = store, cfg, costs
        self.interval = interval
        self.start = pd.Timestamp(start) if start else None
        self.end = pd.Timestamp(end) if end else None
        self.E0 = float(initial_equity)
        self.symbols = symbols
        self.reserve_ppf_tax = reserve_ppf_tax
        self._prepared = False

    # ------------------------------------------------------------------ #
    def prepare(self) -> None:
        intr = self.store.bist_intraday(self.interval)
        if intr.empty:
            raise RuntimeError(f"{self.interval} gün içi veri yok")
        if self.symbols:
            intr = intr[intr["symbol"].isin(self.symbols)]
        intr = intr.sort_values(["symbol", "ts"]).reset_index(drop=True)
        self.bar_minutes = int(pd.Series(intr.groupby("symbol")["ts"].diff().dt.total_seconds()
                                         .dropna()).mode().iloc[0] // 60)
        # gün içi VWAP ve bar ATR'si
        intr["vwap"] = np.nan
        intr["batr"] = np.nan
        for sym, g in intr.groupby("symbol"):
            idx = g.index
            intr.loc[idx, "batr"] = rolling_atr_np(g["high"].values, g["low"].values, g["close"].values, 14)
            for _, gd in g.groupby("date"):
                intr.loc[gd.index, "vwap"] = cumulative_vwap(gd["high"].values, gd["low"].values,
                                                             gd["close"].values, gd["volume"].values)
        self.intr = intr

        daily = self.store.bist_daily()
        self.stats = S.compute_daily_stats(daily).set_index(["date", "symbol"]).sort_index()
        self.stats_idx = {k: (r[0], r[1], r[2], r[3]) for k, r in zip(
            self.stats.index, self.stats[["close", "atr_pct", "adv20_try", "corp_action"]].itertuples(index=False))}
        self.regime_tbl = S.index_regime_table(self.store.index_daily(), self.store.global_daily())

        ix = self.store.index_intraday(self.interval)
        self.ix_by_day = {}
        if not ix.empty:
            ix = ix[ix["symbol"] == "XU100"].sort_values("ts")
            for d, g in ix.groupby("date"):
                self.ix_by_day[d] = DayBars(np.array(pd.DatetimeIndex(g["ts"])), g["open"].values, g["high"].values,
                                            g["low"].values, g["close"].values, g["volume"].values,
                                            cumulative_vwap(g["high"].values, g["low"].values,
                                                            g["close"].values, g["volume"].values),
                                            np.zeros(len(g)))

        self.days = sorted(intr["date"].unique())
        self.by_day: dict = {}
        for d, gd in intr.groupby("date"):
            m = {}
            for sym, g in gd.groupby("symbol"):
                ts = pd.DatetimeIndex(g["ts"])
                db = DayBars(np.array(ts), g["open"].values, g["high"].values, g["low"].values,
                             g["close"].values, g["volume"].values, g["vwap"].values, g["batr"].values)
                db.pos = {t: i for i, t in enumerate(ts)}
                m[sym] = db
            self.by_day[d] = m

        # açılış aralığı hacim geçmişi (RVOL için)
        self._or_cache: dict = {}
        self._prepared = True

    # ------------------------------------------------------------------ #
    def _or_minutes(self) -> int:
        m = int(get(self.cfg, "selection.opening_range_minutes", 5))
        return max(m, self.bar_minutes)

    def _or_window(self, db: DayBars) -> tuple[int, int] | None:
        """Açılış aralığı bar indeksleri [i0, i1)."""
        if len(db.times) == 0:
            return None
        times = pd.DatetimeIndex(db.times)
        day0 = times[0].normalize()
        cont = day0 + pd.Timedelta(hours=10)
        if self.bar_minutes >= 30:
            i0 = 0
        else:
            later = np.where(times >= cont)[0]
            if len(later) == 0:
                return None
            i0 = int(later[0])
        or_end = times[i0] + pd.Timedelta(minutes=self._or_minutes())
        i1 = int(np.searchsorted(times.values, or_end.to_datetime64()))
        if i1 <= i0 or i1 >= len(times):
            return None
        return i0, i1

    def _or_volume(self, d, sym) -> float | None:
        key = (d, sym)
        if key in self._or_cache:
            return self._or_cache[key]
        db = self.by_day.get(d, {}).get(sym)
        val = None
        if db is not None:
            w = self._or_window(db)
            if w:
                val = float(db.v[w[0]:w[1]].sum())
        self._or_cache[key] = val
        return val

    def _rvol(self, di: int, sym: str, or_vol: float) -> float | None:
        lb = int(get(self.cfg, "selection.rvol_lookback_days", 14))
        hist = []
        for j in range(max(0, di - lb), di):
            v = self._or_volume(self.days[j], sym)
            if v is not None and v > 0:
                hist.append(v)
        if len(hist) < max(5, lb // 2):
            return None
        return or_vol / (np.mean(hist) + 1e-9)


    def _day_or_table(self, di: int, d) -> pd.DataFrame:
        """Gün d için tüm hisselerin açılış aralığı + önceki gün istatistikleri (önbellekli)."""
        key = (d, self._or_minutes())
        cache = self.__dict__.setdefault("_or_tbl_cache", {})
        if key in cache:
            return cache[key]
        bars = self.by_day.get(d, {})
        prev_d = self.days[di - 1] if di > 0 else None
        rows = []
        for sym, db in bars.items():
            if prev_d is None or (prev_d, sym) not in self.stats_idx:
                continue
            pc, patr, padv, _ = self.stats_idx[(prev_d, sym)]
            corp = self.stats_idx.get((d, sym), (0, 0, 0, False))[3]
            if not (pc > 0) or pd.isna(patr) or pd.isna(padv):
                continue
            w = self._or_window(db)
            if not w:
                continue
            i0, i1 = w
            orv = float(db.v[i0:i1].sum())
            rv = self._rvol(di, sym, orv)
            if rv is None:
                continue
            rows.append({
                "symbol": sym, "rvol": rv, "gap": float(db.o[0] / pc - 1), "atr_pct": float(patr),
                "or_open": float(db.o[i0]), "or_close": float(db.c[i1 - 1]),
                "or_high": float(db.h[i0:i1].max()), "or_low": float(db.l[i0:i1].min()),
                "or_vwap": float(db.vwap[i1 - 1]), "news": False, "adv20": float(padv), "i1": i1,
                "or_end": pd.Timestamp(db.times[i1]), "corp_action": bool(corp),
                "tick_pct": self.costs.ticks.tick(pc) / pc,
            })
        out = pd.DataFrame(rows)
        cache[key] = out
        return out

    # ------------------------------------------------------------------ #
    @staticmethod
    def _price_at(db: DayBars, i: int, t: pd.Timestamp, bar_minutes: int) -> float:
        s = pd.Timestamp(db.times[i])
        frac = (t - s) / pd.Timedelta(minutes=bar_minutes)
        frac = min(max(frac, 0.0), 1.0)
        return float(db.o[i] + (db.c[i] - db.o[i]) * frac)

    def run(self, cfg: dict | None = None) -> dict:
        if not self._prepared:
            self.prepare()
        cfg = cfg or self.cfg
        costs = self.costs
        bm = self.bar_minutes
        days = [d for d in self.days
                if (self.start is None or d >= self.start) and (self.end is None or d <= self.end)]
        ppf = self.store.ppf_daily_returns(pd.DatetimeIndex(days), net_tax=self.reserve_ppf_tax)
        reserve_on = get(cfg, "backtest.reserve_in_ppf", True)
        reserve_frac = 1.0 - get(cfg, "risk.gross_exposure_cap", 0.80)

        equity = self.E0
        eq_rows, trades, day_logs = [], [], []
        week_start_eq, month_start_eq = equity, equity
        cur_week, cur_month = None, None
        risk_scale_week = 1.0
        paused_until_month = None
        losing_streak, skip_days = 0, 0

        entry_cutoff_cfg = get(cfg, "strategy_orb.entry_valid_until", "12:00")
        k_atr = get(cfg, "strategy_orb.stop.k_atr", 0.35)
        tp_mode = get(cfg, "strategy_orb.take_profit.mode", "partial_then_trail")
        tp1_r = get(cfg, "strategy_orb.take_profit.tp1_r", 2.0)
        tp1_frac = get(cfg, "strategy_orb.take_profit.tp1_fraction", 0.5)
        trail_mult = get(cfg, "strategy_orb.trailing.atr_multiple", 2.0)
        vwap_exit = get(cfg, "strategy_orb.trailing.exit_on_5m_close_below_vwap", True)
        mom_hold = get(cfg, "close_protocol.momentum_hold.enabled", True)
        dll = get(cfg, "risk.daily_loss_limit", -0.015)
        dpl = get(cfg, "risk.daily_profit_lock", 0.025)

        dayset = set(days)
        for di_global, d in enumerate(self.days):
            if d not in dayset:
                continue
            iso = pd.Timestamp(d).isocalendar()
            wk = (iso.year, iso.week)
            mo = (pd.Timestamp(d).year, pd.Timestamp(d).month)
            if wk != cur_week:
                if cur_week is not None:
                    risk_scale_week = 0.5 if equity / week_start_eq - 1 <= get(cfg, "risk.weekly_loss_limit", -0.04) else 1.0
                cur_week, week_start_eq = wk, equity
            if mo != cur_month:
                cur_month, month_start_eq = mo, equity
            reserve_income = equity * reserve_frac * float(ppf.get(d, 0.0)) if reserve_on else 0.0

            log = {"date": d, "mode": "", "n_cand": 0, "n_trades": 0, "note": ""}
            if paused_until_month is not None and mo <= paused_until_month:
                log["note"] = "aylık zarar limiti: duraklatıldı"
                equity += reserve_income
                eq_rows.append((d, equity, 0.0, reserve_income, 0.0))
                day_logs.append(log)
                continue
            if skip_days > 0:
                skip_days -= 1
                log["note"] = "art arda zararlı gün: paper"
                equity += reserve_income
                eq_rows.append((d, equity, 0.0, reserve_income, 0.0))
                day_logs.append(log)
                continue

            res = self._run_day(di_global, d, equity, cfg, costs, bm, entry_cutoff_cfg, k_atr, tp_mode,
                                tp1_r, tp1_frac, trail_mult, vwap_exit, mom_hold, dll, dpl,
                                risk_scale_week, log)
            day_pnl, day_cost, day_trades = res
            trades.extend(day_trades)
            equity += day_pnl + reserve_income
            eq_rows.append((d, equity, day_pnl, reserve_income, day_cost))
            day_logs.append(log)

            if day_trades:
                losing_streak = losing_streak + 1 if day_pnl < 0 else 0
                if losing_streak >= get(cfg, "risk.max_consecutive_losing_days", 5):
                    skip_days, losing_streak = 2, 0
            if equity / month_start_eq - 1 <= get(cfg, "risk.monthly_loss_limit", -0.08):
                paused_until_month = mo

        eq = pd.DataFrame(eq_rows, columns=["date", "equity", "trading_pnl", "reserve_income", "costs"]).set_index("date")
        tr = pd.DataFrame(trades)
        return {"equity": eq, "trades": tr, "days": pd.DataFrame(day_logs), "ppf": ppf,
                "bar_minutes": bm, "initial_equity": self.E0}

    # ------------------------------------------------------------------ #
    def _run_day(self, di, d, equity, cfg, costs, bm, entry_cutoff_cfg, k_atr, tp_mode, tp1_r,
                 tp1_frac, trail_mult, vwap_exit, mom_hold, dll, dpl, risk_scale, log):
        bars = self.by_day.get(d, {})
        if not bars:
            return 0.0, 0.0, []
        day0 = pd.Timestamp(d).tz_localize("Europe/Istanbul")
        # seans sonu (yarım gün tespiti)
        all_end = max(pd.Timestamp(db.times[-1]) for db in bars.values()) + pd.Timedelta(minutes=bm)
        session_end = day0 + pd.Timedelta(hours=18)
        if all_end < day0 + pd.Timedelta(hours=17):
            session_end = all_end
        win_start = session_end - pd.Timedelta(minutes=get(cfg, "session.close_window_start_offset_min", 30))
        final_exit = session_end - pd.Timedelta(minutes=get(cfg, "close_protocol.final_exit_time_offset_min", 10))
        cancel_t = win_start - pd.Timedelta(minutes=5)
        entry_cutoff = day0 + _hm(entry_cutoff_cfg)
        new_entry_cutoff = day0 + _hm(get(cfg, "session.entry_cutoff", "16:00"))

        # --- rejim ---
        rrow = self.regime_tbl.loc[d] if d in self.regime_tbl.index else None
        ixb = self.ix_by_day.get(d)
        xu_gap = None
        if ixb is not None and rrow is not None and pd.notna(rrow.get("ix_close")):
            xu_gap = float(ixb.o[0] / rrow["ix_close"] - 1)
        regime = S.decide_regime(rrow, xu_gap, cfg)
        log["mode"] = regime.mode
        cautious = regime.mode == "cautious"

        # --- evren + açılış aralığı tablosu ---
        prev_d = self.days[di - 1] if di > 0 else None
        base = self._day_or_table(di, d)
        if base.empty:
            or_tbl, or_end_time = base, None
        else:
            lo, hi = get(cfg, "universe.atr_pct_range", [0.02, 0.07])
            m = (~base["corp_action"]) & (base["tick_pct"] <= get(cfg, "universe.max_tick_to_price", 0.0008)) \
                & (base["adv20"] >= get(cfg, "universe.min_adv20_try", 3e8)) \
                & (base["atr_pct"] >= lo) & (base["atr_pct"] <= hi)
            or_tbl = base[m].reset_index(drop=True)
            or_end_time = or_tbl["or_end"].min() if not or_tbl.empty else None
        seed = get(cfg, "selection.random_seed")
        if seed is not None and not or_tbl.empty and regime.mode != "off":
            # karşılaştırma tabanı: aynı evrenden rastgele aday (RVOL/mum filtresi yok)
            n = get(cfg, "selection.max_candidates.cautious" if regime.mode == "cautious" else "selection.max_candidates.normal", 4)
            k = int(np.random.default_rng(int(seed) * 100003 + di).integers(0, n + 1))
            cands = or_tbl.sample(min(k, len(or_tbl)), random_state=int(seed) * 7919 + di)
        else:
            cands = S.select_candidates(or_tbl, regime, cfg) if not or_tbl.empty else or_tbl
        log["n_cand"] = len(cands)

        # gün içi endeks filtresi (seçim anı)
        def index_ok(t_idx_time, sym_ret):
            if not get(cfg, "regime.intraday_index_vwap_filter", True) or ixb is None:
                return True
            times = pd.DatetimeIndex(ixb.times)
            k = int(np.searchsorted(times.values, t_idx_time.to_datetime64())) - 1
            if k < 0:
                return True
            if ixb.c[k] >= ixb.vwap[k]:
                return True
            ix_ret = ixb.c[k] / ixb.o[0] - 1
            return (sym_ret - ix_ret) > get(cfg, "regime.rs_vs_index_if_below_vwap", 0.015)

        pend: list[Pending] = []
        pb_on = bool(get(cfg, "strategy_vwap_pullback.enabled", False)) and bm <= 15
        pbw = get(cfg, "strategy_vwap_pullback.window", ["11:00", "16:00"])
        pb_start, pb_end = day0 + _hm(pbw[0]), day0 + _hm(pbw[1])
        for r in cands.itertuples():
            tick = costs.ticks.tick(r.or_high)
            trigger = costs.ticks.round(r.or_high + tick, "buy")
            sym_ret = r.or_close / r.or_open - 1
            if or_end_time is not None and not index_ok(or_end_time, sym_ret):
                continue
            struct = r.or_low - costs.ticks.tick(r.or_low)
            dist = S.stop_distance(trigger, r.atr_pct, cfg, costs, struct)
            if dist is None:
                continue
            pend.append(Pending(r.symbol, 0, trigger,
                                costs.ticks.round(trigger + 2 * tick, "buy"),
                                entry_cutoff, r.atr_pct, dist, struct, adv20=float(r.adv20)))
        if regime.mode == "off":
            log["note"] = "; ".join(regime.reasons)

        # --- bar döngüsü ---
        positions: dict[str, Position] = {}
        closed: list[dict] = []
        realized, day_costs = 0.0, 0.0
        halted, locked = False, False
        entries_today: dict[str, int] = {}
        all_times = sorted({pd.Timestamp(t) for db in bars.values() for t in db.times})
        start_t = or_end_time if or_end_time is not None else (all_times[0] if all_times else None)

        def open_notional():
            return sum(p.qty * p.entry for p in positions.values()) + sum(
                p.qty * p.trigger for p in pend if p.qty > 0)

        def close_pos(p: Position, price: float, t, reason: str, qty: int | None = None, kind="market"):
            nonlocal realized, day_costs
            q = p.qty if qty is None else qty
            fill = costs.fill(price, "sell", kind) if kind != "limit" else price
            fill = max(fill, costs.ticks.tick(fill))
            com = costs.commission(q * fill)
            pnl = q * (fill - p.entry) - com
            p.realized += pnl
            p.costs += com
            realized += pnl
            day_costs += com
            p.exits.append((t, q, fill, reason))
            p.qty -= q
            if p.qty <= 0:
                risk_per_share = p.entry - p.init_stop
                closed.append({
                    "date": d, "symbol": p.symbol, "strategy": p.strategy, "entry_time": p.entry_time,
                    "exit_time": t, "qty": p.qty0, "entry": p.entry,
                    "exit": sum(x[1] * x[2] for x in p.exits) / max(sum(x[1] for x in p.exits), 1),
                    "stop": p.init_stop, "pnl": p.realized, "costs": p.costs,
                    "R": p.realized / (p.qty0 * risk_per_share) if risk_per_share > 0 else np.nan,
                    "exit_reason": p.exits[-1][3], "regime": regime.mode,
                })

        for t in all_times:
            if start_t is None or t < start_t:
                continue
            t_end = t + pd.Timedelta(minutes=bm)
            in_window = t_end > win_start
            # ---- pozisyon yönetimi ----
            for sym in list(positions):
                p = positions[sym]
                db = bars[sym]
                i = db.pos.get(t)
                if i is None:
                    continue
                o, h, l, c = db.o[i], db.h[i], db.l[i], db.c[i]
                if p.exit_next_open:
                    close_pos(p, o, t, "vwap_kırılımı")
                    del positions[sym]
                    continue
                # kapanış penceresi
                if in_window:
                    if t < win_start and l <= p.stop:
                        close_pos(p, min(p.stop, o), t, "stop", kind="stop")
                        del positions[sym]
                        continue
                    if not p.hold_momentum or not mom_hold:
                        ref = self._price_at(db, i, max(win_start, t), bm)
                        close_pos(p, ref, max(win_start, t), "kapanış_17:30")
                        del positions[sym]
                        continue
                    # güçlü pozisyon: 17:50'ye kadar sıkı trailing
                    tight = p.hh - get(cfg, "close_protocol.momentum_hold.tighten_trailing_atr", 1.0) * db.batr[i]
                    p.stop = max(p.stop, tight)
                    if l <= p.stop and t < final_exit:
                        close_pos(p, min(p.stop, o), t, "trailing_kapanış", kind="stop")
                        del positions[sym]
                        continue
                    if t_end > final_exit:
                        ref = self._price_at(db, i, max(final_exit, t), bm)
                        close_pos(p, ref, max(final_exit, t), "kapanış_17:50")
                        del positions[sym]
                        continue
                    p.hh = max(p.hh, h)
                    continue
                # normal yönetim
                if l <= p.stop:
                    close_pos(p, min(p.stop, o), t, "stop" if not p.tp1_done else "trailing", kind="stop")
                    del positions[sym]
                    continue
                if (p.tp1 is not None and not p.tp1_done and
                        h >= p.tp1 + costs.ticks.tick(p.tp1)):
                    q = int(math.floor(p.qty0 * tp1_frac))
                    if q > 0:
                        close_pos(p, p.tp1, t, "tp1", qty=q, kind="limit")
                    p.tp1_done = True
                    be = p.entry * (1 + costs.roundtrip_estimate(p.entry))
                    if get(cfg, "strategy_orb.take_profit.move_stop_to_breakeven", True):
                        p.stop = max(p.stop, costs.ticks.round(be, "buy"))
                    if p.qty <= 0:
                        del positions[sym]
                        continue
                p.hh = max(p.hh, h)
                trailing_active = (tp_mode == "trail_only") or (tp_mode == "partial_then_trail" and p.tp1_done)
                if trailing_active:
                    mult = 1.0 if locked else trail_mult
                    p.stop = max(p.stop, p.hh - mult * db.batr[i])
                    if vwap_exit and c < db.vwap[i]:
                        p.exit_next_open = True
                elif locked:
                    p.stop = max(p.stop, p.hh - db.batr[i])

            # ---- bekleyen girişler ----
            if not halted and not locked and t < cancel_t and t < new_entry_cutoff:
                for pe in list(pend):
                    if t >= pe.valid_until:
                        pend.remove(pe)
                        continue
                    db = bars[pe.symbol]
                    i = db.pos.get(t)
                    if i is None:
                        continue
                    if entries_today.get(pe.symbol, 0) >= get(cfg, "strategy_orb.max_entries_per_symbol", 2):
                        pend.remove(pe)
                        continue
                    if pe.symbol in positions:
                        continue
                    o, h, l, c = db.o[i], db.h[i], db.l[i], db.c[i]
                    fill = None
                    if pe.resting:
                        if l <= pe.limit - costs.ticks.tick(pe.limit):
                            fill = pe.limit
                    elif h >= pe.trigger:
                        ref = max(pe.trigger, o)
                        f = costs.fill(ref, "buy", "stop")
                        if f <= pe.limit:
                            fill = f
                        else:
                            pe.resting = True
                            if l <= pe.limit - costs.ticks.tick(pe.limit) and o <= pe.limit:
                                fill = pe.limit
                    if fill is None:
                        continue
                    stop = costs.ticks.round(fill * (1 - pe.stop_pct), "sell")
                    if k_atr == "structural":
                        stop = min(stop, pe.structural)
                    adv = pe.adv20
                    q = S.position_qty(equity + realized, fill, stop, adv,
                                       sum(pp.qty * pp.entry for pp in positions.values()),
                                       cfg, cautious, risk_scale)
                    pend.remove(pe)
                    if q <= 0:
                        continue
                    com = costs.commission(q * fill)
                    realized -= com
                    day_costs += com
                    risk = fill - stop
                    tp1 = None if tp_mode in ("trail_only", "eod_only") else costs.ticks.round(fill + tp1_r * risk, "buy")
                    p = Position(pe.symbol, "orb", q, fill, stop, stop, tp1, t, pe.atr_pct, max(h, fill),
                                 costs=com, qty0=q, realized=-com)
                    positions[pe.symbol] = p
                    entries_today[pe.symbol] = entries_today.get(pe.symbol, 0) + 1
                    # giriş barında yarı-kötümser stop kontrolü
                    if l <= stop and c < fill:
                        close_pos(p, stop, t, "stop", kind="stop")
                        del positions[pe.symbol]

            # ---- Strateji B: VWAP geri çekilme (yalnızca ≤15 dk barlarda) ----
            if (pb_on and not halted and not locked and t < cancel_t and t < new_entry_cutoff
                    and pb_start <= t < pb_end and not or_tbl.empty):
                cap_n = get(cfg, "selection.max_candidates.cautious" if cautious else "selection.max_candidates.normal", 4)
                for r in or_tbl.itertuples():
                    if len(positions) + len([x for x in pend]) >= cap_n:
                        break
                    sym = r.symbol
                    if sym in positions or entries_today.get(sym, 0) >= get(cfg, "strategy_orb.max_entries_per_symbol", 2):
                        continue
                    db = bars[sym]
                    i = db.pos.get(t)
                    if i is None or i < 4:
                        continue
                    need = max(int(get(cfg, "strategy_vwap_pullback.min_minutes_above_vwap", 60) / bm), 2)
                    j0 = max(0, i - need)
                    if not np.all(db.c[j0:i] > db.vwap[j0:i]):
                        continue
                    rs = db.c[i] / db.o[0] - 1
                    if ixb is not None:
                        kx = int(np.searchsorted(pd.DatetimeIndex(ixb.times).values, t.to_datetime64()))
                        kx = min(max(kx, 0), len(ixb.c) - 1)
                        rs -= ixb.c[kx] / ixb.o[0] - 1
                    if rs < get(cfg, "strategy_vwap_pullback.min_rs_vs_index", 0.01):
                        continue
                    ema20 = pd.Series(db.c[: i + 1]).ewm(span=20, adjust=False).mean().iloc[-1]
                    touch = db.l[i] <= max(db.vwap[i], ema20) * 1.001
                    vol_ok = db.v[i - 1] <= np.mean(db.v[max(0, i - 20):i]) * get(cfg, "strategy_vwap_pullback.pullback_volume_ratio_max", 1.0)
                    trig = db.c[i] > db.h[i - 1] and db.c[i] > db.o[i]
                    if not (touch and vol_ok and trig):
                        continue
                    nxt = i + 1
                    if nxt >= len(db.times):
                        continue
                    fill = costs.fill(float(db.o[nxt]), "buy", "market")
                    swing = float(db.l[max(0, i - 3): i + 1].min()) - costs.ticks.tick(fill)
                    d_min = S.stop_distance(fill, 0.0, cfg, costs) or 0.02
                    stop = min(swing, fill * (1 - d_min))
                    if (fill - stop) / fill > get(cfg, "strategy_orb.stop.max_pct", 0.02):
                        continue
                    stop = costs.ticks.round(stop, "sell")
                    q = S.position_qty(equity + realized, fill, stop, float(r.adv20),
                                       sum(pp.qty * pp.entry for pp in positions.values()), cfg, cautious, risk_scale)
                    if q <= 0:
                        continue
                    com = costs.commission(q * fill)
                    realized -= com
                    day_costs += com
                    tp1 = None if tp_mode in ("trail_only", "eod_only") else costs.ticks.round(fill + tp1_r * (fill - stop), "buy")
                    t_fill = pd.Timestamp(db.times[nxt])
                    positions[sym] = Position(sym, "vwap_pb", q, fill, stop, stop, tp1, t_fill, r.atr_pct,
                                              fill, costs=com, qty0=q, realized=-com)
                    entries_today[sym] = entries_today.get(sym, 0) + 1

            # ---- kapanış penceresine girerken momentum kararı ----
            if t_end > win_start - pd.Timedelta(minutes=bm) and t_end <= win_start:
                self._set_momentum(positions, bars, ixb, t, cfg, bm, win_start)

            # ---- risk limitleri ----
            unreal = 0.0
            for sym, p in positions.items():
                db = bars[sym]
                i = db.pos.get(t)
                px = db.c[i] if i is not None else p.entry
                unreal += p.qty * (px - p.entry)
            day_ret = (realized + unreal) / equity
            if not halted and day_ret <= dll:
                halted = True
                pend.clear()
                for sym in list(positions):
                    p = positions[sym]
                    db = bars[sym]
                    i = db.pos.get(t)
                    close_pos(p, db.c[i] if i is not None else p.entry, t_end, "günlük_zarar_limiti")
                    del positions[sym]
            elif not locked and day_ret >= dpl:
                locked = True
                pend.clear()

        # gün sonu: kalan pozisyon (veri eksikliği) son fiyattan kapatılır
        for sym in list(positions):
            p = positions[sym]
            db = bars[sym]
            close_pos(p, float(db.c[-1]), pd.Timestamp(db.times[-1]), "veri_sonu")
            del positions[sym]
        log["n_trades"] = len(closed)
        return realized, day_costs, closed

    def _set_momentum(self, positions, bars, ixb, t, cfg, bm, win_start):
        """17:30 öncesi son barda: kârda + VWAP üstü + XU100 r1>0 ve r_pre>0 ise tut."""
        r1 = r_pre = 0.0
        if ixb is not None and len(ixb.times) > 1:
            times = pd.DatetimeIndex(ixb.times)
            i_first_end = int(np.searchsorted(times.values,
                                              (times[0] + pd.Timedelta(minutes=max(30, bm))).to_datetime64())) - 1
            i_first_end = max(i_first_end, 0)
            r1 = ixb.c[i_first_end] / ixb.o[0] - 1
            k = int(np.searchsorted(times.values, (win_start - pd.Timedelta(minutes=30)).to_datetime64()))
            k = min(max(k, 0), len(times) - 1)
            kk = int(np.searchsorted(times.values, t.to_datetime64()))
            kk = min(max(kk, 0), len(times) - 1)
            k = min(k, kk)
            base = ixb.o[k]
            r_pre = ixb.c[kk] / base - 1 if base > 0 else 0.0
        for sym, p in positions.items():
            db = bars[sym]
            i = db.pos.get(t)
            if i is None:
                continue
            p.hold_momentum = bool(db.c[i] > p.entry and db.c[i] > db.vwap[i] and r1 > 0 and r_pre > 0)
