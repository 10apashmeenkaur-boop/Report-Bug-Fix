"""Provider-agnostic JSON LLM client. Stdlib only (urllib), so nothing to install.

- Providers: gemini (default), openai (any OpenAI-compatible endpoint: OpenAI, Groq, OpenRouter), none.
- Every call is cached on disk by hash(provider, model, prompt id+version, messages) -> reruns are free
  and results are reproducible for the reviewers.
- Every call is traced to out/llm_trace.jsonl (prompt id, version, model, ms, cache hit, tokens if given).
- Simple client-side rate limit (free Gemini tier is ~10 requests/min) + retry with backoff on 429/5xx.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "llm"
TRACE = ROOT / "out" / "llm_trace.jsonl"


class LLMUnavailable(Exception):
    pass


class QuotaExhausted(LLMUnavailable):
    """Daily quota gone: stop calling instead of burning minutes on retries."""


STRONG_PROMPTS = {"SOLVE", "VERIFY"}  # maths-heavy steps may use a stronger model (LLM_MODEL_STRONG)


class LLM:
    def __init__(self, provider: str | None = None, model: str | None = None, rpm: int | None = None):
        self.provider = (provider or os.getenv("LLM_PROVIDER") or ("gemini" if os.getenv("GEMINI_API_KEY") else "none")).lower()
        defaults = {"gemini": "gemini-2.5-flash", "openai": "gpt-4o-mini", "none": "none"}
        self.model = model or os.getenv("LLM_MODEL") or defaults.get(self.provider, "none")
        self.rpm = rpm or int(os.getenv("LLM_RPM", "9" if self.provider == "gemini" else "60"))
        self.model_strong = os.getenv("LLM_MODEL_STRONG") or self.model
        self.reasoning_effort = os.getenv("LLM_REASONING_EFFORT")  # e.g. "low"/"medium" for gpt-oss on Groq
        self._last: dict[str, float] = {}
        self.dead: str | None = None  # set when a daily quota is exhausted
        self.calls = {"live": 0, "cached": 0, "failed": 0}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        TRACE.parent.mkdir(parents=True, exist_ok=True)

    @property
    def available(self) -> bool:
        return self.provider != "none"

    # ------------------------------------------------------------------ public
    def json(self, prompt_id: str, version: str, system: str, user: str, temperature: float = 0.0,
             model: str | None = None) -> dict:
        model = model or (self.model_strong if prompt_id in STRONG_PROMPTS else self.model)
        key = hashlib.sha256(json.dumps([self.provider, model, prompt_id, version, system, user, temperature],
                                        ensure_ascii=False).encode()).hexdigest()[:24]
        path = CACHE_DIR / f"{prompt_id}_{key}.json"
        if path.exists():
            self.calls["cached"] += 1
            data = json.loads(path.read_text(encoding="utf-8"))
            self._trace(prompt_id, version, model, 0, True, data.get("_usage"))
            return data["output"]
        if not self.available:
            raise LLMUnavailable("no LLM provider configured (set GEMINI_API_KEY)")
        if self.dead:
            raise QuotaExhausted(self.dead)
        t0 = time.time()
        text, usage = self._call(system, user, temperature, model)
        ms = int((time.time() - t0) * 1000)
        out = parse_json(text)
        path.write_text(json.dumps({"prompt_id": prompt_id, "version": version, "provider": self.provider,
                                    "model": model, "output": out, "raw": text, "_usage": usage},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        self.calls["live"] += 1
        self._trace(prompt_id, version, model, ms, False, usage)
        return out

    # ------------------------------------------------------------------ transport
    def _throttle(self, model: str):
        # one budget per model: on Groq/Gemini each model has its own rate limit
        gap = 60.0 / max(self.rpm, 1)
        wait = self._last.get(model, 0.0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last[model] = time.time()

    def _call(self, system: str, user: str, temperature: float, model: str) -> tuple[str, dict | None]:
        for attempt in range(6):
            self._throttle(model)
            try:
                if self.provider == "gemini":
                    return self._gemini(system, user, temperature, model)
                if self.provider == "openai":
                    return self._openai(system, user, temperature, model)
                raise LLMUnavailable(self.provider)
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="ignore")[:2000]
                if e.code == 429 and re.search(r"per ?day|PerDay|daily|RPD|tokens per day|TPD", body, re.I):
                    self.dead = f"daily quota exhausted for {model}: {body[:300]}"
                    self.calls["failed"] += 1
                    print(f"  !! DAILY QUOTA EXHAUSTED for {model}. Stopping AI calls; rerun later or switch key/model.")
                    raise QuotaExhausted(self.dead) from e
                if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                    ra = e.headers.get("retry-after") if e.headers else None
                    m = re.search(r"try again in ([\d.]+)s", body) or re.search(r"retry in ([\d.]+)s", body, re.I)
                    delay = float(ra) if ra and ra.replace(".", "").isdigit() else float(m.group(1)) if m else 4 * 2 ** attempt
                    print(f"  .. {e.code} from {model}, waiting {min(delay + 1, 90):.0f}s (attempt {attempt + 1}/6)")
                    time.sleep(min(delay + 1, 90))
                    continue
                self.calls["failed"] += 1
                raise RuntimeError(f"LLM HTTP {e.code}: {body}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 5:
                    time.sleep(3 * (attempt + 1))
                    continue
                self.calls["failed"] += 1
                raise RuntimeError(f"LLM network error: {e}") from e
        raise RuntimeError("LLM retries exhausted")

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "acadza-engine/1.0", **headers})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())

    def _gemini(self, system, user, temperature, model):
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMUnavailable("GEMINI_API_KEY not set")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "responseMimeType": "application/json"},
        }
        data = self._post(url, payload, {"x-goog-api-key": key})
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected Gemini response: {json.dumps(data)[:400]}") from e
        return text, data.get("usageMetadata")

    def _openai(self, system, user, temperature, model):
        key = os.environ.get("OPENAI_API_KEY")
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if not key:
            raise LLMUnavailable("OPENAI_API_KEY not set")
        payload = {"model": model, "temperature": temperature, "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.reasoning_effort and "gpt-oss" in model:
            payload["reasoning_effort"] = self.reasoning_effort
        data = self._post(f"{base}/chat/completions", payload, {"Authorization": f"Bearer {key}"})
        return data["choices"][0]["message"]["content"], data.get("usage")

    def _trace(self, pid, ver, model, ms, cached, usage):
        with TRACE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "prompt": pid, "version": ver,
                                "provider": self.provider, "model": model, "ms": ms, "cached": cached,
                                "usage": usage}) + "\n")


def parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in ``` fences or add a sentence. Be forgiving, then strict."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group())
        raise
