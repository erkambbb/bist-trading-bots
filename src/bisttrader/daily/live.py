"""Günlük Bot canlı/paper döngüsü (docs/03-gunluk-bot.md).

Akış: 07:30 hazırlık → 10:00+OR seçim → stop-limit girişler (istemci tarafı tetik) →
dakikalık yönetim (stop/TP1/trailing/VWAP) → 17:25 iptal → 17:30–17:55 kapanış protokolü → rapor.

Kurallar backtest motoruyla aynı fonksiyonlardan gelir (signals.py). Gerçek emir için:
  mode: live  +  broker: matriks_iq  +  data.realtime_confirmed: true   (aksi halde dry-run / paper)
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .. import notify
from ..config import DATA_DIR, get
from ..costs import CostModel
from ..execution.base import Broker, Order
from ..indicators import atr, cumulative_vwap, ema, rolling_atr_np
from . import signals as S

log = logging.getLogger("daily.live")
TZ = "Europe/Istanbul"


# --------------------------------------------------------------------------- #
class TradingViewFeed:
    """borsapy TradingViewStream ile 5 dk barlar ve son fiyat.

    TradingView hesabı olmadan veri ~15 dk gecikmelidir → yalnızca paper için uygundur.
    Gerçek zamanlı için TV_USERNAME/TV_PASSWORD (BIST gerçek zamanlı aboneliği olan hesap).
    """

    def __init__(self, interval: str = "5m"):
        import os

        import borsapy as bp
        self.bp = bp
        self.interval = interval
        token = None
        if os.getenv("TV_USERNAME") and os.getenv("TV_PASSWORD"):
            auth = bp.set_tradingview_auth(username=os.getenv("TV_USERNAME"), password=os.getenv("TV_PASSWORD"))
            token = (auth or {}).get("auth_token")
        self.realtime = token is not None
        self.stream = bp.TradingViewStream(auth_token=token)
        self.stream.connect()

    def subscribe(self, symbols: list[str]) -> None:
        for s in symbols:
            self.stream.subscribe(s)
            self.stream.subscribe_chart(s, self.interval)
        self.stream.subscribe("XU100")
        self.stream.subscribe_chart("XU100", self.interval)

    def bars(self, symbol: str) -> pd.DataFrame:
        c = self.stream.get_candles(symbol, self.interval)
        if not c:
            return pd.DataFrame()
        df = pd.DataFrame(c)
        df["ts"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(TZ)
        today = pd.Timestamp.now(tz=TZ).normalize()
        return df[df["ts"] >= today].set_index("ts")[["open", "high", "low", "close", "volume"]]

    def last(self, symbol: str) -> float | None:
        q = self.stream.get_quote(symbol)
        return float(q["last"]) if q and q.get("last") else None


# --------------------------------------------------------------------------- #
@dataclass
class LivePos:
    symbol: str
    strategy: str
    qty: int
    entry: float
    stop: float
    init_stop: float
    tp1: float | None
    hh: float
    tp1_done: bool = False
    hold_momentum: bool = False
    exit_next: bool = False
    entry_time: str = ""


@dataclass
class LivePending:
    symbol: str
    trigger: float
    limit: float
    stop_pct: float
    structural: float
    atr_pct: float
    adv20: float
    valid_until: str
    order: dict | None = None


@dataclass
class DayState:
    date: str
    equity_start: float
    mode: str = "normal"
    halted: bool = False
    locked: bool = False
    positions: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    realized: float = 0.0
    trades: list = field(default_factory=list)
    entries: dict = field(default_factory=dict)


class LiveDailyBot:
    def __init__(self, cfg: dict, costs: CostModel, broker: Broker, feed, equity: float,
                 state_dir: Path | None = None):
        self.cfg, self.costs, self.broker, self.feed = cfg, costs, broker, feed
        self.equity = equity
        self.state_dir = Path(state_dir or DATA_DIR / "live")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state: DayState | None = None
        self.universe: pd.DataFrame = pd.DataFrame()
        self.or_hist: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ #
    def save(self) -> None:
        if self.state:
            p = self.state_dir / f"daily_{self.state.date}.json"
            p.write_text(json.dumps(asdict(self.state), default=str, ensure_ascii=False, indent=1))

    def premarket(self) -> None:
        """Önceki kapanışa kadarki veriyle evren, rejim ve RVOL geçmişi."""
        import borsapy as bp
        import yfinance as yf
        today = pd.Timestamp.now(tz=TZ).normalize().tz_localize(None)
        self.state = DayState(str(today.date()), self.equity)
        syms = bp.Index("XU100").component_symbols
        rows = []
        m = int(get(self.cfg, "selection.opening_range_minutes", 5))
        for s in syms:
            try:
                d = bp.Ticker(s).history(period="3mo", interval="1d")
                d.columns = [c.lower() for c in d.columns]
                d = d[d.index.tz_localize(None).normalize() < today] if d.index.tz is not None else d[d.index < today]
                if len(d) < 25:
                    continue
                a = atr(d["high"], d["low"], d["close"], 14)
                prev = pd.Series({"close": d["close"].iloc[-1], "atr_pct": a.iloc[-1] / d["close"].iloc[-1],
                                  "adv20_try": (d["close"] * d["volume"]).iloc[-20:].mean()})
                if not S.passes_universe(prev, self.cfg, self.costs):
                    continue
                intr = bp.Ticker(s).history(period="1mo", interval="5m")
                intr.columns = [c.lower() for c in intr.columns]
                vols = []
                for _, g in intr.groupby(intr.index.date):
                    if pd.Timestamp(g.index[0].date()) >= today:
                        continue
                    g = g[g.index.time >= datetime.strptime("10:00", "%H:%M").time()]
                    vols.append(float(g["volume"].iloc[: max(m // 5, 1)].sum()))
                self.or_hist[s] = vols[-14:]
                rows.append({"symbol": s, **prev.to_dict()})
            except Exception as e:  # noqa: BLE001
                log.warning("%s hazırlık hatası: %s", s, e)
        self.universe = pd.DataFrame(rows).set_index("symbol") if rows else pd.DataFrame()
        # rejim
        ix = bp.Index("XU100").history(period="6mo", interval="1d")
        ix.columns = [c.lower() for c in ix.columns]
        e20 = ema(ix["close"], 20)
        row = {"ix_close": ix["close"].iloc[-1], "ix_ema20": e20.iloc[-1], "ix_ema20_slope": e20.diff().iloc[-1]}
        try:
            v = yf.download("^VIX", period="5d", progress=False)["Close"].dropna()
            spx = yf.download("^GSPC", period="5d", progress=False)["Close"].dropna()
            row["vix"] = float(np.asarray(v)[-1])
            sp = np.asarray(spx).ravel()
            row["spx_ret"] = float(sp[-1] / sp[-2] - 1)
        except Exception:  # noqa: BLE001
            pass
        self.regime_row = pd.Series(row)
        self.feed.subscribe(list(self.universe.index))
        notify.send(f"[Günlük Bot] {self.state.date} hazırlık: evren {len(self.universe)} hisse; "
                    f"veri {'gerçek zamanlı' if getattr(self.feed, 'realtime', False) else 'GECİKMELİ (paper)'}")
        self.save()

    # ------------------------------------------------------------------ #
    def select(self) -> None:
        st = self.state
        ixb = self.feed.bars("XU100")
        xu_gap = float(ixb["open"].iloc[0] / self.regime_row["ix_close"] - 1) if len(ixb) else None
        reg = S.decide_regime(self.regime_row, xu_gap, self.cfg)
        st.mode = reg.mode
        m = int(get(self.cfg, "selection.opening_range_minutes", 5))
        rows = []
        for s, prev in self.universe.iterrows():
            b = self.feed.bars(s)
            if b.empty:
                continue
            b = b[b.index.time >= datetime.strptime("10:00", "%H:%M").time()]
            n = max(m // 5, 1)
            if len(b) < n:
                continue
            orb = b.iloc[:n]
            hist = self.or_hist.get(s, [])
            if len(hist) < 7:
                continue
            vw = cumulative_vwap(orb["high"].values, orb["low"].values, orb["close"].values, orb["volume"].values)
            rows.append({"symbol": s, "rvol": orb["volume"].sum() / (np.mean(hist) + 1e-9),
                         "gap": float(b["open"].iloc[0] / prev["close"] - 1), "atr_pct": prev["atr_pct"],
                         "or_open": orb["open"].iloc[0], "or_close": orb["close"].iloc[-1],
                         "or_high": orb["high"].max(), "or_low": orb["low"].min(), "or_vwap": vw[-1],
                         "news": False, "adv20": prev["adv20_try"]})
        cands = S.select_candidates(pd.DataFrame(rows), reg, self.cfg) if rows else pd.DataFrame()
        valid = get(self.cfg, "strategy_orb.entry_valid_until", "12:00")
        for r in cands.itertuples():
            tick = self.costs.ticks.tick(r.or_high)
            trig = self.costs.ticks.round(r.or_high + tick, "buy")
            dist = S.stop_distance(trig, r.atr_pct, self.cfg, self.costs, r.or_low - tick)
            if dist is None:
                continue
            st.pending[r.symbol] = asdict(LivePending(r.symbol, trig, self.costs.ticks.round(trig + 2 * tick, "buy"),
                                                      dist, r.or_low - tick, r.atr_pct, r.adv20, valid))
        notify.send(f"[Günlük Bot] rejim {reg.mode} ({'; '.join(reg.reasons)}). Adaylar: "
                    + (", ".join(f"{p['symbol']} tetik {p['trigger']}" for p in st.pending.values()) or "yok"))
        self.save()

    # ------------------------------------------------------------------ #
    def _gross(self) -> float:
        return sum(p["qty"] * p["entry"] for p in self.state.positions.values())

    def _exit(self, sym: str, qty: int, reason: str, price: float | None = None) -> None:
        st = self.state
        p = st.positions[sym]
        o = self.broker.place(Order(sym, "sell", qty, None if price is None else self.costs.ticks.round(price, "sell"),
                                    "market" if price is None else "limit", note=reason))
        fill = o.avg_fill or (price or self.feed.last(sym) or p["entry"])
        pnl = qty * (fill - p["entry"]) - self.costs.commission(qty * fill)
        st.realized += pnl
        p["qty"] -= qty
        st.trades.append({"symbol": sym, "qty": qty, "entry": p["entry"], "exit": fill, "pnl": pnl, "reason": reason,
                          "time": datetime.now().isoformat(timespec="seconds")})
        notify.send(f"[Günlük Bot] SAT {sym} {qty} @~{fill:.2f} ({reason}) P&L {pnl:,.0f} TL", silent=True)
        if p["qty"] <= 0:
            del st.positions[sym]

    def tick(self, now: pd.Timestamp) -> None:
        """Dakikada bir çağrılır."""
        st, cfg = self.state, self.cfg
        day0 = now.normalize()
        hm = lambda s: day0 + pd.Timedelta(hours=int(s[:2]), minutes=int(s[3:]))
        session_end = hm("18:00")
        win_start = session_end - pd.Timedelta(minutes=get(cfg, "session.close_window_start_offset_min", 30))
        final_exit = session_end - pd.Timedelta(minutes=get(cfg, "close_protocol.final_exit_time_offset_min", 10))
        aggressive = session_end - pd.Timedelta(minutes=get(cfg, "close_protocol.aggressive_exit_time_offset_min", 7))
        deadline = session_end - pd.Timedelta(minutes=get(cfg, "session.close_deadline_offset_min", 5))
        self.broker.refresh()

        # kapanış protokolü
        if now >= win_start - pd.Timedelta(minutes=5):
            st.pending.clear()
        if now >= win_start:
            for sym in list(st.positions):
                p = st.positions[sym]
                last = self.feed.last(sym) or p["entry"]
                b = self.feed.bars(sym)
                vw = cumulative_vwap(b["high"].values, b["low"].values, b["close"].values, b["volume"].values)[-1] if len(b) else last
                if not p["hold_momentum"] or now >= final_exit or last <= p["stop"]:
                    agg = now >= aggressive
                    price = None if agg else last - self.costs.ticks.tick(last)
                    self._exit(sym, p["qty"], "kapanış" if now < final_exit else "kapanış_17:50", price)
            if now >= deadline and st.positions:
                notify.send("[Günlük Bot] UYARI: 17:55'te açık pozisyon kaldı → piyasa emri")
                for sym in list(st.positions):
                    self._exit(sym, st.positions[sym]["qty"], "son_tarih", None)
            self.save()
            return

        # momentum kararı (pencereden hemen önce)
        if win_start - pd.Timedelta(minutes=5) <= now < win_start:
            ixb = self.feed.bars("XU100")
            if len(ixb) > 6:
                r1 = ixb["close"].iloc[5] / ixb["open"].iloc[0] - 1
                pre = ixb[ixb.index >= win_start - pd.Timedelta(minutes=30)]
                r_pre = pre["close"].iloc[-1] / pre["open"].iloc[0] - 1 if len(pre) else 0
                for sym, p in st.positions.items():
                    last = self.feed.last(sym) or p["entry"]
                    b = self.feed.bars(sym)
                    vw = cumulative_vwap(b["high"].values, b["low"].values, b["close"].values, b["volume"].values)[-1]
                    p["hold_momentum"] = bool(get(cfg, "close_protocol.momentum_hold.enabled", True) and last > p["entry"]
                                              and last > vw and r1 > 0 and r_pre > 0)

        # pozisyon yönetimi
        for sym in list(st.positions):
            p = st.positions[sym]
            last = self.feed.last(sym)
            if last is None:
                continue
            p["hh"] = max(p["hh"], last)
            if last <= p["stop"]:
                self._exit(sym, p["qty"], "stop" if not p["tp1_done"] else "trailing")
                continue
            if p["tp1"] and not p["tp1_done"] and last >= p["tp1"]:
                q = int(math.floor(p["qty"] * get(cfg, "strategy_orb.take_profit.tp1_fraction", 0.5)))
                if q > 0:
                    self._exit(sym, q, "tp1", p["tp1"])
                if sym in st.positions:
                    p["tp1_done"] = True
                    p["stop"] = max(p["stop"], self.costs.ticks.round(p["entry"] * (1 + self.costs.roundtrip_estimate(p["entry"])), "buy"))
            if sym in st.positions and now.minute % 5 == 0:
                b = self.feed.bars(sym)
                if len(b) >= 3:
                    a = rolling_atr_np(b["high"].values, b["low"].values, b["close"].values, 14)[-1]
                    mode = get(cfg, "strategy_orb.take_profit.mode", "partial_then_trail")
                    if mode == "trail_only" or (mode == "partial_then_trail" and p["tp1_done"]):
                        mult = 1.0 if st.locked else get(cfg, "strategy_orb.trailing.atr_multiple", 2.0)
                        p["stop"] = max(p["stop"], p["hh"] - mult * a)
                        vw = cumulative_vwap(b["high"].values, b["low"].values, b["close"].values, b["volume"].values)[-1]
                        if get(cfg, "strategy_orb.trailing.exit_on_5m_close_below_vwap", True) and b["close"].iloc[-1] < vw:
                            self._exit(sym, p["qty"], "vwap_kırılımı")

        # girişler
        if not st.halted and not st.locked and now < hm(get(cfg, "session.entry_cutoff", "16:00")):
            for sym in list(st.pending):
                pe = st.pending[sym]
                if now >= hm(pe["valid_until"]):
                    del st.pending[sym]
                    continue
                last = self.feed.last(sym)
                if last is None or last < pe["trigger"] or sym in st.positions:
                    continue
                if last > pe["limit"]:
                    continue
                stop = self.costs.ticks.round(pe["limit"] * (1 - pe["stop_pct"]), "sell")
                q = S.position_qty(st.equity_start + st.realized, pe["limit"], stop, pe["adv20"], self._gross(),
                                   cfg, st.mode == "cautious")
                del st.pending[sym]
                if q <= 0:
                    continue
                o = self.broker.place(Order(sym, "buy", q, pe["limit"], "limit", note="orb"))
                fill = o.avg_fill or pe["limit"]
                if o.status == "rejected":
                    notify.send(f"[Günlük Bot] emir reddedildi: {sym}")
                    continue
                risk = fill - stop
                tp1 = None if get(cfg, "strategy_orb.take_profit.mode") in ("trail_only", "eod_only") else \
                    self.costs.ticks.round(fill + get(cfg, "strategy_orb.take_profit.tp1_r", 2.0) * risk, "buy")
                st.positions[sym] = asdict(LivePos(sym, "orb", q, fill, stop, stop, tp1, fill,
                                                   entry_time=now.isoformat()))
                st.realized -= self.costs.commission(q * fill)
                notify.send(f"[Günlük Bot] AL {sym} {q} @~{fill:.2f} stop {stop} TP1 {tp1}")

        # risk limitleri
        unreal = sum(p["qty"] * ((self.feed.last(s) or p["entry"]) - p["entry"]) for s, p in st.positions.items())
        day_ret = (st.realized + unreal) / st.equity_start
        if not st.halted and day_ret <= get(cfg, "risk.daily_loss_limit", -0.015):
            st.halted = True
            st.pending.clear()
            for sym in list(st.positions):
                self._exit(sym, st.positions[sym]["qty"], "günlük_zarar_limiti")
            notify.send(f"[Günlük Bot] günlük zarar limiti ({day_ret:.2%}) → gün kapatıldı")
        elif not st.locked and day_ret >= get(cfg, "risk.daily_profit_lock", 0.025):
            st.locked = True
            st.pending.clear()
            notify.send(f"[Günlük Bot] günlük kâr kilidi ({day_ret:.2%})")
        self.save()

    def report(self) -> None:
        st = self.state
        n = len(st.trades)
        wins = sum(1 for t in st.trades if t["pnl"] > 0)
        notify.send(f"[Günlük Bot] {st.date} sonu: P&L {st.realized:,.0f} TL ({st.realized / st.equity_start:.2%}), "
                    f"işlem {n}, kazanan {wins}. Rejim {st.mode}.")
        self.equity += st.realized
        self.save()


def run_day(bot: LiveDailyBot, sleep_sec: int = 20) -> None:
    """Basit zamanlayıcı: hazırlık → seçim → dakikalık döngü → rapor."""
    now = lambda: pd.Timestamp.now(tz=TZ)
    bot.premarket()
    m = int(get(bot.cfg, "selection.opening_range_minutes", 5))
    sel_t = now().normalize() + pd.Timedelta(hours=10, minutes=m, seconds=15)
    while now() < sel_t:
        time.sleep(5)
    bot.select()
    end = now().normalize() + pd.Timedelta(hours=17, minutes=56)
    while now() < end:
        try:
            bot.tick(now())
        except Exception as e:  # noqa: BLE001
            log.exception("tick hatası")
            notify.send(f"[Günlük Bot] HATA: {e}")
        time.sleep(sleep_sec)
    bot.report()
