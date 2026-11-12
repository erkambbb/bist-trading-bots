"""Teknik göstergeler (tek kaynak: backtest ve canlı aynı fonksiyonları kullanır)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    return pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder ATR."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def cumulative_vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                    volume: np.ndarray) -> np.ndarray:
    """Gün içi kümülatif VWAP (tipik fiyat ile)."""
    tp = (high + low + close) / 3.0
    v = np.where(volume > 0, volume, 0.0)
    cv = np.cumsum(v)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.cumsum(tp * v) / cv
    # hacim yoksa tipik fiyat
    return np.where(cv > 0, out, tp)


def rolling_atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """Bar dizisi için Wilder ATR (numpy, gün içi trailing için)."""
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    out = np.empty_like(tr)
    out[0] = tr[0]
    a = 1.0 / n
    for i in range(1, len(tr)):
        out[i] = out[i - 1] + a * (tr[i] - out[i - 1])
    return out
