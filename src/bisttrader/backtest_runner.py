"""İki botun backtest paketleri (docs/05-backtest-plani.md).

Günlük Bot
    1. Ön-kayıtlı varsayılan parametrelerle 2 yıl (en iyi kapsayan çözünürlükte)
    2. Parametre ızgarası + walk-forward (6 ay eğitim → 2 ay test, plato seçimi), Deflated Sharpe
    3. Tabanlar: rastgele giriş (çok tohum), RVOL filtresiz ORB, PPF, XU100
    4. Stres: maliyet ×1,5 / ×2, S3 kurum senaryosu; blok bootstrap
    5. Yüksek çözünürlük kontrolü (5/15 dk veri ne kadar geriye gidiyorsa)
Orta Vade Bot
    1. Kalibrasyon (2018-11 → 2024-11) ızgarası → seçilen parametre
    2. Son 2 yıl OOS: varsayılan + kalibre edilmiş; ablasyonlar; karşılaştırma tabanları
"""
from __future__ import annotations

import copy
import itertools
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .config import deep_merge, get, load_config, set_path
from .costs import CostModel
from .daily.engine import DailyBacktester
from .data.store import DataStore
from .metrics import block_bootstrap, deflated_sharpe, perf_summary, sharpe, trade_stats
from .midterm.engine import MidtermBacktester

DAILY_GRID = {
    "selection.rvol_threshold": [1.5, 2.0, 3.0],
    "selection.max_candidates.normal": [2, 4],
    "strategy_orb.stop.k_atr": [0.25, 0.35, 0.5, "structural"],
    "strategy_orb.take_profit.mode": ["partial_then_trail", "trail_only", "eod_only"],
    "regime.enabled": [True, False],
}

MID_WEIGHTS = {
    "varsayılan": {"value": 0.20, "quality": 0.25, "momentum": 0.25, "earnings_surprise": 0.15},
    "eşit": {"value": 0.25, "quality": 0.25, "momentum": 0.25, "earnings_surprise": 0.25},
    "momentum_ağır": {"value": 0.10, "quality": 0.20, "momentum": 0.50, "earnings_surprise": 0.20},
    "kalite_ağır": {"value": 0.20, "quality": 0.45, "momentum": 0.20, "earnings_surprise": 0.15},
}
MID_THRESH = [(0.20, 0.50), (0.10, 0.40), (0.30, 0.60)]
MID_TRANCHES = {
    "40/30/30": None,
    "tek_seferde": [{"name": "T1", "size": 1.0, "max_rsi": 70, "max_ext_over_sma50": 1.15}],
    "33/33/34": [{"name": "T1", "size": 0.33, "max_rsi": 70, "max_ext_over_sma50": 1.15},
                 {"name": "T2", "size": 0.33, "pullback_atr": 1.0, "rsi_max": 35, "valid_weeks": 6, "min_score_percentile": 0.30},
                 {"name": "T3", "size": 0.34, "confirm": ["close_above_sma20"], "valid_weeks": 10}],
}


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# =========================================================================== #
# GÜNLÜK BOT
# =========================================================================== #
def choose_interval(store: DataStore, start: pd.Timestamp, end: pd.Timestamp) -> tuple[str | None, dict]:
    cover = {}
    ix = store.index_daily()
    xu_days = ix[(ix["symbol"] == "XU100") & (ix["date"] >= start) & (ix["date"] <= end)]["date"].nunique()
    for iv in store.intraday_intervals():
        df = store.bist_intraday(iv)
        dd = df[(df["date"] >= start) & (df["date"] <= end)]["date"].unique()
        cover[iv] = {"days": int(len(dd)), "first": str(pd.Timestamp(dd.min()).date()) if len(dd) else None,
                     "share": float(len(dd) / max(xu_days, 1))}
    order = ["1m", "5m", "15m", "30m", "1h"]
    primary = None
    for iv in order:
        if iv in cover and cover[iv]["share"] >= 0.85:
            primary = iv
            break
    if primary is None and cover:
        primary = max(cover, key=lambda k: cover[k]["days"])
    return primary, cover


def _daily_cfg(interval: str) -> dict:
    cfg = load_config("daily_bot")
    return deep_merge(cfg, get(cfg, f"backtest.interval_overrides.{interval}", {}) or {})


def daily_suite(store: DataStore, out: Path, equity: float = 500_000.0, start: str = "2024-11-11",
                end: str = "2026-11-10", quick: bool = False, n_random: int = 20) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    primary, cover = choose_interval(store, s, e)
    if primary is None:
        raise RuntimeError("Gün içi veri yok")
    _log(f"Günlük bot: birincil çözünürlük {primary}; kapsama {cover}")
    cfg0 = _daily_cfg(primary)
    costs = CostModel(scenario=get(cfg0, "cost_scenario", "S1_midas_matriks_api"))
    bt = DailyBacktester(store, cfg0, costs, primary, start=start, end=end, initial_equity=equity,
                         reserve_ppf_tax=get(cfg0, "backtest.reserve_ppf_tax", 0.175))
    bt.prepare()
    res = {"interval": primary, "coverage": cover}

    def run(cfg):
        r = bt.run(cfg)
        eq = r["equity"]["equity"]
        return r, eq

    # 1) varsayılan
    r0, eq0 = run(cfg0)
    rf = r0["ppf"]
    res["default"] = {"summary": perf_summary(eq0, rf), "trades": trade_stats(r0["trades"]),
                      "reserve_income": float(r0["equity"]["reserve_income"].sum()),
                      "trading_pnl": float(r0["equity"]["trading_pnl"].sum()),
                      "costs": float(r0["equity"]["costs"].sum()),
                      "days_by_mode": r0["days"]["mode"].value_counts().to_dict(),
                      "exit_reasons": r0["trades"]["exit_reason"].value_counts().to_dict() if len(r0["trades"]) else {},
                      "by_strategy": r0["trades"].groupby("strategy")["pnl"].agg(["count", "sum"]).to_dict() if len(r0["trades"]) else {}}
    res["_default_run"] = r0
    _log(f"  varsayılan: {res['default']['summary']['total_return']:.2%}, işlem {res['default']['trades'].get('n_trades')}")

    # 2) ızgara
    keys = list(DAILY_GRID)
    combos = list(itertools.product(*[DAILY_GRID[k] for k in keys]))
    if quick:
        combos = combos[:: max(len(combos) // 12, 1)]
    rets = {}
    t0 = time.time()
    for n, combo in enumerate(combos):
        c = copy.deepcopy(cfg0)
        for k, v in zip(keys, combo):
            c = set_path(c, k, v)
        r, eq = run(c)
        rets[combo] = eq.pct_change().fillna(0.0)
        if n % 20 == 0:
            _log(f"  ızgara {n + 1}/{len(combos)} ({time.time() - t0:.0f} sn)")
    grid_rows = []
    for combo, r in rets.items():
        eqc = (1 + r).cumprod() * equity
        grid_rows.append(dict(zip(keys, combo), total_return=float(eqc.iloc[-1] / equity - 1),
                              sharpe=sharpe(r, rf), max_dd=float((eqc / eqc.cummax() - 1).min())))
    grid = pd.DataFrame(grid_rows).sort_values("sharpe", ascending=False)
    res["grid"] = grid
    res["n_trials"] = len(combos)

    # walk-forward
    idx = rf.index
    first = idx[0]
    folds = []
    oos_parts = []
    chosen = []
    k = 0
    while True:
        is_start = first + pd.DateOffset(months=2 * k)
        is_end = is_start + pd.DateOffset(months=6)
        oos_end = is_end + pd.DateOffset(months=2)
        if is_end >= idx[-1]:
            break
        is_mask = (idx >= is_start) & (idx < is_end)
        oos_mask = (idx >= is_end) & (idx < oos_end)
        if oos_mask.sum() < 10:
            break
        sc = {cb: sharpe(r[is_mask], rf[is_mask]) for cb, r in rets.items()}
        plat = {}
        for cb, v in sc.items():
            neigh = [sc[o] for o in sc if sum(a != b for a, b in zip(o, cb)) == 1]
            plat[cb] = 0.5 * v + 0.5 * (np.mean(neigh) if neigh else v)
        best = max(plat, key=plat.get)
        chosen.append(best)
        oos = rets[best][oos_mask]
        oos_parts.append(oos)
        folds.append({"is": f"{is_start.date()}→{(is_end - pd.Timedelta(days=1)).date()}",
                      "oos": f"{is_end.date()}→{(oos_end - pd.Timedelta(days=1)).date()}",
                      "params": dict(zip(keys, best)), "is_sharpe": sc[best],
                      "oos_return": float((1 + oos).prod() - 1),
                      "oos_ppf": float((1 + rf[oos_mask]).prod() - 1)})
        k += 1
    if oos_parts:
        wf = pd.concat(oos_parts)
        wf_eq = (1 + wf).cumprod() * equity
        res["walk_forward"] = {"folds": folds, "summary": perf_summary(wf_eq, rf, n_trials=len(combos)),
                               "positive_folds": int(sum(f["oos_return"] > f["oos_ppf"] for f in folds))}
        res["_wf_eq"] = wf_eq
    # 3) tabanlar
    rand = []
    for sd in range(n_random if not quick else 4):
        c = set_path(cfg0, "selection.random_seed", sd)
        _, eq = run(c)
        rand.append(float(eq.iloc[-1] / equity - 1))
    strat = res["default"]["summary"]["total_return"]
    res["random"] = {"returns": rand, "median": float(np.median(rand)),
                     "p_value": float(np.mean([x >= strat for x in rand]))}
    _, eq_nf = run(set_path(set_path(cfg0, "selection.rvol_threshold", 0.0), "selection.score_weights.news_flag", 0.0))
    res["no_sip"] = perf_summary(eq_nf, rf)
    res["_no_sip_eq"] = eq_nf
    # 4) stres
    stress = {}
    for mult in (1.5, 2.0):
        bt.costs.cost_multiplier = mult
        _, eq = run(cfg0)
        stress[f"maliyet ×{mult}"] = perf_summary(eq, rf)
    bt.costs.cost_multiplier = 1.0
    for scn in ("S2_api_broker_negotiated", "S3_api_broker_standard"):
        keep = bt.costs
        bt.costs = CostModel(scenario=scn)
        _, eq = run(cfg0)
        stress[scn] = perf_summary(eq, rf)
        bt.costs = keep
    res["stress"] = stress
    res["bootstrap"] = block_bootstrap(eq0.pct_change().dropna())
    # 5) yüksek çözünürlük kontrolü
    hires = {}
    for iv in ("5m", "15m"):
        if iv == primary or iv not in cover or cover[iv]["days"] < 20:
            continue
        cfg_h = _daily_cfg(iv)
        bth = DailyBacktester(store, cfg_h, CostModel(scenario=get(cfg_h, "cost_scenario")), iv,
                              start=cover[iv]["first"], end=end, initial_equity=equity)
        try:
            bth.prepare()
            rh = bth.run(cfg_h)
            eqh = rh["equity"]["equity"]
            sub0 = eq0[eq0.index >= eqh.index[0]]
            hires[iv] = {"window": f"{eqh.index[0].date()}→{eqh.index[-1].date()}",
                         "summary": perf_summary(eqh, rh["ppf"]), "trades": trade_stats(rh["trades"]),
                         "primary_same_window": float(sub0.iloc[-1] / sub0.iloc[0] - 1) if len(sub0) > 1 else None,
                         "by_strategy": rh["trades"].groupby("strategy")["pnl"].agg(["count", "sum"]).to_dict() if len(rh["trades"]) else {}}
        except Exception as ex:  # noqa: BLE001
            hires[iv] = {"error": str(ex)}
    res["hires"] = hires
    return res


# =========================================================================== #
# ORTA VADE BOT
# =========================================================================== #
def midterm_suite(store: DataStore, out: Path, equity: float = 500_000.0, quick: bool = False) -> dict:
    cfg0 = load_config("midterm_bot")
    cal = get(cfg0, "backtest.calibration")
    oos = get(cfg0, "backtest.oos")
    impl = get(cfg0, "backtest.bist_implicit_per_side", 0.0005)
    res = {}

    def mk(start, end):
        b = MidtermBacktester(store, cfg0, start=start, end=end, initial_equity=equity, bist_implicit_per_side=impl)
        b.prepare()
        return b

    def variant(base, weights=None, thr=None, tranches=None):
        c = copy.deepcopy(base)
        if weights:
            c = set_path(c, "factors.weights", {**get(c, "factors.weights"), **weights})
        if thr:
            c = set_path(c, "entry.candidate_percentile", thr[0])
            c = set_path(c, "exit.sell_percentile", thr[1])
        if tranches:
            c = set_path(c, "entry.tranches", tranches)
        return c

    # kalibrasyon
    bcal = mk(*cal)
    cal_rows = []
    t0 = time.time()
    wkeys = list(MID_WEIGHTS)[:2] if quick else list(MID_WEIGHTS)
    tkeys = MID_THRESH[:2] if quick else MID_THRESH
    for wn in wkeys:
        for th in tkeys:
            c = variant(cfg0, MID_WEIGHTS[wn], th)
            r = bcal.run(c)
            sm = perf_summary(r["equity"]["equity"], r["ppf"])
            cal_rows.append({"weights": wn, "buy_pct": th[0], "sell_pct": th[1], "tranches": "40/30/30",
                             "total_return": sm["total_return"], "cagr": sm["cagr"], "sharpe": sm["sharpe_vs_ppf"],
                             "max_dd": sm["max_dd"]})
            _log(f"  kalibrasyon {wn} {th}: {sm['total_return']:.1%} sharpe {sm['sharpe_vs_ppf']:.2f} ({time.time() - t0:.0f} sn)")
    best = max(cal_rows, key=lambda x: x["sharpe"])
    for tn, tr in MID_TRANCHES.items():
        if tn == "40/30/30" or quick:
            continue
        c = variant(cfg0, MID_WEIGHTS[best["weights"]], (best["buy_pct"], best["sell_pct"]), tr)
        r = bcal.run(c)
        sm = perf_summary(r["equity"]["equity"], r["ppf"])
        cal_rows.append({"weights": best["weights"], "buy_pct": best["buy_pct"], "sell_pct": best["sell_pct"],
                         "tranches": tn, "total_return": sm["total_return"], "cagr": sm["cagr"],
                         "sharpe": sm["sharpe_vs_ppf"], "max_dd": sm["max_dd"]})
    best = max(cal_rows, key=lambda x: x["sharpe"])
    res["calibration"] = pd.DataFrame(cal_rows).sort_values("sharpe", ascending=False)
    res["calibration_window"] = cal
    res["n_trials"] = len(cal_rows)
    rcal_def = bcal.run(cfg0)
    res["calibration_default"] = perf_summary(rcal_def["equity"]["equity"], rcal_def["ppf"])
    res["chosen"] = best

    # OOS
    boos = mk(*oos)
    r_def = boos.run(cfg0)
    c_best = variant(cfg0, MID_WEIGHTS[best["weights"]], (best["buy_pct"], best["sell_pct"]), MID_TRANCHES[best["tranches"]])
    r_best = boos.run(c_best)
    res["oos_window"] = oos
    for name, r in (("default", r_def), ("calibrated", r_best)):
        eq = r["equity"]
        tr = r["trades"]
        res[name] = {"summary": perf_summary(eq["equity"], r["ppf"], n_trials=res["n_trials"]),
                     "end_sleeves": eq.iloc[-1].to_dict(),
                     "avg_sleeves": (eq[["cash_try", "bist", "foreign", "crypto"]].div(eq["equity"], axis=0)).mean().to_dict(),
                     "n_trades": tr.groupby(["sleeve", "side"]).size().to_dict() if len(tr) else {},
                     "bist_reasons": tr[tr["sleeve"] == "bist"]["reason"].value_counts().to_dict() if len(tr) else {},
                     "tax_paid": float(eq["tax_paid"].iloc[-1]),
                     "fees": float(tr["fee_try"].sum()) if len(tr) else 0.0,
                     "bist_realized": float(tr[(tr["sleeve"] == "bist")]["gain_try"].sum()) if len(tr) else 0.0}
        res[f"_{name}_run"] = r
    res["bench"] = {k: perf_summary(v, r_def["ppf"]) for k, v in r_def["bench"].items()}
    # ablasyonlar
    abl = {}
    if not quick:
        for nm, c in (("rejim kapalı (sabit %60 BIST)", set_path(cfg0, "regime_bist.enabled", False)),
                      ("kripto trend filtresi kapalı (sabit %5)", set_path(cfg0, "crypto.trend_enabled", False)),
                      ("tek seferde alım (dilim yok)", variant(cfg0, tranches=MID_TRANCHES["tek_seferde"])),
                      ("trailing 4×ATR", set_path(cfg0, "exit.trailing_weekly.from_peak_atr", 4.0))):
            r = boos.run(c)
            abl[nm] = perf_summary(r["equity"]["equity"], r["ppf"])
    res["ablations"] = abl
    return res
