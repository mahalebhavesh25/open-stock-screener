from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from app.core.config import Config
from app.engine.regime import Regime

LOG_LLM = logging.getLogger("engine.llm")

GEMINI_SYSTEM = """You are a risk reviewer on a systematic Indian small-cap swing desk.
You do NOT generate trade ideas. You review ideas the quant model has already produced and flag
qualitative risks the model cannot see.

For each candidate return an object with:
  symbol: str (echo exactly)
  adjust: number in [-10, 10]  -- score adjustment; be conservative, use 0 when unsure
  flags: array of short strings from
    [EXTENDED, THIN_LIQUIDITY, EARNINGS_RISK, NEWS_DRIVEN_SPIKE, LOW_QUALITY_BASE,
     SECTOR_CROWDING, CIRCUIT_RISK, PUMP_PATTERN, NONE]
  rationale: one sentence, under 25 words, specific and non-generic.

Rules:
- Penalise names that are already far extended from the 20-EMA, have deep sloppy bases,
  or show a vertical volume spike typical of an operator-driven move.
- Penalise crowding: if many candidates share a sector, flag SECTOR_CROWDING on the weaker ones.
- Never invent facts you were not given. Never output prices, quantities or stops.
- Return ONLY a JSON array. No markdown fences, no prose.
"""


class GeminiAnalyst:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        self.mode = None
        if not (cfg.use_gemini and cfg.gemini_api_key):
            LOG_LLM.info("Gemini off (use_gemini=%s, key %s)", cfg.use_gemini,
                         "set" if cfg.gemini_api_key else "missing")
            return
        try:
            from google import genai
            self.client = genai.Client(api_key=cfg.gemini_api_key)
            self.mode = "genai"
        except Exception:
            try:
                import google.generativeai as ggen
                ggen.configure(api_key=cfg.gemini_api_key)
                self.client = ggen.GenerativeModel(cfg.gemini_model)
                self.mode = "legacy"
            except Exception as e:
                LOG_LLM.warning("Gemini unavailable, running without it: %s", e)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _generate(self, prompt: str, system: str = "", temperature: float = 0.2) -> str:
        if not self.enabled:
            return ""
        t0 = time.time()
        LOG_LLM.info("Gemini request · model=%s · prompt %d chars", self.cfg.gemini_model, len(prompt))
        try:
            if self.mode == "genai":
                from google.genai import types
                gen_config = types.GenerateContentConfig(
                    temperature=temperature,
                    system_instruction=system or None,
                    response_mime_type="application/json" if system else "text/plain",
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
                )
                r = self.client.models.generate_content(
                    model=self.cfg.gemini_model,
                    contents=prompt,
                    config=gen_config,
                )
                out = getattr(r, "text", "") or ""
            else:
                r = self.client.generate_content(f"{system}\n\n{prompt}")
                out = getattr(r, "text", "") or ""
            LOG_LLM.info("Gemini reply · %d chars in %.1fs", len(out), time.time() - t0)
            return out
        except Exception as e:
            LOG_LLM.warning("Gemini call failed after %.1fs (continuing without): %s", time.time() - t0, e)
            return ""

    @staticmethod
    def _parse_json(text: str) -> Any:
        if not text:
            return None
        t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(t)
        except Exception:
            i, j = t.find("["), t.rfind("]")
            if 0 <= i < j:
                try:
                    return json.loads(t[i:j + 1])
                except Exception:
                    return None
            return None

    def review(self, cand: pd.DataFrame, regime: Regime,
               context: Optional[Dict[str, str]] = None, top_n: int = 12) -> pd.DataFrame:
        d = cand.copy()
        d["llm_adjust"] = 0.0
        d["llm_flags"] = ""
        d["llm_note"] = ""
        if not self.enabled or d.empty:
            return d

        cols = ["symbol", "sector", "strategy", "score", "ret_21", "ret_63", "rel_63",
                "dist_52w_high", "ext_atr", "atr_pct", "vol_ratio", "base_depth",
                "turnover_med20_cr", "adx", "rsi14"]
        sub = d.head(top_n)[[c for c in cols if c in d.columns]].round(2)
        payload = {
            "regime": regime.to_dict(),
            "sector_counts": sub["sector"].value_counts().to_dict() if "sector" in sub else {},
            "candidates": sub.to_dict("records"),
            "context": context or {},
        }
        raw = self._generate(json.dumps(payload, default=str), GEMINI_SYSTEM)
        parsed = self._parse_json(raw)
        if not isinstance(parsed, list):
            return d

        known = set(d["symbol"])
        for item in parsed:
            try:
                sym = str(item.get("symbol", ""))
                if sym not in known:
                    continue
                adj = float(item.get("adjust", 0))
                adj = float(np.clip(adj, -self.cfg.gemini_max_adjust, self.cfg.gemini_max_adjust))
                m = d["symbol"] == sym
                d.loc[m, "llm_adjust"] = adj
                d.loc[m, "llm_flags"] = ",".join(map(str, item.get("flags", [])))[:120]
                d.loc[m, "llm_note"] = str(item.get("rationale", ""))[:200]
            except Exception:
                continue
        d["score_quant"] = d["score"]
        d["score"] = np.clip(d["score"] + d["llm_adjust"], 0, 100)
        return d.sort_values("score", ascending=False)
