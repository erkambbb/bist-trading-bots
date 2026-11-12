"""LLM boru hattı (Claude API): KAP/haber olay sınıflandırma ve bilanço okuma.

- Önbellek: aynı metin bir kez analiz edilir (data/llm_cache.jsonl, sha256 anahtarlı).
- Prompt caching: sistem promptu `cache_control: ephemeral` ile önbelleğe alınır.
- Sayısal alanlar LLM'den alınmaz; LLM niteliksel bayrak ve olay yönü üretir.
- Ortam değişkeni: ANTHROPIC_API_KEY. Model adları config'ten (llm.filter_model / llm.analysis_model).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..config import DATA_DIR

EVENT_SYSTEM = """Sen Borsa İstanbul şirket bildirimlerini (KAP) ve finans haberlerini sınıflandıran bir analistsin.
Her metin için YALNIZCA şu JSON'u döndür:
{"relevant": bool, "event_type": one of ["geri_alim","iceriden_alim","yeni_is_iliskisi","bedelsiz","bedelli",
 "ana_ortak_satisi","kar_uyarisi","uretim_durmasi","dava","spk_yaptirim","denetim_gorusu_olumsuz",
 "hedef_fiyat_artisi","hedef_fiyat_dususu","kredi_notu","finansal_rapor","diger"],
 "direction": integer -3..+2, "importance": 0..1, "horizon_days": integer, "confidence": 0..1,
 "summary_tr": "tek cümle"}
Kurallar: SPK yaptırımı/manipülasyon/işlem yasağı → -3. Kâr uyarısı, üretim durması, büyük dava → -2.
Bedelli sermaye artırımı, ana ortak satışı → -1. Geri alım, içeriden alım, gelirin %5'inden büyük yeni iş → +1 veya +2.
Rutin bildirimler (genel kurul, özel durum güncellemesi vb.) → relevant=false, direction=0. Spekülasyon yapma."""

REPORT_SYSTEM = """Sen bir Türk şirketinin finansal raporunu ve faaliyet raporunu okuyan bir analistsin.
Sayıları HESAPLAMA; sadece metinde açıkça yazanları aktar. YALNIZCA şu JSON'u döndür:
{"one_offs": [str], "guidance": str|null, "red_flags": [str], "net_fx_position_try": number|null,
 "related_party_concern": bool, "audit_opinion": "olumlu"|"sartli"|"olumsuz"|"gorus_yok"|null,
 "quality_score": 0..1, "summary_tr": str, "confidence": 0..1}
Kırmızı bayrak örnekleri: ilişkili taraf alacaklarında artış, büyük açık döviz pozisyonu, süreklilik şüphesi,
tek seferlik kazançların net kârın büyük kısmı olması, denetçi şartlı görüşü."""


class LLMClient:
    def __init__(self, filter_model: str = "claude-haiku-5-5", analysis_model: str = "claude-sonnet-5-5",
                 cache_path: Path | None = None):
        self.filter_model, self.analysis_model = filter_model, analysis_model
        self.cache_path = cache_path or DATA_DIR / "llm_cache.jsonl"
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for ln in self.cache_path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(ln)
                    self.cache[r["key"]] = r["value"]
                except Exception:  # noqa: BLE001
                    continue
        self._client = None

    def _api(self):
        if self._client is None:
            import anthropic
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY tanımlı değil")
            self._client = anthropic.Anthropic()
        return self._client

    def _remember(self, key: str, value: dict) -> None:
        self.cache[key] = value
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n")

    def _ask(self, system: str, text: str, model: str, max_tokens: int = 600) -> dict:
        key = hashlib.sha256((model + system[:50] + text).encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        msg = self._api().messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": text[:60_000]}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = {"error": "json", "raw": raw[:500]}
        self._remember(key, val)
        return val

    def classify_event(self, ticker: str, title: str, body: str = "") -> dict:
        txt = f"Hisse: {ticker}\nBaşlık: {title}\nMetin: {body[:6000]}"
        return self._ask(EVENT_SYSTEM, txt, self.filter_model, 300)

    def analyze_report(self, ticker: str, period: str, text: str) -> dict:
        return self._ask(REPORT_SYSTEM, f"Hisse: {ticker}\nDönem: {period}\n\n{text}", self.analysis_model, 900)


def news_score(events: list[dict], asof, half_life_days: float = 10.0) -> float:
    """N = Σ yön × önem × 2^(-(t - t_e)/yarı_ömür)."""
    import pandas as pd
    tot = 0.0
    for e in events:
        if not e.get("relevant", True):
            continue
        age = (pd.Timestamp(asof) - pd.Timestamp(e["date"])).days
        tot += float(e.get("direction", 0)) * float(e.get("importance", 0.5)) * 2 ** (-max(age, 0) / half_life_days)
    return tot
