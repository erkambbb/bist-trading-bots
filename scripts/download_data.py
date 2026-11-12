#!/usr/bin/env python3
"""Backtest verisini indirir ve `data/` altına Parquet olarak yazar.

Kendi bilgisayarınızda çalıştırın (TradingView, İş Yatırım, TEFAS, TCMB ve Yahoo'ya erişim gerekir):

    python3 -m venv .venv && . .venv/bin/activate
    pip install -e ".[data]"
    python scripts/download_data.py

Kaldığı yerden devam eder: her hisse `data/raw/` altında önbelleğe alınır. Baştan indirmek için
`--fresh`. Yalnızca bir kısmı için `--only daily,intraday,fund,global,macro`.

Kaynaklar: borsapy (TradingView, İş Yatırım, TEFAS, TCMB) ve yfinance. Bu veriler yalnızca kişisel
kullanım içindir; repoya eklemeyin (data/ zaten .gitignore'da).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"
START = "2017-06-01"

FALLBACK_XU100 = []  # boşsa borsapy'den alınır

GLOBAL_TICKERS = {
    "USDTRY": "USDTRY=X", "VIX": "^VIX", "SPX": "^GSPC", "SPY": "SPY", "QQQ": "QQQ",
    "XLK": "XLK", "XLF": "XLF", "XLE": "XLE", "XLV": "XLV", "XLI": "XLI", "XLY": "XLY",
    "XLP": "XLP", "XLU": "XLU", "XLB": "XLB", "XLRE": "XLRE", "XLC": "XLC", "SMH": "SMH",
    "BTC-USD": "BTC-USD", "ETH-USD": "ETH-USD",
}
INTRADAY_INTERVALS = ["1h", "15m", "5m"]
PERIODS = {"1h": ["2y", "1y"], "15m": ["2y", "1y", "6mo"], "5m": ["2y", "6mo", "3mo", "1mo"]}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def cached(name: str, fn, fresh: bool):
    p = RAW / name
    if p.exists() and not fresh:
        return pd.read_parquet(p)
    df = fn()
    if df is not None and not df.empty:
        p.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p)
    return df


def retry(fn, tries: int = 3, wait: float = 2.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(wait * (i + 1))
    raise last


def to_ist(idx) -> pd.DatetimeIndex:
    idx = pd.to_datetime(idx)
    return idx.tz_localize("Europe/Istanbul") if idx.tz is None else idx.tz_convert("Europe/Istanbul")


def norm_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={c: c.lower().replace(" ", "_") for c in df.columns})
    keep = [c for c in ("open", "high", "low", "close", "volume", "dividends", "stock_splits", "adj_close")
            if c in df.columns]
    return df[keep]


# --------------------------------------------------------------------------- #
def get_universe(bp) -> list[str]:
    syms = []
    try:
        syms = retry(lambda: bp.Index("XU100").component_symbols)
    except Exception as e:  # noqa: BLE001
        log(f"XU100 bileşenleri alınamadı: {e}")
    syms = sorted(set(syms or FALLBACK_XU100))
    if not syms:
        sys.exit("Evren boş. borsapy güncel mi? (pip install -U borsapy)")
    return syms


def dl_bist_daily(bp, syms, fresh):
    out = []
    for i, s in enumerate(syms, 1):
        def fetch():
            t = bp.Ticker(s)
            adj = norm_ohlcv(retry(lambda: t.history(start=START, interval="1d", actions=True)))
            raw = norm_ohlcv(retry(lambda: t.history(start=START, interval="1d", adjust=False)))
            adj["close_raw"] = raw["close"].reindex(adj.index)
            return adj
        try:
            df = cached(f"daily/{s}.parquet", fetch, fresh)
        except Exception as e:  # noqa: BLE001
            log(f"  {s} günlük HATA: {e}; yfinance deneniyor")
            try:
                import yfinance as yf
                y = yf.download(f"{s}.IS", start=START, auto_adjust=False, progress=False, actions=True)
                if isinstance(y.columns, pd.MultiIndex):
                    y.columns = y.columns.get_level_values(0)
                df = norm_ohlcv(y)
                df["close_raw"] = df["close"]
            except Exception as e2:  # noqa: BLE001
                log(f"  {s} yfinance da başarısız: {e2}")
                continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_convert("Europe/Istanbul").tz_localize(None)
        df.index = df.index.normalize()
        df["symbol"] = s
        df["dividend"] = df.pop("dividends") if "dividends" in df else 0.0
        df["split"] = df.pop("stock_splits") if "stock_splits" in df else 0.0
        out.append(df.reset_index(names="date"))
        if i % 10 == 0:
            log(f"  günlük {i}/{len(syms)}")
    res = pd.concat(out, ignore_index=True)
    res.to_parquet(DATA / "bist_daily.parquet", index=False)
    log(f"bist_daily: {res['symbol'].nunique()} hisse, {res['date'].min().date()} → {res['date'].max().date()}")


def dl_index(bp, fresh):
    rows, intr = [], {iv: [] for iv in INTRADAY_INTERVALS}
    for s in ("XU100", "XU030"):
        ix = bp.Index(s)
        d = cached(f"index/{s}_1d.parquet", lambda: norm_ohlcv(retry(lambda: ix.history(start=START, interval="1d"))), fresh)
        d = d.copy()
        d.index = pd.to_datetime(d.index)
        if d.index.tz is not None:
            d.index = d.index.tz_convert("Europe/Istanbul").tz_localize(None)
        d.index = d.index.normalize()
        d["symbol"] = s
        rows.append(d.reset_index(names="date"))
        if s != "XU100":
            continue
        for iv in INTRADAY_INTERVALS:
            try:
                def fx_ix():
                    for per in PERIODS[iv]:
                        try:
                            d = norm_ohlcv(retry(lambda: ix.history(period=per, interval=iv), tries=1))
                            if d is not None and not d.empty:
                                return d
                        except Exception:  # noqa: BLE001
                            continue
                    return pd.DataFrame()
                x = cached(f"index/{s}_{iv}.parquet", fx_ix, fresh)
                if x is None or x.empty:
                    continue
                x = x.copy()
                x["symbol"] = s
                intr[iv].append(x.reset_index(names="ts"))
            except Exception as e:  # noqa: BLE001
                log(f"  {s} {iv} HATA: {e}")
    pd.concat(rows, ignore_index=True).to_parquet(DATA / "index_daily.parquet", index=False)
    for iv, parts in intr.items():
        if parts:
            pd.concat(parts, ignore_index=True).to_parquet(DATA / f"index_intraday_{iv}.parquet", index=False)
    log("endeksler tamam")


def dl_intraday(bp, syms, fresh):
    cover = {}
    for iv in INTRADAY_INTERVALS:
        parts = []
        for i, s in enumerate(syms, 1):
            def fetch_progressive():
                last_err = None
                for per in PERIODS[iv]:
                    try:
                        d = norm_ohlcv(retry(lambda: bp.Ticker(s).history(period=per, interval=iv), tries=1))
                        if d is not None and not d.empty:
                            return d
                    except Exception as e:  # noqa: BLE001
                        last_err = e
                if last_err:
                    raise last_err
                return pd.DataFrame()
            try:
                df = cached(f"intraday_{iv}/{s}.parquet", fetch_progressive, fresh)
            except Exception as e:  # noqa: BLE001
                log(f"  {s} {iv} HATA: {e}")
                continue
            if df is None or df.empty:
                continue
            df = df.copy()
            df["symbol"] = s
            parts.append(df.reset_index(names="ts"))
            if i % 20 == 0:
                log(f"  {iv} {i}/{len(syms)}")
        if not parts:
            log(f"{iv}: veri yok")
            continue
        res = pd.concat(parts, ignore_index=True)
        res.to_parquet(DATA / f"bist_intraday_{iv}.parquet", index=False)
        first = pd.to_datetime(res["ts"]).min()
        cover[iv] = {"first": str(first), "last": str(pd.to_datetime(res["ts"]).max()), "symbols": int(res["symbol"].nunique())}
        log(f"{iv}: {cover[iv]}")
    # yfinance 1 saatlik yedek (730 gün)
    if "1h" not in cover or pd.Timestamp(cover["1h"]["first"]).tz_localize(None) > pd.Timestamp.now() - pd.Timedelta(days=600):
        log("TradingView 1 saatlik kapsama yetersiz; yfinance 730 gün deneniyor")
        try:
            import yfinance as yf
            parts = []
            for s in syms:
                y = yf.download(f"{s}.IS", period="730d", interval="1h", auto_adjust=False, progress=False)
                if y is None or y.empty:
                    continue
                if isinstance(y.columns, pd.MultiIndex):
                    y.columns = y.columns.get_level_values(0)
                y = norm_ohlcv(y)
                y.index = to_ist(y.index)
                y["symbol"] = s
                parts.append(y.reset_index(names="ts"))
            if parts:
                res = pd.concat(parts, ignore_index=True)
                res.to_parquet(DATA / "bist_intraday_1h.parquet", index=False)
                y = yf.download("XU100.IS", period="730d", interval="1h", auto_adjust=False, progress=False)
                if y is not None and not y.empty:
                    if isinstance(y.columns, pd.MultiIndex):
                        y.columns = y.columns.get_level_values(0)
                    y = norm_ohlcv(y)
                    y.index = to_ist(y.index)
                    y["symbol"] = "XU100"
                    y.reset_index(names="ts").to_parquet(DATA / "index_intraday_1h.parquet", index=False)
                cover["1h"] = {"first": str(res["ts"].min()), "last": str(res["ts"].max()),
                               "symbols": int(res["symbol"].nunique()), "source": "yfinance"}
                log(f"1h (yfinance): {cover['1h']}")
        except Exception as e:  # noqa: BLE001
            log(f"yfinance 1h HATA: {e}")
    return cover


def dl_fundamentals(bp, syms, fresh):
    rows, meta = [], []
    for i, s in enumerate(syms, 1):
        t = bp.Ticker(s)
        group_used = None
        for group in ("XI_29", "UFRS"):
            ok = False
            for stmt, fn in (("income_stmt", t.get_income_stmt), ("balance_sheet", t.get_balance_sheet),
                             ("cashflow", t.get_cashflow)):
                try:
                    df = cached(f"fund/{s}_{group}_{stmt}.parquet",
                                lambda: retry(lambda: fn(quarterly=True, financial_group=group, last_n="all"), tries=2),
                                fresh)
                except Exception:  # noqa: BLE001
                    continue
                if df is None or df.empty:
                    continue
                ok = True
                long = df.reset_index().melt(id_vars=df.index.name or "Item", var_name="period", value_name="value")
                long = long.rename(columns={df.index.name or "Item": "item"})
                long["symbol"], long["group"], long["statement"] = s, group, stmt
                long["value"] = pd.to_numeric(long["value"], errors="coerce")
                rows.append(long.dropna(subset=["value"]))
            if ok:
                group_used = group
                break
        sector, name = None, None
        try:
            info = t.info
            sector = info.get("sector") or info.get("sektor")
            name = info.get("longName") or info.get("name")
        except Exception:  # noqa: BLE001
            pass
        meta.append({"symbol": s, "name": name, "sector": sector, "group": group_used})
        if i % 10 == 0:
            log(f"  temel veri {i}/{len(syms)}")
    if rows:
        f = pd.concat(rows, ignore_index=True)[["symbol", "group", "statement", "item", "period", "value"]]
        f.to_parquet(DATA / "fundamentals.parquet", index=False)
        log(f"fundamentals: {f['symbol'].nunique()} hisse, {len(f)} satır, dönemler {sorted(f['period'].unique())[:2]}…{sorted(f['period'].unique())[-2:]}")
        pd.Series(sorted(f["item"].unique())).to_frame("item").to_parquet(DATA / "fundamental_items.parquet")
    pd.DataFrame(meta).to_parquet(DATA / "meta.parquet", index=False)


def dl_global(fresh):
    import yfinance as yf
    parts = []
    for name, tk in GLOBAL_TICKERS.items():
        def fetch():
            y = yf.download(tk, start=START, auto_adjust=True, progress=False)
            if isinstance(y.columns, pd.MultiIndex):
                y.columns = y.columns.get_level_values(0)
            return norm_ohlcv(y)
        try:
            df = cached(f"global/{name}.parquet", fetch, fresh)
        except Exception as e:  # noqa: BLE001
            log(f"  {name} HATA: {e}")
            continue
        if df is None or df.empty:
            continue
        df = df.copy()
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df["symbol"] = name
        parts.append(df.reset_index(names="date"))
    res = pd.concat(parts, ignore_index=True)
    res.to_parquet(DATA / "global_daily.parquet", index=False)
    log(f"global: {sorted(res['symbol'].unique())}")


def dl_macro(bp, fresh):
    # politika faizi
    try:
        h = retry(lambda: bp.TCMB().history("policy"))
        h = h.reset_index()
        col = "lending" if "lending" in h and h["lending"].notna().any() else "borrowing"
        pr = pd.DataFrame({"date": pd.to_datetime(h["date"]), "rate": pd.to_numeric(h[col], errors="coerce")}).dropna()
        pr.to_parquet(DATA / "policy_rate.parquet", index=False)
        log(f"politika faizi: {len(pr)} karar, son {pr.iloc[-1].to_dict()}")
    except Exception as e:  # noqa: BLE001
        log(f"politika faizi HATA: {e}")
    # enflasyon
    try:
        inf = bp.Inflation()
        t = retry(lambda: inf.tufe(start="2016-01-01"))
        u = retry(lambda: inf.ufe(start="2016-01-01"))
        def prep(df, pre):
            df = df.copy()
            df.index = pd.to_datetime(df.index)
            return df.rename(columns={"MonthlyInflation": f"{pre}_mom", "YearlyInflation": f"{pre}_yoy"})[[f"{pre}_mom", f"{pre}_yoy"]]
        m = prep(t, "tufe").join(prep(u, "ufe"), how="outer").sort_index()
        m.index = m.index.to_period("M").to_timestamp()
        m.reset_index(names="date").to_parquet(DATA / "inflation.parquet", index=False)
        log(f"enflasyon: {m.index.min().date()} → {m.index.max().date()}")
    except Exception as e:  # noqa: BLE001
        log(f"enflasyon HATA: {e}")
    # PPF
    try:
        best = None
        for q in ("para piyasası", "PARA PİYASASI"):
            res = retry(lambda: bp.search_funds(q, limit=50))
            for r in res or []:
                code = r.get("fund_code")
                try:
                    hist = retry(lambda: bp.Fund(code).history(period="5y"), tries=2)
                except Exception:  # noqa: BLE001
                    continue
                if hist is None or hist.empty:
                    continue
                span = (hist.index.max() - hist.index.min()).days
                if best is None or span > best[1]:
                    best = (code, span, hist, r.get("name"))
            if best:
                break
        if best:
            code, _, hist, name = best
            ppf = pd.DataFrame({"date": pd.to_datetime(hist.index).tz_localize(None) if hist.index.tz is not None else pd.to_datetime(hist.index),
                                "price": hist["Price"].values})
            ppf.to_parquet(DATA / "ppf.parquet", index=False)
            log(f"PPF: {code} {name} {ppf['date'].min().date()} → {ppf['date'].max().date()}")
            return {"ppf_code": code, "ppf_name": name}
    except Exception as e:  # noqa: BLE001
        log(f"PPF HATA: {e}")
    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--only", default="universe,daily,index,intraday,fund,global,macro")
    ap.add_argument("--limit", type=int, default=0, help="test için ilk N hisse")
    a = ap.parse_args()
    parts = set(a.only.split(","))
    DATA.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    import borsapy as bp

    log(f"borsapy {getattr(bp, '__version__', '?')}")
    syms = get_universe(bp)
    if a.limit:
        syms = syms[: a.limit]
    log(f"evren: {len(syms)} hisse")
    manifest = {"created": datetime.now().isoformat(timespec="seconds"), "symbols": syms,
                "universe_note": "Bugünkü XU100 bileşenleri (tarihsel bileşen listesi yok → survivorship yanlılığı)"}
    steps = [
        ("daily", lambda: dl_bist_daily(bp, syms, a.fresh)),
        ("index", lambda: dl_index(bp, a.fresh)),
        ("intraday", lambda: manifest.update({"intraday_coverage": dl_intraday(bp, syms, a.fresh)})),
        ("fund", lambda: dl_fundamentals(bp, syms, a.fresh)),
        ("global", lambda: dl_global(a.fresh)),
        ("macro", lambda: manifest.update(dl_macro(bp, a.fresh) or {})),
    ]
    for name, fn in steps:
        if name not in parts:
            continue
        log(f"== {name} ==")
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"{name} adımı HATA: {e}")
            traceback.print_exc()
        log(f"{name} süresi {time.time() - t0:.0f} sn")
    old = DATA / "manifest.json"
    if old.exists():
        prev = json.loads(old.read_text())
        prev.update(manifest)
        manifest = prev
    old.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    log("BİTTİ. data/ klasöründeki .parquet dosyaları ve manifest.json hazır.")


if __name__ == "__main__":
    main()
