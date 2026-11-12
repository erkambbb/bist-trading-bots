"""Orta Vade Bot faktörleri (docs/04-orta-vade-bot.md §4).

Temel veri İş Yatırım mali tablolarından gelir (kalem adları Türkçe). Gelir tablosu ve
nakit akışı kümülatif (yıl başından) olduğu için çeyreklik değerlere çevrilir, TTM toplanır.
Yayın tarihi KAP tarihi bilinmiyorsa yasal sürelere göre kötümser bir gecikmeyle varsayılır.
"""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

# KAP yayın gecikmesi varsayımı (çeyrek sonundan gün): Q1 40, Q2 60, Q3 40, Q4 70 gün + tampon
PUBLISH_LAG_DAYS = {1: 50, 2: 70, 3: 50, 4: 80}


def _norm(s: str) -> str:
    s = s.lower().replace("ı", "i").replace("İ", "i")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


# (anahtar, tablo, regex listesi, birleşim)  birleşim: first | sum
ITEM_MAP = [
    ("revenue", "income_stmt", [r"^satis gelirleri$", r"^hasilat$"], "first"),
    ("gross_profit", "income_stmt", [r"^brut kar", r"ticari faaliyetlerden brut kar"], "first"),
    ("op_profit", "income_stmt", [r"^faaliyet kari", r"^esas faaliyet kari"], "first"),
    ("net_income", "income_stmt", [r"^ana ortaklik paylari", r"donem net kar", r"net donem kari"], "first"),
    ("da", "cashflow", [r"amortisman"], "first"),
    ("ocf", "cashflow", [r"isletme faaliyetlerinden .*nakit", r"isletme faaliyetlerinden"], "first"),
    ("assets", "balance_sheet", [r"^toplam varliklar", r"^aktif toplami", r"^toplam aktifler"], "first"),
    ("equity", "balance_sheet", [r"^ana ortakliga ait ozkaynaklar", r"^ozkaynaklar$", r"ozkaynaklar"], "first"),
    ("paid_in_capital", "balance_sheet", [r"^odenmis sermaye", r"^sermaye$"], "first"),
    ("cash", "balance_sheet", [r"^nakit ve nakit benzerleri"], "first"),
    ("fin_debt", "balance_sheet", [r"^finansal borclar", r"^kisa vadeli finansal borclar",
                                   r"^uzun vadeli finansal borclar"], "sum"),
]
FLOW_KEYS = {"revenue", "gross_profit", "op_profit", "net_income", "da", "ocf"}


def _period_to_ts(p: str) -> tuple[int, int, pd.Timestamp]:
    y, q = int(p[:4]), int(p.split("Q")[1])
    end = pd.Timestamp(year=y, month=q * 3, day=1) + pd.offsets.MonthEnd(0)
    return y, q, end


def tidy_fundamentals(fund: pd.DataFrame, earnings: pd.DataFrame | None = None) -> pd.DataFrame:
    """Uzun formatlı ham tabloyu (symbol, period) × anahtar geniş tabloya çevirir.

    Dönüş sütunları: symbol, period, year, q, period_end, publish_date, <anahtar>_q (çeyreklik akış),
    <anahtar>_ttm, stok kalemleri.
    """
    if fund is None or fund.empty:
        return pd.DataFrame()
    f = fund.copy()
    f = f[f["period"].astype(str).str.contains("Q")]
    f["n"] = f["item"].astype(str).map(_norm)
    recs = []
    for key, stmt, pats, how in ITEM_MAP:
        sub = f[f["statement"] == stmt]
        hit = None
        for p in pats:
            m = sub[sub["n"].str.contains(p, regex=True)]
            if not m.empty:
                hit = m
                break
        if hit is None:
            continue
        if how == "sum":
            agg = hit.groupby(["symbol", "period"])["value"].sum(min_count=1)
        else:
            agg = hit.groupby(["symbol", "period"])["value"].first()
        recs.append(agg.rename(key))
    if not recs:
        return pd.DataFrame()
    w = pd.concat(recs, axis=1).reset_index()
    parts = w["period"].map(_period_to_ts)
    w["year"] = [p[0] for p in parts]
    w["q"] = [p[1] for p in parts]
    w["period_end"] = [p[2] for p in parts]
    w = w.sort_values(["symbol", "period_end"]).reset_index(drop=True)
    # kümülatif → çeyreklik
    for k in FLOW_KEYS:
        if k not in w:
            continue
        prev = w.groupby(["symbol", "year"])[k].shift(1)
        w[f"{k}_q"] = np.where(w["q"] == 1, w[k], w[k] - prev)
        w[f"{k}_ttm"] = w.groupby("symbol")[f"{k}_q"].transform(lambda s: s.rolling(4, min_periods=4).sum())
    # yayın tarihi
    w["publish_date"] = w["period_end"] + w["q"].map(lambda q: pd.Timedelta(days=PUBLISH_LAG_DAYS[q]))
    if earnings is not None and not earnings.empty:
        e = earnings.sort_values("date")
        for i, row in w.iterrows():
            ed = e[(e["symbol"] == row["symbol"]) & (e["date"] > row["period_end"]) &
                   (e["date"] <= row["period_end"] + pd.Timedelta(days=100))]
            if not ed.empty:
                w.at[i, "publish_date"] = ed["date"].iloc[0] + pd.Timedelta(days=1)
    # FAVÖK ve SUE
    if "op_profit_q" in w:
        da_q = w["da_q"] if "da_q" in w else 0.0
        w["ebitda_q"] = w["op_profit_q"] + (da_q.fillna(0) if isinstance(da_q, pd.Series) else 0.0)
        w["ebitda_ttm"] = w.groupby("symbol")["ebitda_q"].transform(lambda s: s.rolling(4, min_periods=4).sum())
        diff = w["ebitda_q"] - w.groupby("symbol")["ebitda_q"].shift(4)
        sd = diff.groupby(w["symbol"]).transform(lambda s: s.shift(1).rolling(8, min_periods=4).std())
        w["sue"] = diff / sd.replace(0, np.nan)
    # sermaye artışı (bedelli/bedelsiz ayrımı motorda bölünme verisiyle yapılır)
    if "paid_in_capital" in w:
        w["cap_growth_2q"] = w["paid_in_capital"] / w.groupby("symbol")["paid_in_capital"].shift(2) - 1
    return w


def point_in_time(tidy: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """asof tarihinde yayımlanmış son çeyrek (her hisse için)."""
    if tidy.empty:
        return tidy
    t = tidy[tidy["publish_date"] <= asof]
    if t.empty:
        return t
    return t.sort_values("period_end").groupby("symbol").tail(1).set_index("symbol")


def zscore(s: pd.Series, groups: pd.Series | None, clip: float = 3.0) -> pd.Series:
    s = s.replace([np.inf, -np.inf], np.nan)
    # önce aykırıları yüzdelikten kırp
    lo, hi = s.quantile(0.02), s.quantile(0.98)
    s = s.clip(lo, hi)

    def _z(x):
        sd = x.std(ddof=0)
        return (x - x.mean()) / sd if sd and sd > 0 else x * 0.0

    if groups is not None:
        g = groups.reindex(s.index).fillna("NA")
        counts = g.map(g.value_counts())
        z_sec = s.groupby(g).transform(_z)
        z_all = _z(s)
        z = z_sec.where(counts >= 4, z_all)
    else:
        z = _z(s)
    return z.clip(-clip, clip)


def compute_scores(asof: pd.Timestamp, close_adj: pd.DataFrame, close_raw: pd.DataFrame,
                   index_close: pd.Series, tidy: pd.DataFrame, sectors: pd.Series | None,
                   weights: dict, universe: list[str]) -> pd.DataFrame:
    """asof itibarıyla kompozit skor tablosu. Sadece asof ve öncesi veri kullanılır."""
    px = close_adj.loc[:asof, [s for s in universe if s in close_adj.columns]]
    if len(px) < 260:
        return pd.DataFrame()
    last = px.iloc[-1]
    p21, p126, p252 = px.iloc[-22], px.iloc[-127], px.iloc[-253]
    out = pd.DataFrame(index=px.columns)
    out["price"] = last
    out["mom_12_1"] = p21 / p252 - 1
    out["mom_6_1"] = p21 / p126 - 1
    ix = index_close.loc[:asof]
    ix_ret6 = ix.iloc[-1] / ix.iloc[-127] - 1 if len(ix) > 127 else 0.0
    out["rs_6m"] = (last / p126 - 1) - ix_ret6
    out["hi52"] = last / px.iloc[-252:].max()
    out["vol60"] = px.pct_change().iloc[-60:].std() * np.sqrt(252)

    pit = point_in_time(tidy, asof) if tidy is not None and not tidy.empty else pd.DataFrame()
    have_f = not pit.empty
    if have_f:
        pit = pit.reindex(out.index)
        raw = close_raw.loc[:asof].iloc[-1].reindex(out.index) if not close_raw.empty else last
        shares = pit.get("paid_in_capital")
        mcap = raw * shares if shares is not None else np.nan
        net_debt = pit.get("fin_debt", 0).fillna(0) - pit.get("cash", 0).fillna(0)
        ev = mcap + net_debt
        ebitda = pit.get("ebitda_ttm")
        out["mcap"] = mcap
        out["ev"] = ev
        out["ebitda_ttm"] = ebitda
        out["net_debt"] = net_debt
        out["shares"] = shares
        out["ey_ebitda"] = ebitda / ev
        out["sales_ev"] = pit.get("revenue_ttm") / ev
        out["bp"] = pit.get("equity") / mcap
        out["gp_assets"] = pit.get("gross_profit_ttm") / pit.get("assets")
        out["roe"] = pit.get("net_income_ttm") / pit.get("equity")
        out["nd_ebitda_inv"] = -(net_debt / ebitda.where(ebitda > 0))
        out["ocf_ebitda"] = pit.get("ocf_ttm") / ebitda.where(ebitda > 0)
        out["sue"] = pit.get("sue")
        out["cap_growth_2q"] = pit.get("cap_growth_2q")
    sec = sectors.reindex(out.index) if sectors is not None else None

    comp = {}
    comp["momentum"] = pd.concat([zscore(out[c], None) for c in ("mom_12_1", "mom_6_1", "rs_6m", "hi52")], axis=1).mean(axis=1)
    if have_f:
        comp["value"] = pd.concat([zscore(out[c], sec) for c in ("ey_ebitda", "sales_ev", "bp")], axis=1).mean(axis=1)
        comp["quality"] = pd.concat([zscore(out[c], sec) for c in ("gp_assets", "roe", "nd_ebitda_inv", "ocf_ebitda")], axis=1).mean(axis=1)
        comp["earnings_surprise"] = zscore(out["sue"], None)
    used = {k: float(weights.get(k, 0)) for k in comp if float(weights.get(k, 0)) > 0}
    tot = sum(used.values()) or 1.0
    score = sum(comp[k].fillna(0) * (w / tot) for k, w in used.items())
    for k, v in comp.items():
        out[f"z_{k}"] = v
    out["score"] = score
    out["fund_coverage"] = float(have_f and out["ebitda_ttm"].notna().mean())
    # fundamentali eksik hisselerin değer/kalite bileşeni 0 sayılır; tamamen eksikse cezalandır
    if have_f:
        miss = out["ebitda_ttm"].isna() & out["bp"].isna()
        out.loc[miss, "score"] -= 0.25
    out["rank_pct"] = out["score"].rank(ascending=False, pct=True)
    return out


def fair_value_upside(tbl: pd.DataFrame, sectors: pd.Series | None) -> pd.Series:
    """Sektör medyan FD/FAVÖK ile adil değer → potansiyel (konsensüs backtestte yok)."""
    if "ev" not in tbl or tbl["ebitda_ttm"].isna().all():
        return pd.Series(np.nan, index=tbl.index)
    mult = (tbl["ev"] / tbl["ebitda_ttm"].where(tbl["ebitda_ttm"] > 0))
    mult = mult.where((mult > 0) & (mult < 60))
    if sectors is not None:
        sec = sectors.reindex(tbl.index).fillna("NA")
        med = mult.groupby(sec).transform("median")
        med = med.fillna(mult.median())
    else:
        med = pd.Series(mult.median(), index=tbl.index)
    fv_equity = med * tbl["ebitda_ttm"] - tbl["net_debt"]
    fv_ps = fv_equity / tbl["shares"]
    raw_price = tbl["mcap"] / tbl["shares"]
    return fv_ps / raw_price - 1
