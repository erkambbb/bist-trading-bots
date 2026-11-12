import json
import socket
import threading

import numpy as np
import pandas as pd
import pytest

from bisttrader.config import load_config
from bisttrader.costs import CostModel
from bisttrader.daily import signals as S
from bisttrader.execution.base import Order
from bisttrader.execution.matriks import TERM, MatriksIQBroker, MatriksIQClient
from bisttrader.execution.paper import PaperBroker
from bisttrader.midterm import factors as F


@pytest.fixture(scope="module")
def costs():
    return CostModel(scenario="S1_midas_matriks_api")


def test_tick_table(costs):
    t = costs.ticks
    assert t.tick(8.0) == 0.01
    assert t.tick(20.0) == 0.02
    assert t.tick(49.98) == 0.02
    assert t.tick(75) == 0.05
    assert t.tick(300) == 0.10
    assert t.round(35.011, "buy") == 35.02
    assert t.round(35.019, "sell") == 35.0


def test_explicit_costs():
    s1 = CostModel(scenario="S1_midas_matriks_api")
    assert s1.explicit_rate == pytest.approx(0.0001 * 1.05)
    s3 = CostModel(scenario="S3_api_broker_standard")
    assert s3.explicit_rate == pytest.approx(0.0009 * 1.05)
    s2 = CostModel(scenario="S2_api_broker_negotiated")
    assert s2.explicit_rate == pytest.approx(0.0002 * 1.05 + 0.000029 * 1.05)


def test_roundtrip_matches_doc(costs):
    # 15 TL hisse: adım/fiyat %0,067 → örtük 2.5×0.067% = %0,167, + açık 2×0,0105%
    assert costs.roundtrip_estimate(15.0) == pytest.approx(2.5 * 0.01 / 15 + 2 * 0.000105, rel=1e-6)


def test_fill_slippage(costs):
    assert costs.fill(100.0, "buy", "stop") == pytest.approx(100.15)
    assert costs.fill(100.0, "sell", "market") == pytest.approx(99.9)
    assert costs.fill(100.0, "buy", "limit") == 100.0


def test_stop_distance_floor(costs):
    cfg = load_config("daily_bot")
    d = S.stop_distance(15.0, 0.02, cfg, costs)       # 0.35×2% = 0.7% < d_min ≈ 6.7×0.188%
    assert d == pytest.approx(6.7 * costs.roundtrip_estimate(15.0))
    assert S.stop_distance(100.0, 0.07, cfg, costs) is None   # 0.35×7% = 2.45% > %2 tavan


def test_position_qty_respects_80pct():
    cfg = load_config("daily_bot")
    q = S.position_qty(500_000, 100.0, 98.8, 1e12, open_notional=350_000, cfg=cfg, cautious=False)
    assert q * 100.0 <= 0.80 * 500_000 - 350_000 + 1e-6
    q2 = S.position_qty(500_000, 100.0, 98.8, 1e12, open_notional=0, cfg=cfg, cautious=False)
    assert q2 * 100 <= 0.20 * 500_000 + 1e-6


def test_select_candidates_filters():
    cfg = load_config("daily_bot")
    t = pd.DataFrame([
        {"symbol": "A", "rvol": 3.0, "gap": 0.01, "atr_pct": 0.03, "or_open": 10, "or_close": 10.2, "or_vwap": 10.1, "news": False},
        {"symbol": "B", "rvol": 1.0, "gap": 0.01, "atr_pct": 0.03, "or_open": 10, "or_close": 10.2, "or_vwap": 10.1, "news": False},
        {"symbol": "C", "rvol": 5.0, "gap": 0.01, "atr_pct": 0.03, "or_open": 10, "or_close": 9.9, "or_vwap": 10.0, "news": False},
        {"symbol": "D", "rvol": 4.0, "gap": 0.10, "atr_pct": 0.03, "or_open": 10, "or_close": 10.2, "or_vwap": 10.1, "news": False},
    ])
    c = S.select_candidates(t, S.Regime("normal"), cfg)
    assert list(c["symbol"]) == ["A"]
    assert S.select_candidates(t, S.Regime("off"), cfg).empty


def test_regime_rules():
    cfg = load_config("daily_bot")
    row = pd.Series({"ix_close": 100, "ix_ema20": 95, "ix_ema20_slope": 0.5, "vix": 18, "spx_ret": 0.002})
    assert S.decide_regime(row, 0.0, cfg).mode == "normal"
    assert S.decide_regime(row, -0.04, cfg).mode == "off"
    row2 = row.copy(); row2["vix"] = 30
    assert S.decide_regime(row2, 0.0, cfg).mode == "cautious"


def test_tidy_decumulates_income():
    rows = []
    for p, v in [("2024Q1", 100), ("2024Q2", 250), ("2024Q3", 400), ("2024Q4", 600), ("2025Q1", 150)]:
        rows.append(("X", "XI_29", "income_stmt", "Satış Gelirleri", p, v))
        rows.append(("X", "XI_29", "income_stmt", "FAALİYET KARI (ZARARI)", p, v / 10))
        rows.append(("X", "XI_29", "balance_sheet", "Finansal Borçlar", p, 50))
        rows.append(("X", "XI_29", "balance_sheet", "Finansal Borçlar", p, 30))
    f = pd.DataFrame(rows, columns=["symbol", "group", "statement", "item", "period", "value"])
    w = F.tidy_fundamentals(f)
    q = w.set_index("period")["revenue_q"]
    assert list(q.values) == [100, 150, 150, 200, 150]
    assert w.set_index("period")["revenue_ttm"]["2025Q1"] == 150 + 150 + 200 + 150
    assert (w["fin_debt"] == 80).all()
    # yayın tarihi: Q4 → +80 gün
    assert w.set_index("period").loc["2024Q4", "publish_date"] == pd.Timestamp("2024-12-31") + pd.Timedelta(days=80)
    assert F.point_in_time(w, pd.Timestamp("2025-03-01")).loc["X", "period"] == "2024Q3"


def test_paper_broker(costs):
    b = PaperBroker(costs, 100_000)
    o = b.place(Order("THYAO", "buy", 100, 300.0))
    b.on_quote("THYAO", 299.9)
    assert o.status == "filled" and b.positions()["THYAO"].qty == 100
    b.place(Order("THYAO", "sell", 100, None, "market"))   # son fiyattan (299,9) hemen dolar
    assert "THYAO" not in b.positions()
    assert 99_900 < b.cash < 100_000                         # sadece spread/kayma + komisyon kaybı


def test_matriks_framing():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen = []

    def serve():
        conn, _ = srv.accept()
        buf = ""
        while True:
            data = conn.recv(4096).decode()
            if not data:
                break
            buf += data
            while TERM in buf:
                msg, buf = buf.split(TERM, 1)
                seen.append(msg)
                if msg.startswith("{"):
                    j = json.loads(msg)
                    if j["ApiCommands"] == 0:
                        conn.sendall((json.dumps({"ApiCommands": 0, "Accounts": [{"BrokageId": "7", "AccountIdList": ["0~1"]}]}) + TERM).encode())
                    if j["ApiCommands"] == 3:
                        conn.sendall((json.dumps({**j, "OrderID": "42", "OrdStatus": "0"}) + TERM).encode())
        conn.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    c = MatriksIQClient(port=port)
    c.connect()
    br = MatriksIQBroker(c, dry_run=False)
    br.discover_account()
    assert br.brokage_id == "7" and br.account_id == "0~1"
    o = br.place(Order("GARAN", "buy", 10, 120.5))
    assert o.broker_id == "42" and o.status == "new"
    sent = json.loads(seen[-1])
    assert sent["OrderSide"] == 1 and sent["OrderType"] == "2" and sent["TimeInForce"] == "0"
    assert seen[0] == "SetMessageType0"
    c.close()
    srv.close()
