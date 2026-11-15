"""Model construction per provider.

gemini : primary Flash model -> (429/5xx) -> Flash-Lite fallback, each with its own HTTP retry
         policy (ADK `FallbackModel` + `Gemini(retry_options=...)`).
ollama : local model through ADK's LiteLlm wrapper (offline interactive use).
fake   : deterministic RuleBasedLlm (offline demo / CI).
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable

from google.adk.models import BaseLlm
from google.genai import types

from retail_agent.config import Settings
from retail_agent.llm.fake import ChaosLlm, RuleBasedLlm
from retail_agent.resilience import CircuitBreaker

RETRIABLE = [429, 500, 502, 503, 504]


def _export_key(settings: Settings) -> None:
    if settings.google_api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = settings.google_api_key
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "false")


def build_model(settings: Settings, chaos_rate: float = 0.0) -> BaseLlm:
    warnings.filterwarnings("ignore", message=".*FALLBACK_MODEL.*")
    warnings.filterwarnings("ignore", category=UserWarning, module="google.adk")
    if settings.llm_provider == "fake":
        primary: BaseLlm = RuleBasedLlm()
        fallback: BaseLlm = RuleBasedLlm(model="rule-based-offline-fallback")
    elif settings.llm_provider == "ollama":
        from google.adk.models.lite_llm import LiteLlm

        primary = LiteLlm(model=settings.ollama_model)
        fallback = RuleBasedLlm(model="rule-based-offline-fallback")
    else:
        from google.adk.models import Gemini

        _export_key(settings)
        retry = types.HttpRetryOptions(
            attempts=settings.llm_retry_attempts, initial_delay=1.0, max_delay=8.0, exp_base=2.0,
            http_status_codes=RETRIABLE,
        )
        primary = Gemini(model=settings.model_primary, retry_options=retry)
        fallback = Gemini(model=settings.model_fallback, retry_options=types.HttpRetryOptions(attempts=2, initial_delay=1.0))
    if chaos_rate > 0:
        primary = ChaosLlm(inner=primary, failure_rate=chaos_rate, model=f"chaos({primary.model})")
    from google.adk.models import FallbackModel

    return FallbackModel(models=[primary, fallback])


def build_classifier(settings: Settings) -> Callable[[str], str] | None:
    """Cheap scope/safety classifier for the input guard (Gemini Flash-Lite, JSON output)."""
    if settings.llm_provider != "gemini":
        return None
    _export_key(settings)
    from google import genai

    client = genai.Client()
    breaker = CircuitBreaker("classifier", failure_threshold=3, reset_timeout_s=60)
    cfg = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="application/json",
        max_output_tokens=200,
        http_options=types.HttpOptions(timeout=10_000),
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    def classify(prompt: str) -> str:
        def call() -> str:
            r = client.models.generate_content(model=settings.model_fallback, contents=prompt, config=cfg)
            return r.text or ""

        return breaker.call(call)

    return classify
