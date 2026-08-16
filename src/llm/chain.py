"""Ordered LLM fallback chain (comm-assistant style) via OpenAI-compatible HTTP.

Last step is always ``template`` (handled by the caller / returned as sentinel).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from src.config import get_env

logger = logging.getLogger(__name__)

TEMPLATE_SENTINEL = "__TEMPLATE__"

_KNOWN_PROVIDERS = {
    "gemini",
    "groq",
    "openrouter",
    "openai",
    "openai_compatible",
    "compatible",
    "ollama",
    "template",
    "heuristic",
    "default",
    "local",
}


def _env(*names: str, default: str = "") -> str:
    for name in names:
        val = (os.getenv(name) or "").strip()
        if val:
            return val
    return default


def _split_csv(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _parse_endpoint_spec(spec: str) -> tuple[str, str]:
    s = (spec or "").strip()
    if not s:
        return "template", "template"
    lower = s.lower()
    if lower in {"template", "heuristic", "default", "local"}:
        return "template", "template"
    if "/" in s:
        provider, _, model = s.partition("/")
        return provider.strip().lower(), (model.strip() or "")
    if ":" in s:
        left, _, right = s.partition(":")
        if left.strip().lower() in _KNOWN_PROVIDERS:
            return left.strip().lower(), right.strip()
    return s.lower(), ""


def default_model(provider: str) -> str:
    if provider == "gemini":
        return "gemini-flash-lite-latest"
    if provider == "groq":
        # Groq shut down llama-3.1-8b-instant / llama-3.3-70b-versatile (2026-08-16).
        return "openai/gpt-oss-20b"
    if provider == "openrouter":
        return "meta-llama/llama-3.3-70b-instruct:free"
    if provider in {"openai", "openai_compatible", "compatible"}:
        return "gpt-4o-mini"
    if provider == "ollama":
        return _env("OLLAMA_MODEL", default="qwen3:8b") or "qwen3:8b"
    return "template"


# Free/dev-tier Groq IDs shut down 2026-08-16 → official replacements.
_GROQ_MODEL_ALIASES = {
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama3-8b-8192": "openai/gpt-oss-20b",
    "llama3-70b-8192": "openai/gpt-oss-120b",
    "qwen/qwen3-32b": "openai/gpt-oss-120b",
}


def _canonicalize_model(provider: str, model: str) -> str:
    model = (model or "").strip()
    if provider != "groq" or not model:
        return model
    return _GROQ_MODEL_ALIASES.get(model, model)


def parse_llm_chain_specs(*, yaml_model: str | None = None) -> list[tuple[str, str]]:
    """
    Parse LLM chain env. Always ends with ``template``.
    If no env chain is set, default to groq/(yaml_model or openai/gpt-oss-20b).
    """
    chain_raw = _env("LLM_CHAIN")
    if chain_raw:
        specs = [_parse_endpoint_spec(s) for s in _split_csv(chain_raw)]
    else:
        primary_provider = (_env("LLM_PROVIDER") or "groq").lower()
        primary_model = (
            _env("LLM_MODEL")
            or (yaml_model or "").strip()
            or default_model(primary_provider)
        )
        specs = [(primary_provider, primary_model)]
        for item in _split_csv(_env("LLM_FALLBACKS")):
            specs.append(_parse_endpoint_spec(item))

    remote: list[tuple[str, str]] = []
    ollama_model = default_model("ollama")
    include_ollama = bool(_env("OLLAMA_BASE_URL") or _env("OLLAMA_MODEL"))

    for provider, model in specs:
        provider = provider.lower().strip()
        if provider in {"template", "heuristic", "default", "local"}:
            continue
        if provider == "ollama":
            include_ollama = True
            if (model or "").strip():
                ollama_model = model.strip()
            continue
        model = _canonicalize_model(
            provider, (model or default_model(provider)).strip()
        )
        if remote and remote[-1] == (provider, model):
            continue
        remote.append((provider, model))

    out = list(remote)
    if include_ollama:
        out.append(("ollama", ollama_model))
    out.append(("template", "template"))
    return out


@dataclass
class LLMEndpoint:
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""

    @property
    def label(self) -> str:
        if self.provider == "template":
            return "template"
        return f"{self.provider}/{self.model or 'default'}"


class LLMChain:
    """Try cloud/local chat models in order; caller treats placeholders as local fallback."""

    def __init__(self, *, yaml_model: str | None = None) -> None:
        self.chain: list[LLMEndpoint] = self._build_chain(yaml_model=yaml_model)
        self.last_endpoint: str | None = None
        self.last_error: str | None = None
        self.last_used_template = False
        logger.info("LLM chain: %s", " → ".join(e.label for e in self.chain))

    def _provider_config(self, provider: str) -> tuple[str, str, str]:
        """Return (model_default, base_url, api_key)."""
        if provider == "gemini":
            return (
                default_model(provider),
                "https://generativelanguage.googleapis.com/v1beta/openai",
                _env("GEMINI_API_KEY", "GOOGLE_API_KEY", "LLM_API_KEY"),
            )
        if provider == "groq":
            return (
                default_model(provider),
                "https://api.groq.com/openai/v1",
                _env("GROQ_API_KEY", "LLM_API_KEY"),
            )
        if provider == "openrouter":
            return (
                default_model(provider),
                "https://openrouter.ai/api/v1",
                _env("OPENROUTER_API_KEY", "LLM_API_KEY"),
            )
        if provider in {"openai", "openai_compatible", "compatible"}:
            return (
                default_model(provider),
                _env("LLM_BASE_URL") or "https://api.openai.com/v1",
                _env("OPENAI_API_KEY", "LLM_API_KEY"),
            )
        if provider == "ollama":
            return (
                default_model(provider),
                (_env("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
                + "/v1",
                _env("LLM_API_KEY") or "ollama",
            )
        return ("template", "", "")

    def _build_chain(self, *, yaml_model: str | None) -> list[LLMEndpoint]:
        endpoints: list[LLMEndpoint] = []
        for provider, model in parse_llm_chain_specs(yaml_model=yaml_model):
            if provider == "template":
                endpoints.append(LLMEndpoint("template", "template"))
                continue
            model_default, base_url, api_key = self._provider_config(provider)
            model = (model or model_default).strip()
            if provider != "ollama" and not api_key:
                logger.warning("Skip %s/%s — no API key", provider, model)
                continue
            endpoints.append(
                LLMEndpoint(
                    provider=provider,
                    model=model,
                    base_url=base_url.rstrip("/"),
                    api_key=api_key,
                )
            )
        if not endpoints or endpoints[-1].provider != "template":
            endpoints.append(LLMEndpoint("template", "template"))
        return endpoints

    def _invoke_endpoint(
        self,
        endpoint: LLMEndpoint,
        system: str,
        user: str,
        *,
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str:
        url = f"{endpoint.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
        }
        if endpoint.provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/news-shorts-pipeline"
            headers["X-Title"] = "News Shorts Pipeline"
        # gpt-oss burns completion budget on reasoning; leave room for content.
        model_l = (endpoint.model or "").lower()
        is_gpt_oss = "gpt-oss" in model_l
        token_budget = max(max_tokens, 1536 if is_gpt_oss else max_tokens)
        payload: dict[str, Any] = {
            "model": endpoint.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if endpoint.provider == "groq" and is_gpt_oss:
            payload["max_completion_tokens"] = token_budget
            payload["reasoning_effort"] = "low"
        else:
            payload["max_tokens"] = token_budget
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content")
        if isinstance(text, list):
            parts = []
            for block in text:
                if isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(str(block))
            text = "\n".join(parts)
        text = str(text or "").strip()
        if not text:
            finish = choice.get("finish_reason")
            raise RuntimeError(
                f"empty model response (finish_reason={finish!r}; "
                "reasoning models may need a higher token budget)"
            )
        return text

    def cloud_endpoints(self) -> list[LLMEndpoint]:
        return [e for e in self.chain if e.provider != "template"]

    def try_complete(
        self,
        endpoint: LLMEndpoint,
        system: str,
        user: str,
        *,
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str | None:
        """Invoke one endpoint with a single 429 retry. Returns None on failure."""
        for attempt in range(2):
            try:
                text = self._invoke_endpoint(
                    endpoint,
                    system,
                    user,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if not text.strip():
                    raise RuntimeError("empty model response")
                self.last_endpoint = endpoint.label
                return text
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                self.last_error = f"{endpoint.label}: {err[:300]}"
                if ("429" in err or "RESOURCE_EXHAUSTED" in err) and attempt == 0:
                    m = re.search(r"[Rr]etry in ([\d.]+)", err)
                    wait = float(m.group(1)) if m else 2.0
                    wait = min(max(wait, 0.5), 8.0)
                    logger.warning(
                        "LLM rate-limited on %s — retry in %.1fs",
                        endpoint.label,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                logger.warning(
                    "LLM failed on %s (%s)",
                    endpoint.label,
                    err[:160],
                )
                return None
        return None

    def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str:
        """
        Return model text, or ``TEMPLATE_SENTINEL`` when all cloud endpoints fail /
        the chain reaches the template step.
        """
        return self.complete_until(
            system,
            user,
            accept=lambda _text: True,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def complete_until(
        self,
        system: str,
        user: str,
        *,
        accept: Callable[[str], bool],
        temperature: float = 0.55,
        max_tokens: int = 1800,
    ) -> str:
        """
        Try each cloud endpoint until ``accept(text)`` is true.
        HTTP failures / 429 / rejected payloads advance to the next endpoint.
        Returns ``TEMPLATE_SENTINEL`` when exhausted.
        """
        self.last_endpoint = None
        self.last_error = None
        self.last_used_template = False
        errors: list[str] = []

        for endpoint in self.chain:
            if endpoint.provider == "template":
                self.last_used_template = True
                self.last_endpoint = "template"
                if errors:
                    self.last_error = " | ".join(errors)[:500]
                    logger.warning(
                        "All LLM endpoints failed validation — using template. Last: %s",
                        errors[-1][:200],
                    )
                return TEMPLATE_SENTINEL

            for attempt in range(2):
                try:
                    text = self._invoke_endpoint(
                        endpoint,
                        system,
                        user,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    if not text.strip():
                        raise RuntimeError("empty model response")
                    if not accept(text):
                        errors.append(f"{endpoint.label}: response rejected by validator")
                        self.last_error = errors[-1][:300]
                        logger.warning(
                            "LLM response rejected on %s — next fallback",
                            endpoint.label,
                        )
                        break
                    self.last_endpoint = endpoint.label
                    if attempt or errors:
                        logger.info("LLM ok via %s", endpoint.label)
                    else:
                        logger.info("LLM accepted via %s", endpoint.label)
                    return text
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    errors.append(f"{endpoint.label}: {err[:180]}")
                    self.last_error = err[:300]
                    if ("429" in err or "RESOURCE_EXHAUSTED" in err) and attempt == 0:
                        m = re.search(r"[Rr]etry in ([\d.]+)", err)
                        wait = float(m.group(1)) if m else 2.0
                        wait = min(max(wait, 0.5), 8.0)
                        logger.warning(
                            "LLM rate-limited on %s — retry in %.1fs then fallback",
                            endpoint.label,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    logger.warning(
                        "LLM failed on %s — next fallback (%s)",
                        endpoint.label,
                        err[:160],
                    )
                    break

        self.last_used_template = True
        self.last_endpoint = "template"
        self.last_error = " | ".join(errors)[:500] if errors else None
        return TEMPLATE_SENTINEL


def get_llm_chain(*, yaml_model: str | None = None) -> LLMChain:
    return LLMChain(yaml_model=yaml_model)
