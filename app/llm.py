"""LLM clients behind one tiny interface: ``complete(system, user) -> str``.

* ``OpenAICompatLLM`` talks to any OpenAI-compatible chat endpoint (Groq,
  OpenRouter, Gemini's OpenAI endpoint, a local vLLM ...).
* ``MockLLM`` is deterministic and offline. It builds test cases from the
  prompt's own source text and can be told to misbehave (``failures``) so the
  retry/validation path can be exercised without a network.
"""
from __future__ import annotations

import json
import re
import time
from typing import Protocol

import httpx


class LLMError(Exception):
    """Transport-level failure (timeout, 5xx, 429...). Counts as a failed attempt."""


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatLLM:
    provider = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0):
        if not api_key:
            raise ValueError("LLM_API_KEY is required for the openai_compat provider")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise LLMError(f"transport error: {exc}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise LLMError(f"provider returned {r.status_code}: {r.text[:200]}")
        if r.status_code >= 400:
            raise LLMError(f"provider rejected request {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"unexpected provider payload: {r.text[:300]}") from exc


SECTION_RE = re.compile(r'<section node_id="([^"]+)"[^>]*>\n(.*?)\n</section>', re.S)


class MockLLM:
    """Deterministic stand-in. ``failures`` is consumed one per call, e.g.
    ["malformed", "too_few", "bad_ids", "bad_quote", "prose_wrapped", "timeout"]."""

    provider = "mock"
    model = "mock-1"

    def __init__(self, failures: list[str] | None = None):
        self.failures = list(failures or [])
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        mode = self.failures.pop(0) if self.failures else "ok"
        cases = self._cases(user)
        if mode == "timeout":
            raise LLMError("simulated timeout")
        if mode == "malformed":
            return '{"test_cases": [{"title": "Overpressure", "steps": ["inflate"'  # truncated JSON
        if mode == "too_few":
            return json.dumps({"test_cases": cases[:1]})
        if mode == "bad_ids":
            for c in cases:
                c["source_node_ids"] = ["made-up:v9:9.9"]
            return json.dumps({"test_cases": cases})
        if mode == "bad_quote":
            for c in cases:
                c["requirement_quote"] = "The device shall reboot when the moon is full."
            return json.dumps({"test_cases": cases})
        if mode == "prose_wrapped":
            return "Sure! Here are your test cases:\n```json\n" + json.dumps({"test_cases": cases}) + "\n```"
        return json.dumps({"test_cases": cases})

    @staticmethod
    def _cases(user: str) -> list[dict]:
        sources = SECTION_RE.findall(user)
        candidates: list[tuple[str, str]] = []
        for nid, text in sources:
            units = [u.strip() for u in re.split(r"(?<=[.;])\s+|\n", text) if len(u.strip()) > 15]
            units.sort(key=lambda u: not re.search(r"\d", u))  # sentences with numbers first
            candidates += [(nid, u) for u in units]
        kinds = [("Verify nominal behaviour", "high"), ("Verify boundary condition", "high"),
                 ("Verify behaviour outside the specified condition", "medium")]
        cases = []
        i = 0
        while len(cases) < 4 and candidates:
            nid, quote = candidates[i % len(candidates)]
            kind, prio = kinds[len(cases) % len(kinds)]
            cases.append({
                "title": f"{kind}: {quote[:60]}",
                "objective": f"Confirm the device meets: {quote}",
                "preconditions": ["Device powered on with fresh batteries", "Standard cuff attached"],
                "steps": ["Set up the condition described in the requirement",
                          "Perform a measurement or trigger the behaviour", "Observe the display and device response"],
                "expected_result": f"Behaviour matches the requirement: {quote}",
                "priority": prio,
                "source_node_ids": [nid],
                "requirement_quote": quote,
            })
            i += 1
        return cases


def make_llm(settings) -> LLMClient:
    if settings.llm_provider == "mock":
        import os
        failures = [f for f in os.getenv("MOCK_LLM_FAILURES", "").split(",") if f]
        return MockLLM(failures)
    if settings.llm_provider == "openai_compat":
        return OpenAICompatLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    raise ValueError(f"unknown LLM_PROVIDER {settings.llm_provider!r}")


def backoff(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 8) * 0.25)
