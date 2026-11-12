"""Komut satırı: `bisttrader <komut>`.

  download          veriyi indir (scripts/download_data.py)
  backtest          iki botun backtesti + rapor (scripts/run_backtest.py)
  daily-live        günlük botu bugün için çalıştır (paper varsayılan)
  midterm-weekly    orta vade haftalık emir planı
  midterm-daily     orta vade günlük kontrol (stop, dilim tetikleri, KAP)
  midterm-record    Midas'ta gerçekleşen emri portföy dosyasına işle
  synth             test için sentetik veri üret
"""
from __future__ import annotations

import argparse
import logging
import runpy
import sys

from .config import ROOT, get, load_config


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="bisttrader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download")
    sub.add_parser("backtest")
    dl = sub.add_parser("daily-live")
    dl.add_argument("--equity", type=float, default=500_000)
    dl.add_argument("--live", action="store_true", help="gerçek emir (config: mode=live, realtime_confirmed=true)")
    for name in ("midterm-weekly", "midterm-daily"):
        p = sub.add_parser(name)
        p.add_argument("--refresh", action="store_true", help="önce veriyi güncelle")
        p.add_argument("--no-send", action="store_true")
    rec = sub.add_parser("midterm-record")
    rec.add_argument("--sleeve", choices=["bist", "foreign", "crypto"], required=True)
    rec.add_argument("--symbol", required=True)
    rec.add_argument("--side", choices=["buy", "sell"], required=True)
    rec.add_argument("--qty", type=float, required=True)
    rec.add_argument("--price", type=float, required=True)
    rec.add_argument("--fx", type=float, default=1.0)
    rec.add_argument("--tranche")
    rec.add_argument("--target-value", type=float)
    sy = sub.add_parser("synth")
    sy.add_argument("out", nargs="?", default="data_synth")
    a, rest = ap.parse_known_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    if a.cmd in ("download", "backtest"):
        script = ROOT / "scripts" / ("download_data.py" if a.cmd == "download" else "run_backtest.py")
        sys.argv = [str(script), *rest]
        runpy.run_path(str(script), run_name="__main__")
    elif a.cmd == "daily-live":
        from .costs import CostModel
        from .daily.live import LiveDailyBot, TradingViewFeed, run_day
        from .execution.matriks import MatriksIQBroker, MatriksIQClient
        from .execution.paper import PaperBroker
        cfg = load_config("daily_bot")
        costs = CostModel(scenario=get(cfg, "cost_scenario"))
        feed = TradingViewFeed("5m")
        live = a.live and get(cfg, "mode") == "live" and get(cfg, "data.realtime_confirmed", False) and feed.realtime
        if a.live and not live:
            print("Canlı mod koşulları sağlanmadı (mode=live, data.realtime_confirmed=true, gerçek zamanlı veri). Paper ile devam.")
        if get(cfg, "broker") == "matriks_iq" and live:
            c = MatriksIQClient(get(cfg, "matriks.host", "127.0.0.1"), get(cfg, "matriks.port", 18890))
            c.connect()
            broker = MatriksIQBroker(c, get(cfg, "matriks.brokage_id"), get(cfg, "matriks.account_id"), dry_run=False)
            broker.discover_account()
        else:
            broker = PaperBroker(costs, a.equity)
            orig = feed.last

            def last_and_match(sym, _o=orig, _b=broker):
                px = _o(sym)
                if px is not None:
                    _b.on_quote(sym, px)
                return px
            feed.last = last_and_match
        run_day(LiveDailyBot(cfg, costs, broker, feed, a.equity))
    elif a.cmd in ("midterm-weekly", "midterm-daily"):
        from .data.store import DataStore
        from .midterm.live import fetch_llm_events, load_state, plan
        cfg = load_config("midterm_bot")
        if a.refresh:
            script = ROOT / "scripts" / "download_data.py"
            sys.argv = [str(script), "--only", "daily,index,fund,global,macro"]
            runpy.run_path(str(script), run_name="__main__")
        events = None
        if get(cfg, "llm.enabled", False):
            st = load_state()
            events = fetch_llm_events(list(st["bist"]), cfg)
        plan(DataStore(), cfg, weekly=a.cmd == "midterm-weekly", send=not a.no_send, llm_events=events)
    elif a.cmd == "midterm-record":
        from .midterm.live import record_fill
        st = record_fill(a.sleeve, a.symbol.upper(), a.side, a.qty, a.price, a.fx, a.tranche, a.target_value)
        print(f"Kaydedildi. Nakit {st['cash_try']:,.0f} TL, USD {st['usd_cash']:,.2f}")
    elif a.cmd == "synth":
        from .data.synthetic import make_synthetic
        print(make_synthetic(a.out))


if __name__ == "__main__":
    main()
