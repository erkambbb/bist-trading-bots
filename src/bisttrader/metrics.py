"""Performans ölçütleri (docs/05-backtest-plani.md §6)."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats as st

EULER_GAMMA = 0.5772156649


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1).min()) if len(equity) else 0.0


def cagr(equity: pd.Series, periods_per_year: float = 252) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    years = len(equity) / periods_per_year
    return float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0


def sharpe(ret: pd.Series, rf: pd.Series | None = None, periods: int = 252) -> float:
    ex = ret - (rf.reindex(ret.index).fillna(0) if rf is not None else 0)
    sd = ex.std(ddof=1)
    return float(ex.mean() / sd * math.sqrt(periods)) if sd and sd > 0 else 0.0


def sortino(ret: pd.Series, rf: pd.Series | None = None, periods: int = 252) -> float:
    ex = ret - (rf.reindex(ret.index).fillna(0) if rf is not None else 0)
    dn = ex[ex < 0]
    dd = math.sqrt((dn ** 2).sum() / max(len(ex), 1))
    return float(ex.mean() / dd * math.sqrt(periods)) if dd > 0 else 0.0


def deflated_sharpe(ret: pd.Series, n_trials: int, sr_trials_var: float | None = None) -> float:
    """Bailey & López de Prado (2014). Sharpe'lar gözlem frekansında (yıllıklandırılmamış)."""
    r = ret.dropna()
    T = len(r)
    if T < 10 or r.std(ddof=1) == 0:
        return float("nan")
    sr = r.mean() / r.std(ddof=1)
    g3 = st.skew(r)
    g4 = st.kurtosis(r, fisher=False)
    var_sr = sr_trials_var if sr_trials_var is not None else (1 - g3 * sr + (g4 - 1) / 4 * sr ** 2) / (T - 1)
    N = max(int(n_trials), 1)
    if N == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(max(var_sr, 1e-12)) * ((1 - EULER_GAMMA) * st.norm.ppf(1 - 1 / N)
                                               + EULER_GAMMA * st.norm.ppf(1 - 1 / (N * math.e)))
    denom = math.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2, 1e-12))
    return float(st.norm.cdf((sr - sr0) * math.sqrt(T - 1) / denom))


def block_bootstrap(ret: pd.Series, block: int = 10, n: int = 2000, seed: int = 7) -> dict:
    r = ret.dropna().values
    T = len(r)
    if T < block * 2:
        return {}
    rng = np.random.default_rng(seed)
    nb = int(math.ceil(T / block))
    cag, mdd = [], []
    for _ in range(n):
        starts = rng.integers(0, T - block + 1, nb)
        path = np.concatenate([r[s:s + block] for s in starts])[:T]
        eq = np.cumprod(1 + path)
        cag.append(eq[-1] ** (252 / T) - 1)
        peak = np.maximum.accumulate(eq)
        mdd.append((eq / peak - 1).min())
    q = lambda a: {p: float(np.percentile(a, p)) for p in (5, 50, 95)}
    return {"cagr": q(cag), "max_dd": q(mdd)}


def trade_stats(tr: pd.DataFrame) -> dict:
    if tr is None or tr.empty:
        return {"n_trades": 0}
    w = tr[tr["pnl"] > 0]
    l = tr[tr["pnl"] <= 0]
    gp, gl = w["pnl"].sum(), -l["pnl"].sum()
    return {
        "n_trades": int(len(tr)),
        "win_rate": float(len(w) / len(tr)),
        "avg_R": float(tr["R"].mean()),
        "median_R": float(tr["R"].median()),
        "profit_factor": float(gp / gl) if gl > 0 else float("inf"),
        "avg_win_try": float(w["pnl"].mean()) if len(w) else 0.0,
        "avg_loss_try": float(l["pnl"].mean()) if len(l) else 0.0,
        "total_costs_try": float(tr["costs"].sum()),
    }


def perf_summary(equity: pd.Series, rf: pd.Series | None = None, n_trials: int = 1) -> dict:
    ret = equity.pct_change().dropna()
    out = {
        "start": str(equity.index[0].date()) if len(equity) else "",
        "end": str(equity.index[-1].date()) if len(equity) else "",
        "start_equity": float(equity.iloc[0]) if len(equity) else 0.0,
        "end_equity": float(equity.iloc[-1]) if len(equity) else 0.0,
        "total_return": float(equity.iloc[-1] / equity.iloc[0] - 1) if len(equity) else 0.0,
        "cagr": cagr(equity),
        "vol": float(ret.std(ddof=1) * math.sqrt(252)) if len(ret) > 1 else 0.0,
        "sharpe_vs_ppf": sharpe(ret, rf),
        "sortino_vs_ppf": sortino(ret, rf),
        "max_dd": max_drawdown(equity),
    }
    out["calmar"] = out["cagr"] / abs(out["max_dd"]) if out["max_dd"] < 0 else float("nan")
    if rf is not None and len(ret):
        rf_al = rf.reindex(ret.index).fillna(0)
        out["ppf_total_return"] = float((1 + rf_al).prod() - 1)
        out["excess_ret_vs_ppf"] = out["total_return"] - out["ppf_total_return"]
        out["dsr"] = deflated_sharpe(ret - rf_al, n_trials)
    return out


def fmt_pct(x: float, nd: int = 1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "–"
    v = f"{abs(x) * 100:.{nd}f}".replace(".", ",")
    return f"-%{v}" if x < 0 else f"%{v}"


def fmt_num(x: float, nd: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "–"
    s = f"{x:,.{nd}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")
