"""Orta Vade Bot canlı planlayıcı: gerçek portföy durumundan emir fişleri üretir.

Backtest motorundaki kurallar (regime_weights, _weekly_bist, _daily_checks, foreign_composition)
aynen kullanılır; tek fark, portföyün simülasyondan değil `data/live/portfolio.json`'dan gelmesidir.

Akış:
  bisttrader midterm-weekly     → Cuma akşamı: skor + rejim + emir fişleri (Telegram)
  bisttrader midterm-daily      → her gün 08:30 / 18:30: stop, dilim tetikleri, KAP/LLM olayları
  bisttrader midterm-record ... → Midas'ta gerçekleşen emri portföy dosyasına işle
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .. import notify
from ..config import DATA_DIR, get
from ..data.store import DataStore
from . import factors as F
from .engine import CRYPTO, BistPos, MidtermBacktester

STATE = DATA_DIR / "live" / "portfolio.json"


def load_state(path: Path = STATE, initial_cash: float = 500_000.0) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"cash_try": initial_cash, "usd_cash": 0.0, "bist": {}, "foreign": {}, "crypto": {},
            "cur_t": {"bist": 0.0, "foreign": 0.0, "crypto": 0.0}, "history": []}


def save_state(st: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def _to_pos(sym: str, r: dict) -> BistPos:
    p = BistPos(r["qty"], r["qty"] * r["avg"], r.get("peak", r["avg"]), pd.Timestamp(r["t1_date"]),
                r.get("t1_price", r["avg"]), r.get("target_value", r["qty"] * r["avg"] / 0.4),
                tranches=r.get("tranches", {"T1": "done", "T2": "pending", "T3": "pending"}),
                trimmed=r.get("trimmed", False), exit_stage=r.get("exit_stage", 0),
                t1_sue_period=r.get("t1_sue_period"))
    return p


def record_fill(sleeve: str, symbol: str, side: str, qty: float, price: float, fx: float = 1.0,
                tranche: str | None = None, target_value: float | None = None, path: Path = STATE) -> dict:
    """Kullanıcının Midas'ta gerçekleştirdiği emri portföy durumuna işler."""
    st = load_state(path)
    today = str(pd.Timestamp.now().date())
    if sleeve == "bist":
        book = st["bist"]
        if side == "buy":
            st["cash_try"] -= qty * price
            if symbol in book:
                r = book[symbol]
                r["avg"] = (r["avg"] * r["qty"] + price * qty) / (r["qty"] + qty)
                r["qty"] += qty
                if tranche:
                    r.setdefault("tranches", {})[tranche] = "done"
            else:
                book[symbol] = {"qty": qty, "avg": price, "t1_date": today, "t1_price": price, "peak": price,
                                "target_value": target_value or qty * price / 0.4,
                                "tranches": {"T1": "done", "T2": "pending", "T3": "pending"}}
        else:
            st["cash_try"] += qty * price
            book[symbol]["qty"] -= qty
            if book[symbol]["qty"] <= 0:
                del book[symbol]
    else:
        book = st[sleeve]
        if side == "buy":
            st["usd_cash"] -= qty * price
            r = book.setdefault(symbol, {"qty": 0.0, "avg_usd": 0.0, "lots": []})
            r["avg_usd"] = (r["avg_usd"] * r["qty"] + price * qty) / (r["qty"] + qty)
            r["qty"] += qty
            r["lots"].append({"qty": qty, "usd_px": price, "fx": fx, "date": today})
        else:
            st["usd_cash"] += qty * price
            book[symbol]["qty"] -= qty
            if book[symbol]["qty"] <= 1e-9:
                del book[symbol]
    st["history"].append({"date": today, "sleeve": sleeve, "symbol": symbol, "side": side, "qty": qty,
                          "price": price, "fx": fx, "tranche": tranche})
    save_state(st, path)
    return st


def _ticket(o: dict, d, C, fx: float, E: float, scores: pd.DataFrame | None) -> str:
    s = o["symbol"]
    if o["sleeve"] == "bist":
        px = float(C.at[d, s])
        if o["side"] == "buy":
            q = math.floor(o["value"] / px)
            extra = ""
            if scores is not None and s in scores.index:
                r = scores.loc[s]
                extra = (f"\nSkor {r['score']:.2f} (ilk %{r['rank_pct'] * 100:.0f}) · potansiyel "
                         f"{(r.get('upside') if pd.notna(r.get('upside')) else float('nan')):.0%}")
            return (f"[AL • {o.get('tranche', '')}] {s} | BIST | Kapanış seansı\nAdet {q} · Limit ~{px:.2f} (±%1) · "
                    f"Tutar ~{q * px:,.0f} TL (%{q * px / E * 100:.1f}){extra}")
        frac = "TAMAMI" if o.get("all") else f"%{o.get('frac', 1) * 100:.0f}"
        return f"[SAT • {o.get('reason', '')}] {s} | BIST | {frac} | Kapanış seansı ~{px:.2f}"
    side = "AL" if o["side"] == "buy" else "SAT"
    return f"[{side}] {s} | {o['sleeve']} | ~{o['usd']:,.0f} $ (~{o['usd'] * fx:,.0f} TL)"


def plan(store: DataStore, cfg: dict, weekly: bool = True, path: Path = STATE, send: bool = True,
         llm_events: dict | None = None) -> list[dict]:
    """Bugün için emir listesi üretir ve (istenirse) Telegram'a yollar."""
    st = load_state(path)
    bt = MidtermBacktester(store, cfg, start=str((pd.Timestamp.now() - pd.Timedelta(days=30)).date()))
    bt.prepare()
    C = bt.close
    d = C.index[-1]
    fx = float(bt.fx.iloc[-1]) if len(bt.fx) else 1.0
    bist = {s: _to_pos(s, r) for s, r in st["bist"].items() if s in C.columns}
    gpx = lambda s: float(bt.gd[s].iloc[-1]) if s in bt.gd else np.nan
    E = st["cash_try"] + sum(p.qty * C.at[d, s] for s, p in bist.items()) + \
        (st["usd_cash"] + sum(r["qty"] * gpx(s) for s, r in {**st["foreign"], **st["crypto"]}.items())) * fx
    tr_sizes = [t["size"] for t in get(cfg, "entry.tranches")]
    w_fac = {**get(cfg, "factors.weights")}
    if llm_events is None:
        w_fac["news"] = 0.0
    w_fac["analyst"] = 0.0
    pending: list[dict] = []
    liq = bt.adv20.loc[d]
    univ = [s for s in bt.universe if pd.notna(C.at[d, s]) and liq.get(s, 0) >= get(cfg, "universe_bist.add_bist_all_if_adv20_try_above", 5e7)]
    scores = F.compute_scores(d, C, bt.raw, bt.xu, bt.tidy, bt.sectors, w_fac, univ)
    if llm_events and not scores.empty:
        from ..nlp.llm import news_score
        ns = pd.Series({s: news_score(ev, d, get(cfg, "factors.news_half_life_days", 10)) for s, ev in llm_events.items()})
        scores["score"] += get(cfg, "factors.weights.news", 0.05) * F.zscore(ns.reindex(scores.index).fillna(0), None)
        scores["rank_pct"] = scores["score"].rank(ascending=False, pct=True)
        for s, ev in llm_events.items():
            if s in bist and any(e.get("direction", 0) <= -2 and e.get("relevant", True) for e in ev):
                pending.append({"sleeve": "bist", "symbol": s, "side": "sell", "all": True,
                                "reason": "tez_kırılması (KAP/LLM) — ONAY GEREKLİ"})
    bt._daily_checks(d, bist, pending, scores if not scores.empty else pd.DataFrame(), cfg, tr_sizes,
                     get(cfg, "exit.catastrophe_stop", -0.15))
    rg = None
    if weekly:
        rg = bt.regime_weights(d, cfg, scores)
        cur_t = st.get("cur_t", {"bist": 0.0, "foreign": 0.0, "crypto": 0.0})
        tw = max(int(get(cfg, "schedule.allocation_transition_weeks", 3)), 1)
        for k in cur_t:
            gap = rg[k] - cur_t[k]
            cur_t[k] += gap if abs(gap) <= 0.02 or cur_t[k] == 0 else gap / tw
        st["cur_t"] = cur_t
        if not scores.empty:
            bt._weekly_bist(d, E, scores, bist, pending, cur_t, cfg, C, tr_sizes,
                            get(cfg, "entry.candidate_percentile", 0.2), get(cfg, "exit.sell_percentile", 0.5),
                            get(cfg, "valuation.min_upside_to_buy", 0.15),
                            get(cfg, "exit.trailing_weekly.from_avg_cost_atr", 2.5),
                            get(cfg, "exit.trailing_weekly.from_peak_atr", 3.0),
                            get(cfg, "sizing.max_single_bist", 0.10), get(cfg, "sizing.max_sector_bist", 0.25),
                            get(cfg, "sizing.bist_positions", [8, 15])[1])
        # yurt dışı
        target_usd = cur_t["foreign"] * E / fx
        comp = bt.foreign_composition(d, cfg, target_usd)
        cur_vals = {s: r["qty"] * gpx(s) for s, r in st["foreign"].items()}
        for s in set(comp) | set(cur_vals):
            diff = comp.get(s, 0) * target_usd - cur_vals.get(s, 0.0)
            if abs(diff) >= bt.fc.min_position_usd or (comp.get(s, 0) == 0 and cur_vals.get(s, 0) > 0):
                pending.append({"sleeve": "foreign", "symbol": s, "side": "buy" if diff > 0 else "sell", "usd": abs(diff)})
        # kripto
        cr_vals = {s: r["qty"] * gpx(s) for s, r in st["crypto"].items()}
        cr_w = sum(cr_vals.values()) * fx / E if E else 0
        tgt = cur_t["crypto"]
        if cr_w > get(cfg, "crypto.hard_cap", 0.10):
            tgt = min(tgt, get(cfg, "crypto.trim_to", 0.08))
        if abs(cr_w - tgt) > get(cfg, "crypto.rebalance_band", 0.02) or (tgt == 0 and cr_w > 0):
            for s, w in CRYPTO.items():
                diff = tgt * w * E / fx - cr_vals.get(s, 0.0)
                if abs(diff) >= 50:
                    pending.append({"sleeve": "crypto", "symbol": s, "side": "buy" if diff > 0 else "sell", "usd": abs(diff)})
    # durum güncelle (dilim sona ermeleri vb.)
    for s, p in bist.items():
        r = st["bist"][s]
        r["tranches"] = p.tranches
        r["peak"] = max(r.get("peak", p.avg), float(C.at[d, s]))
        r["exit_stage"] = p.exit_stage
    save_state(st, path)
    out_dir = path.parent
    (out_dir / f"tickets_{d.date()}.json").write_text(json.dumps(pending, ensure_ascii=False, indent=1, default=str))
    if send:
        head = f"[Orta Vade] {d.date()} {'haftalık' if weekly else 'günlük'} plan · özkaynak ~{E:,.0f} TL"
        if rg:
            head += (f"\nRejim {rg['R']}/4 ({', '.join(rg['reasons'])}) · hedef BIST %{st['cur_t']['bist'] * 100:.0f}, "
                     f"yurt dışı %{st['cur_t']['foreign'] * 100:.0f}, kripto %{st['cur_t']['crypto'] * 100:.0f}")
        body = "\n\n".join(_ticket(o, d, C, fx, E, scores if not scores.empty else None) for o in pending) or "Emir yok."
        notify.send(head + "\n\n" + body)
    return pending


def fetch_llm_events(symbols: list[str], cfg: dict, days: int = 30) -> dict:
    """KAP bildirimlerini borsapy ile çeker, LLM ile sınıflandırır."""
    import borsapy as bp

    from ..nlp.llm import LLMClient
    llm = LLMClient(get(cfg, "llm.filter_model_id", "claude-haiku-5-5"), get(cfg, "llm.analysis_model_id", "claude-sonnet-5-5"))
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    out: dict = {}
    for s in symbols:
        try:
            news = bp.Ticker(s).news
        except Exception:  # noqa: BLE001
            continue
        if news is None or news.empty:
            continue
        evs = []
        for _, row in news.head(15).iterrows():
            dt = pd.to_datetime(row.get("Date") or row.get("date"), errors="coerce", dayfirst=True)
            if pd.isna(dt):
                continue
            if dt.tzinfo is not None:
                dt = dt.tz_localize(None)
            if dt < cutoff:
                continue
            title = str(row.get("Title") or row.get("title") or "")
            ev = llm.classify_event(s, title)
            ev["date"] = str(dt.date())
            ev["title"] = title
            evs.append(ev)
        if evs:
            out[s] = evs
    return out
