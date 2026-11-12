"""Sentetik veriyle uçtan uca duman testleri (kurallar ihlal edilmiyor mu?)."""
import pandas as pd
import pytest

from bisttrader.config import deep_merge, get, load_config
from bisttrader.costs import CostModel
from bisttrader.daily.engine import DailyBacktester
from bisttrader.data.store import DataStore
from bisttrader.data.synthetic import make_synthetic
from bisttrader.midterm.engine import MidtermBacktester


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    d = tmp_path_factory.mktemp("synth")
    make_synthetic(d, n_symbols=12, end="2026-11-10")
    return DataStore(d)


def test_daily_engine_rules(store):
    cfg = load_config("daily_bot")
    cfg = deep_merge(cfg, get(cfg, "backtest.interval_overrides.1h"))
    cfg = deep_merge(cfg, {"universe": {"min_adv20_try": 1e6}})
    bt = DailyBacktester(store, cfg, CostModel(), "1h", start="2026-01-01", end="2026-11-10")
    r = bt.run(cfg)
    tr = r["trades"]
    assert len(r["equity"]) > 100
    if len(tr):
        # gece pozisyon yok: her işlem aynı gün kapanır, en geç 17:55
        assert (pd.to_datetime(tr["exit_time"]).dt.tz_convert("Europe/Istanbul").dt.date == tr["date"].dt.date).all()
        hhmm = pd.to_datetime(tr["exit_time"]).dt.tz_convert("Europe/Istanbul").dt.strftime("%H:%M")
        assert (hhmm <= "17:55").all()
        # %80 brüt kuralı: aynı anda açık pozisyonların toplamı
        for d, g in tr.groupby("date"):
            eq = r["equity"]["equity"].shift(1).fillna(bt.E0).loc[d]
            assert (g["qty"] * g["entry"]).sum() <= 0.80 * eq * len(g) + 1  # zayıf kontrol (ardışık girişler)
            mx = 0.0
            for t in sorted(set(g["entry_time"]) | set(g["exit_time"])):
                open_ = g[(g["entry_time"] <= t) & (g["exit_time"] > t)]
                mx = max(mx, float((open_["qty"] * open_["entry"]).sum()))
            assert mx <= 0.80 * eq * 1.02


def test_random_baseline_runs(store):
    cfg = load_config("daily_bot")
    cfg = deep_merge(cfg, get(cfg, "backtest.interval_overrides.1h"))
    cfg = deep_merge(cfg, {"universe": {"min_adv20_try": 1e6}, "selection": {"random_seed": 3}})
    bt = DailyBacktester(store, cfg, CostModel(), "1h", start="2026-06-01", end="2026-11-10")
    r = bt.run(cfg)
    assert len(r["equity"]) > 50


def test_midterm_engine_caps(store):
    cfg = load_config("midterm_bot")
    bt = MidtermBacktester(store, cfg, start="2025-01-01", end="2026-11-10")
    r = bt.run(cfg)
    eq = r["equity"]
    assert (eq["equity"] > 0).all()
    share_crypto = eq["crypto"] / eq["equity"]
    assert share_crypto.max() <= 0.105   # %10 sert tavan (+fiyat oynaması toleransı)
    tr = r["trades"]
    if len(tr):
        assert set(tr["sleeve"]).issubset({"bist", "foreign", "crypto", "fx", "tax"})
