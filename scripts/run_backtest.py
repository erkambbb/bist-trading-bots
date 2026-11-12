#!/usr/bin/env python3
"""İki botun backtestini çalıştırır, rapor üretir.

    python scripts/run_backtest.py                      # data/ ile tam paket
    python scripts/run_backtest.py --quick              # hızlı (küçük ızgara)
    python scripts/run_backtest.py --bot daily          # sadece günlük bot

Çıktılar:
    reports/backtest_<tarih>.html      grafikler gömülü tek dosya
    docs/07-backtest-sonuclari.md      repoda paylaşılabilir özet (+ docs/img/*.png)
    reports/*.parquet                  işlemler ve özkaynak eğrileri
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bisttrader import report as R  # noqa: E402
from bisttrader.backtest_runner import daily_suite, midterm_suite  # noqa: E402
from bisttrader.data.store import DataStore  # noqa: E402
from bisttrader.metrics import fmt_num, fmt_pct  # noqa: E402


def P(x, nd=1):
    return fmt_pct(x, nd)


def N(x, nd=2):
    return fmt_num(x, nd)


def daily_md(d: dict, img: Path, equity: float) -> tuple[str, dict]:
    imgs = {}
    r0 = d["_default_run"]
    eq = r0["equity"]["equity"]
    ppf = (1 + r0["ppf"]).cumprod() * equity
    series = {"Günlük Bot (varsayılan)": eq, "PPF (net)": ppf}
    if "_no_sip_eq" in d:
        series["ORB, RVOL filtresiz"] = d["_no_sip_eq"]
    imgs["daily_equity.png"] = R.line_chart(series, "Günlük Bot — özkaynak (TL)", img / "daily_equity.png")
    imgs["daily_dd.png"] = R.drawdown_chart(eq, "Günlük Bot — düşüş (%)", img / "daily_dd.png")
    s, t = d["default"]["summary"], d["default"]["trades"]
    L = []
    L.append("## Günlük Bot (BIST100 gün içi)\n")
    cov = ", ".join(f"{k}: {v['days']} gün (ilk {v['first']})" for k, v in d["coverage"].items())
    L.append(f"> Veri çözünürlüğü: **{d['interval']}** barlar. Kapsama: {cov}. "
             "1 saatlik barda 5 dk açılış aralığı ve VWAP geri çekilme stratejisi uygulanamaz; açılış aralığı ilk saat, "
             "giriş son saati 14:00 alınır. Sonuç bu yüzden planın kaba bir yaklaşımıdır.\n")
    L.append("### Ön-kayıtlı varsayılan parametreler (optimizasyon yok)\n")
    L.append(R.md_table([
        ["Başlangıç → bitiş", f"{s['start']} → {s['end']}"],
        ["Bitiş özkaynağı", N(s["end_equity"], 0) + " TL"],
        ["Toplam getiri", P(s["total_return"])],
        ["Yıllık getiri (CAGR)", P(s["cagr"])],
        ["Aynı dönem PPF (net)", P(s.get("ppf_total_return", np.nan))],
        ["PPF'ye göre fazla getiri", P(s.get("excess_ret_vs_ppf", np.nan))],
        ["Sharpe (PPF'ye göre)", N(s["sharpe_vs_ppf"])],
        ["Max düşüş", P(s["max_dd"])],
        ["İşlem sayısı", t.get("n_trades", 0)],
        ["Kazanma oranı", P(t.get("win_rate", np.nan))],
        ["Ortalama R", N(t.get("avg_R", np.nan))],
        ["Profit factor", N(t.get("profit_factor", np.nan))],
        ["İşlem kârı (maliyet sonrası)", N(d["default"]["trading_pnl"], 0) + " TL"],
        ["%20 yedeğin PPF getirisi", N(d["default"]["reserve_income"], 0) + " TL"],
        ["Toplam komisyon", N(d["default"]["costs"], 0) + " TL"],
    ], ["Ölçüt", "Değer"]))
    L.append("\n![](img/daily_equity.png)\n\n![](img/daily_dd.png)\n")
    L.append(f"Rejim günleri: {d['default']['days_by_mode']}. Çıkış nedenleri: {d['default']['exit_reasons']}.\n")
    if "walk_forward" in d:
        w = d["walk_forward"]
        ws = w["summary"]
        L.append(f"### Walk-forward (6 ay eğitim → 2 ay test, {d['n_trials']} kombinasyon)\n")
        L.append(R.md_table([[f["oos"], P(f["oos_return"]), P(f["oos_ppf"]),
                              ", ".join(f"{k.split('.')[-1]}={v}" for k, v in f["params"].items())] for f in w["folds"]],
                            ["Test dönemi", "Bot", "PPF", "Seçilen parametreler"]))
        L.append(f"\nBirleşik test (OOS): toplam {P(ws['total_return'])}, PPF {P(ws.get('ppf_total_return'))}, "
                 f"Sharpe {N(ws['sharpe_vs_ppf'])}, max düşüş {P(ws['max_dd'])}, **Deflated Sharpe {N(ws.get('dsr'))}**, "
                 f"PPF'yi geçen katman {w['positive_folds']}/{len(w['folds'])}.\n")
    g = d["grid"].head(10)
    L.append("### Izgaranın en iyi 10 kombinasyonu (tüm dönem, bilgi amaçlı — seçim için kullanılmaz)\n")
    L.append(R.md_table([[r["selection.rvol_threshold"], r["selection.max_candidates.normal"], r["strategy_orb.stop.k_atr"],
                          r["strategy_orb.take_profit.mode"], r["regime.enabled"], P(r["total_return"]), N(r["sharpe"]),
                          P(r["max_dd"])] for _, r in g.iterrows()],
                        ["RVOL", "n_max", "k_atr", "TP modu", "rejim", "getiri", "Sharpe", "max DD"]))
    gp = d["grid"]
    L.append(f"\nIzgaranın {len(gp)} kombinasyonundan PPF'yi geçen: {int((gp['total_return'] > s.get('ppf_total_return', 0)).sum())}; "
             f"medyan getiri {P(gp['total_return'].median())}.\n")
    L.append("### Tabanlar ve stres testleri\n")
    rows = [["Varsayılan strateji", P(s["total_return"]), N(s["sharpe_vs_ppf"]), P(s["max_dd"])],
            ["Rastgele giriş (medyan, %d tohum)" % len(d["random"]["returns"]), P(d["random"]["median"]), "–", "–"],
            ["ORB, RVOL filtresiz", P(d["no_sip"]["total_return"]), N(d["no_sip"]["sharpe_vs_ppf"]), P(d["no_sip"]["max_dd"])]]
    for k, v in d["stress"].items():
        rows.append([k, P(v["total_return"]), N(v["sharpe_vs_ppf"]), P(v["max_dd"])])
    L.append(R.md_table(rows, ["Senaryo", "Toplam getiri", "Sharpe", "Max DD"]))
    L.append(f"\nRastgele girişe karşı p-değeri: **{N(d['random']['p_value'])}** "
             "(rastgele tohumların stratejiye eşit ya da daha iyi olma oranı).\n")
    bs = d.get("bootstrap") or {}
    if bs:
        L.append(f"Blok bootstrap (10 gün): CAGR %5/%50/%95 = {P(bs['cagr'][5])} / {P(bs['cagr'][50])} / {P(bs['cagr'][95])}; "
                 f"max düşüş %5 = {P(bs['max_dd'][5])}.\n")
    for iv, h in d.get("hires", {}).items():
        if "error" in h:
            L.append(f"- {iv} kontrolü çalışmadı: {h['error']}")
            continue
        hs = h["summary"]
        L.append(f"### Yüksek çözünürlük kontrolü ({iv}, {h['window']})\n")
        L.append(f"Tam plan (5 dk açılış aralığı + VWAP geri çekilme) bu kısa dönemde: toplam {P(hs['total_return'])}, "
                 f"PPF {P(hs.get('ppf_total_return'))}, işlem {h['trades'].get('n_trades')}, kazanma {P(h['trades'].get('win_rate', np.nan))}, "
                 f"profit factor {N(h['trades'].get('profit_factor', np.nan))}. Aynı dönemde {d['interval']} sürümü: "
                 f"{P(h['primary_same_window']) if h['primary_same_window'] is not None else '–'}. Strateji kırılımı: {h['by_strategy']}.\n")
    return "\n".join(L), imgs


def mid_md(m: dict, img: Path, equity: float) -> tuple[str, dict]:
    imgs = {}
    r = m["_default_run"]
    eq = r["equity"]
    b = r["bench"]
    series = {"Orta Vade Bot (varsayılan)": eq["equity"], "Kalibre": m["_calibrated_run"]["equity"]["equity"]}
    for k in b.columns:
        series[k] = b[k]
    imgs["mid_equity.png"] = R.line_chart(series, "Orta Vade Bot — özkaynak (TL)", img / "mid_equity.png")
    imgs["mid_dd.png"] = R.drawdown_chart(eq["equity"], "Orta Vade Bot — düşüş (%)", img / "mid_dd.png")
    plt = R._plt()
    fig, ax = plt.subplots(figsize=(8.5, 2.8))
    share = eq[["cash_try", "bist", "foreign", "crypto"]].div(eq["equity"], axis=0).clip(lower=0) * 100
    ax.stackplot(share.index, share.T.values, labels=["Nakit (PPF)", "BIST", "Yurt dışı", "Kripto"],
                 colors=["#9ca3af", "#1f5fbf", "#2e9e6b", "#d9822b"], alpha=0.85)
    ax.set_title("Orta Vade Bot — kol dağılımı (%)", loc="left", fontsize=10, fontweight="bold")
    ax.legend(frameon=False, fontsize=8, ncol=4, loc="upper left")
    ax.set_ylim(0, 100)
    fig.tight_layout()
    fig.savefig(img / "mid_sleeves.png")
    plt.close(fig)
    imgs["mid_sleeves.png"] = img / "mid_sleeves.png"
    s = m["default"]["summary"]
    c = m["calibrated"]["summary"]
    L = ["## Orta Vade Bot\n"]
    L.append(f"> Kalibrasyon {m['calibration_window'][0]} → {m['calibration_window'][1]}, test (OOS) {m['oos_window'][0]} → "
             f"{m['oos_window'][1]}. Analist ve haber/LLM bileşenleri backtestte kapalı (point-in-time veri yok, LLM look-ahead). "
             "Yurt dışı kolu: SPY/QQQ çekirdeği + sektör ETF momentumu. BIST evreni bugünkü XU100 (survivorship yanlılığı var).\n")
    rows = [R.summary_rows("Varsayılan (ön-kayıtlı)", s), R.summary_rows("Kalibre edilmiş", c)]
    for k, v in m["bench"].items():
        rows.append(R.summary_rows(k, v))
    L.append(R.md_table(rows, ["Portföy", "Bitiş TL", "Toplam", "CAGR", "Max DD", "Sharpe", "PPF'ye göre", "İşlem"]))
    L.append(f"\nDeflated Sharpe (varsayılan, {m['n_trials']} deneme): {N(s.get('dsr'))}. "
             f"Ödenen yurt dışı vergi: {N(m['default']['tax_paid'], 0)} TL; işlem/kur maliyeti: {N(m['default']['fees'], 0)} TL.\n")
    av = m["default"]["avg_sleeves"]
    L.append(f"Ortalama kol ağırlıkları: nakit {P(av['cash_try'])}, BIST {P(av['bist'])}, yurt dışı {P(av['foreign'])}, kripto {P(av['crypto'])}.\n")
    L.append("\n![](img/mid_equity.png)\n\n![](img/mid_sleeves.png)\n\n![](img/mid_dd.png)\n")
    L.append(f"BIST çıkış/giriş nedenleri: {m['default']['bist_reasons']}.\n")
    L.append("### Kalibrasyon ızgarası (2018-11 → 2024-11)\n")
    cal = m["calibration"]
    L.append(R.md_table([[x["weights"], x["buy_pct"], x["sell_pct"], x["tranches"], P(x["total_return"]), P(x["cagr"]),
                          N(x["sharpe"]), P(x["max_dd"])] for _, x in cal.iterrows()],
                        ["Ağırlıklar", "Alım", "Satım", "Dilim", "Toplam", "CAGR", "Sharpe", "Max DD"]))
    cd = m["calibration_default"]
    L.append(f"\nKalibrasyon döneminde varsayılan: toplam {P(cd['total_return'])}, PPF {P(cd.get('ppf_total_return'))}, Sharpe {N(cd['sharpe_vs_ppf'])}.\n")
    if m.get("ablations"):
        L.append("### Ablasyonlar (test dönemi)\n")
        L.append(R.md_table([[k, P(v["total_return"]), N(v["sharpe_vs_ppf"]), P(v["max_dd"])] for k, v in m["ablations"].items()],
                            ["Değişiklik", "Toplam", "Sharpe", "Max DD"]))
    return "\n".join(L), imgs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--equity", type=float, default=500_000)
    ap.add_argument("--bot", default="both", choices=["both", "daily", "midterm"])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--docs", default=str(ROOT / "docs"))
    ap.add_argument("--reports", default=str(ROOT / "reports"))
    a = ap.parse_args()
    store = DataStore(a.data)
    docs, reps = Path(a.docs), Path(a.reports)
    img = docs / "img"
    img.mkdir(parents=True, exist_ok=True)
    reps.mkdir(parents=True, exist_ok=True)
    man = store.manifest()
    parts = [f"# 07 · Backtest sonuçları\n",
             f"> Oluşturma: {date.today()} · Başlangıç sermayesi her bot için **{N(a.equity, 0)} TL** · "
             f"Maliyet senaryosu S1 (Midas + MatriksIQ, baz örtük maliyet) · PPF kaynağı: {store.ppf_source()}"
             + (" · **SENTETİK VERİ (gerçek sonuç değildir)**" if man.get("synthetic") else "") + "\n",
             "> Yatırım tavsiyesi değildir. Geçmiş performans gelecekteki sonuçları garanti etmez.\n"]
    images = {}
    if a.bot in ("both", "daily"):
        d = daily_suite(store, reps, equity=a.equity, quick=a.quick)
        md, im = daily_md(d, img, a.equity)
        parts.append(md)
        images.update(im)
        d["_default_run"]["trades"].to_parquet(reps / "daily_trades.parquet")
        d["_default_run"]["equity"].to_parquet(reps / "daily_equity.parquet")
        R.dump_json({k: v for k, v in d.items() if not k.startswith("_") and k != "grid"}, reps / "daily_summary.json")
        d["grid"].to_csv(reps / "daily_grid.csv", index=False)
    if a.bot in ("both", "midterm"):
        m = midterm_suite(store, reps, equity=a.equity, quick=a.quick)
        md, im = mid_md(m, img, a.equity)
        parts.append(md)
        images.update(im)
        m["_default_run"]["trades"].to_parquet(reps / "midterm_trades.parquet")
        m["_default_run"]["equity"].to_parquet(reps / "midterm_equity.parquet")
        R.dump_json({k: v for k, v in m.items() if not k.startswith("_") and k != "calibration"}, reps / "midterm_summary.json")
    text = "\n\n".join(parts)
    (docs / "07-backtest-sonuclari.md").write_text(text, encoding="utf-8")
    html = R.to_html(text, images, "Backtest sonuçları")
    out = reps / f"backtest_{date.today()}.html"
    out.write_text(html, encoding="utf-8")
    print(f"Rapor: {out}\nÖzet: {docs / '07-backtest-sonuclari.md'}")


if __name__ == "__main__":
    main()
