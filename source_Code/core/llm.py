"""Provider-agnostic LLM client with structured (Pydantic) outputs.

Works with any OpenAI-compatible endpoint (Groq, OpenAI, Together, vLLM,
Ollama `http://localhost:11434/v1`). Responsibilities:

* JSON outputs validated against a Pydantic model, with one automatic
  "repair" round-trip when the model returns malformed JSON.
* Robustness: timeouts, exponential backoff, HTTP 429 handling that honours
  `retry-after`, and rotation across a pool of fallback models (each model has
  its own rate-limit bucket on Groq).
* Client-side tokens-per-minute budgeting to avoid hammering 429s.
* Disk cache of responses (exact match) for reproducibility and cost savings.
* Token usage + latency reported to the active trace span.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from core.settings import Settings
from core.tracing import Span

logger = logging.getLogger("rfp.llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the LLM could not produce a usable answer after retries."""


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I | re.M)


def strip_reasoning(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    # Unclosed <think> (truncated reasoning) -> drop everything before the first '{'
    if "<think>" in text.lower():
        idx = text.find("{")
        text = text[idx:] if idx >= 0 else ""
    return text.strip()


def extract_json(text: str) -> Any:
    """Extract the first JSON object/array from an LLM response."""
    text = strip_reasoning(text)
    text = _FENCE_RE.sub("", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Find first balanced {...} block
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
        start = text.find("{", start + 1)
    raise ValueError("No valid JSON object found in model output")


def _estimate_tokens(text: str) -> int:
    return int(len(text) / 3.5) + 1


class _ModelBudget:
    """Sliding 60s window of token usage for one model."""

    def __init__(self, tpm: int):
        self.tpm = tpm
        self.events: deque[tuple[float, int]] = deque()
        self.blocked_until = 0.0

    def _prune(self, now: float) -> None:
        while self.events and now - self.events[0][0] > 60:
            self.events.popleft()

    def used(self, now: float) -> int:
        self._prune(now)
        return sum(t for _, t in self.events)

    def wait_time(self, needed: int, now: float) -> float:
        wait = max(0.0, self.blocked_until - now)
        if self.tpm <= 0:
            return wait
        self._prune(now)
        used = self.used(now)
        if used + needed <= self.tpm:
            return wait
        # time until enough tokens leave the window
        acc = used
        for ts, tok in self.events:
            acc -= tok
            if acc + needed <= self.tpm:
                return max(wait, 60 - (now - ts) + 0.2)
        return max(wait, 60.0)

    def record(self, tokens: int, now: float) -> None:
        self.events.append((now, tokens))


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.models = [settings.llm_model] + [m for m in settings.fallback_models if m != settings.llm_model]
        self._budgets = {m: _ModelBudget(settings.llm_tpm_limit) for m in self.models}
        self._lock = threading.Lock()
        self._client = None
        self.cache_dir = settings.path(settings.cache_dir) / "llm"
        if settings.llm_cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            if not self.settings.llm_api_key:
                raise LLMError("No LLM API key configured (set GROQ_API_KEY / LLM_API_KEY in .env)")
            self._client = OpenAI(
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key,
                timeout=self.settings.llm_timeout,
                max_retries=0,  # we handle retries / model rotation ourselves
            )
        return self._client

    @property
    def available(self) -> bool:
        return bool(self.settings.llm_api_key)

    # ------------------------------------------------------------------
    def _cache_key(self, messages: list[dict], json_mode: bool, max_tokens: int) -> str:
        payload = json.dumps(
            {"model": self.models[0], "messages": messages, "json": json_mode, "max_tokens": max_tokens},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        if not self.settings.llm_cache_enabled:
            return None
        f = self.cache_dir / f"{key}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _cache_put(self, key: str, value: dict) -> None:
        if not self.settings.llm_cache_enabled:
            return
        try:
            (self.cache_dir / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            logger.debug("cache write failed: %s", exc)

    # ------------------------------------------------------------------
    def _pick_model(self, needed: int, prefer_primary: bool = False) -> tuple[str, float]:
        """Return (model, seconds_to_wait) - prefer the primary model, rotate on pressure.

        Quality-critical calls (`prefer_primary`) wait for the primary model when the wait is short,
        because smaller fallback models produce noticeably weaker extractions."""
        now = time.time()
        with self._lock:
            waits = [(self._budgets[m].wait_time(needed, now), i, m) for i, m in enumerate(self.models)]
        if prefer_primary and waits[0][0] <= self.settings.llm_primary_max_wait:
            return waits[0][2], waits[0][0]
        # first model that is free right now, in priority order
        for w, _, m in waits:
            if w <= 0:
                return m, 0.0
        w, _, m = min(waits)
        return m, w

    @staticmethod
    def _retry_after(exc: Exception) -> float:
        try:
            resp = getattr(exc, "response", None)
            if resp is not None:
                ra = resp.headers.get("retry-after")
                if ra:
                    return float(ra)
        except Exception:
            pass
        m = re.search(r"try again in ([\d.]+)(ms|s)", str(exc))
        if m:
            v = float(m.group(1))
            return v / 1000 if m.group(2) == "ms" else v
        return 5.0

    def _complete(self, messages: list[dict], json_mode: bool, max_tokens: int,
                  span: Span | None, prefer_primary: bool = False) -> str:
        """Raw completion with retries, rotation and budgeting. Returns content text."""
        key = self._cache_key(messages, json_mode, max_tokens)
        cached = self._cache_get(key)
        if cached is not None:
            if span:
                span.llm_call(cached.get("model", "cache"), 0, 0, 0.0, cached=True)
            return cached["content"]

        from openai import APIConnectionError, APIStatusError, APITimeoutError, BadRequestError, RateLimitError

        prompt_text = "".join(m["content"] for m in messages)
        needed = _estimate_tokens(prompt_text) + min(max_tokens, 1200)
        last_exc: Exception | None = None
        use_json = json_mode
        for attempt in range(1, self.settings.llm_max_attempts + 1):
            model, wait = self._pick_model(needed, prefer_primary and attempt <= 3)
            if wait > 0:
                logger.info("LLM budget: waiting %.1fs for %s", wait, model)
                time.sleep(min(wait, 65))
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": self.settings.llm_temperature,
                "max_tokens": max_tokens,
            }
            if use_json:
                kwargs["response_format"] = {"type": "json_object"}
            if "gpt-oss" in model and self.settings.llm_reasoning_effort:
                kwargs["extra_body"] = {"reasoning_effort": self.settings.llm_reasoning_effort}
            t0 = time.perf_counter()
            try:
                resp = self.client.chat.completions.create(**kwargs)
                latency = (time.perf_counter() - t0) * 1000
                usage = resp.usage
                p_tok = getattr(usage, "prompt_tokens", 0) or 0
                c_tok = getattr(usage, "completion_tokens", 0) or 0
                with self._lock:
                    self._budgets[model].record(p_tok + c_tok, time.time())
                if span:
                    span.llm_call(model, p_tok, c_tok, latency, attempt=attempt)
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    raise LLMError("empty completion")
                if model == self.models[0]:  # only cache primary-model answers (quality guard)
                    self._cache_put(key, {"model": model, "content": content})
                return content
            except RateLimitError as exc:
                last_exc = exc
                ra = self._retry_after(exc)
                with self._lock:
                    self._budgets[model].blocked_until = time.time() + ra
                logger.warning("429 from %s (retry in %.1fs), attempt %d", model, ra, attempt)
            except BadRequestError as exc:
                last_exc = exc
                msg = str(exc)
                if "json_validate_failed" in msg or "response_format" in msg:
                    logger.warning("JSON mode rejected by %s; retrying without json mode", model)
                    use_json = False
                    continue
                if "model" in msg and ("decommission" in msg or "not found" in msg or "does not exist" in msg):
                    with self._lock:
                        self._budgets[model].blocked_until = time.time() + 3600
                    continue
                raise LLMError(f"Bad request: {msg[:300]}") from exc
            except (APITimeoutError, APIConnectionError) as exc:
                last_exc = exc
                logger.warning("LLM timeout/connection error on %s attempt %d: %s", model, attempt, exc)
                time.sleep(min(2 ** attempt, 20))
            except APIStatusError as exc:
                last_exc = exc
                if exc.status_code == 413:  # request too large for model budget
                    raise LLMError(f"Request too large: {str(exc)[:200]}") from exc
                logger.warning("LLM API error %s on %s attempt %d", exc.status_code, model, attempt)
                time.sleep(min(2 ** attempt, 20))
            except LLMError as exc:
                last_exc = exc
                time.sleep(1)
        raise LLMError(f"LLM failed after {self.settings.llm_max_attempts} attempts: {last_exc}")

    # ------------------------------------------------------------------
    def chat_json(self, system: str, user: str, schema: type[T], span: Span | None = None,
                  max_tokens: int | None = None, prefer_primary: bool = False) -> T:
        """Call the LLM and return a validated instance of `schema`."""
        max_tokens = max_tokens or self.settings.llm_max_output_tokens
        system = system.rstrip() + "\nRespond with a single valid JSON object only."
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        content = self._complete(messages, self.settings.llm_json_mode, max_tokens, span, prefer_primary)
        try:
            return schema.model_validate(extract_json(content))
        except (ValueError, ValidationError) as err:
            logger.warning("Invalid JSON from LLM (%s); attempting repair", str(err)[:200])
            repair = messages + [
                {"role": "assistant", "content": content[:4000]},
                {
                    "role": "user",
                    "content": (
                        f"Your previous output was invalid: {str(err)[:500]}\n"
                        f"Return ONLY a corrected JSON object matching this JSON schema:\n"
                        f"{json.dumps(schema.model_json_schema())[:3000]}"
                    ),
                },
            ]
            content2 = self._complete(repair, self.settings.llm_json_mode, max_tokens, span, prefer_primary)
            try:
                return schema.model_validate(extract_json(content2))
            except (ValueError, ValidationError) as err2:
                raise LLMError(f"Model returned invalid JSON twice: {str(err2)[:300]}") from err2

    def chat_text(self, system: str, user: str, span: Span | None = None, max_tokens: int | None = None) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return strip_reasoning(self._complete(messages, False, max_tokens or self.settings.llm_max_output_tokens, span))
