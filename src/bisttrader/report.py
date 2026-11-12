"""Backtest raporları: PNG grafikler + Markdown özet + HTML (base64 gömülü grafikler)."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import fmt_num, fmt_pct


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"figure.dpi": 110, "font.size": 9, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25})
    return plt


PALETTE = ["#1f5fbf", "#d9822b", "#2e9e6b", "#8e5ac8", "#c43d3d", "#6b7280"]


def line_chart(series: dict[str, pd.Series], title: str, path: Path, ylabel: str = "TL",
               logy: bool = False) -> Path:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8.5, 3.6))
    for i, (name, s) in enumerate(series.items()):
        ax.plot(s.index, s.values, label=name, lw=1.8 if i == 0 else 1.2, color=PALETTE[i % len(PALETTE)])
    if logy:
        ax.set_yscale("log")
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.legend(frameon=False, fontsize=8, ncol=min(len(series), 4))
    ax.yaxis.set_major_formatter(lambda x, _: f"{x:,.0f}".replace(",", "."))
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path


def drawdown_chart(equity: pd.Series, title: str, path: Path) -> Path:
    plt = _plt()
    dd = equity / equity.cummax() - 1
    fig, ax = plt.subplots(figsize=(8.5, 2.2))
    ax.fill_between(dd.index, dd.values * 100, 0, color="#c43d3d", alpha=0.35, lw=0)
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold")
    ax.set_ylabel("%")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def bar_chart(labels: list[str], values: list[float], title: str, path: Path, pct: bool = True) -> Path:
    plt = _plt()
    fig, ax = plt.subplots(figsize=(8.5, 3.0))
    colors = ["#2e9e6b" if v >= 0 else "#c43d3d" for v in values]
    ax.bar(labels, [v * 100 if pct else v for v in values], color=colors)
    ax.set_title(title, loc="left", fontsize=10, fontweight="bold")
    if pct:
        ax.set_ylabel("%")
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def monthly_table(equity: pd.Series) -> pd.DataFrame:
    m = equity.resample("ME").last()
    r = m.pct_change()
    r.iloc[0] = m.iloc[0] / equity.iloc[0] - 1
    t = pd.DataFrame({"yil": r.index.year, "ay": r.index.month, "r": r.values})
    return t.pivot(index="yil", columns="ay", values="r")


def md_table(rows: list[list], header: list[str]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def to_html(md_text: str, images: dict[str, Path], title: str) -> str:
    """Basit Markdown → HTML (tablolar, başlıklar, resimler gömülü)."""
    import html as h
    lines = md_text.splitlines()
    out, in_table = [], False
    for ln in lines:
        if ln.startswith("|"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if all(set(c) <= {"-", ":"} for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{h.escape(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{h.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if ln.startswith("!["):
            name = ln[ln.index("(") + 1: ln.index(")")]
            p = images.get(Path(name).name)
            if p and p.exists():
                b64 = base64.b64encode(p.read_bytes()).decode()
                out.append(f'<img src="data:image/png;base64,{b64}" style="max-width:100%">')
            continue
        if ln.startswith("#"):
            lvl = len(ln) - len(ln.lstrip("#"))
            out.append(f"<h{lvl}>{h.escape(ln[lvl:].strip())}</h{lvl}>")
        elif ln.startswith("- "):
            out.append(f"<li>{h.escape(ln[2:])}</li>")
        elif ln.startswith("> "):
            out.append(f"<blockquote>{h.escape(ln[2:])}</blockquote>")
        elif ln.strip():
            out.append(f"<p>{h.escape(ln)}</p>")
    if in_table:
        out.append("</table>")
    css = ("body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#1f2328}"
           "table{border-collapse:collapse;margin:8px 0;font-size:13px}td,th{border:1px solid #d0d7de;padding:4px 8px;text-align:right}"
           "th{background:#f6f8fa}td:first-child,th:first-child{text-align:left}blockquote{color:#57606a;border-left:3px solid #d0d7de;margin:8px 0;padding-left:10px}")
    return f"<!doctype html><html lang='tr'><head><meta charset='utf-8'><title>{h.escape(title)}</title><style>{css}</style></head><body>{''.join(out)}</body></html>"


def summary_rows(name: str, s: dict, t: dict | None = None) -> list:
    return [name, fmt_num(s.get("end_equity", 0), 0), fmt_pct(s.get("total_return", 0)), fmt_pct(s.get("cagr", 0)),
            fmt_pct(s.get("max_dd", 0)), fmt_num(s.get("sharpe_vs_ppf", 0)),
            fmt_pct(s.get("excess_ret_vs_ppf", np.nan)),
            (t or {}).get("n_trades", "–")]


def dump_json(obj, path: Path) -> None:
    def conv(o):
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, (pd.Timestamp,)):
            return str(o)
        return str(o)
    def keys(o):
        if isinstance(o, dict):
            return {(k if isinstance(k, (str, int, float, bool)) or k is None else "/".join(map(str, k)) if isinstance(k, tuple) else str(k)): keys(v)
                    for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [keys(v) for v in o]
        if isinstance(o, pd.DataFrame):
            return o.to_dict(orient="records")
        return o
    path.write_text(json.dumps(keys(obj), indent=1, ensure_ascii=False, default=conv))
