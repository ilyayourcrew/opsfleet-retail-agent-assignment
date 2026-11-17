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
from retail_agent.llm.fake import BreakerLlm, ChaosLlm, RuleBasedLlm
from retail_agent.resilience import CircuitBreaker

RETRIABLE = [429, 500, 502, 503, 504]


def _export_key(settings: Settings) -> None:
    if settings.google_api_key and not os.environ.get("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = settings.google_api_key
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "false")


def build_model(settings: Settings, chaos_rate: float = 0.0) -> BaseLlm:
    warnings.filterwarnings("ignore", message=".*FALLBACK_MODEL.*")
    warnings.filterwarnings("ignore", category=UserWarning, module="google.adk")
    chain: list[BaseLlm]
    if settings.llm_provider == "fake":
        chain = [RuleBasedLlm(), RuleBasedLlm(model="rule-based-offline-fallback")]
    elif settings.llm_provider == "ollama":
        from google.adk.models.lite_llm import LiteLlm

        chain = [LiteLlm(model=settings.ollama_model), RuleBasedLlm(model="rule-based-offline-fallback")]
    else:
        from google.adk.models import Gemini

        _export_key(settings)

        def gemini(name: str, last: bool) -> BaseLlm:
            # Non-last models: one quick retry on 5xx only; 429 (quota) goes straight to the next model.
            # Last model: full retry policy incl. 429, because there is nothing left to fall back to.
            retry = types.HttpRetryOptions(
                attempts=settings.llm_retry_attempts if last else 2, initial_delay=1.0, max_delay=8.0, exp_base=2.0,
                http_status_codes=RETRIABLE if last else [500, 502, 503],
            )
            return Gemini(model=name, retry_options=retry)

        names = [settings.model_primary, *settings.fallback_models]
        chain = [gemini(n, last=i == len(names) - 1) for i, n in enumerate(names)]
        # skip a model whose recent calls keep failing (no latency paid while it is overloaded)
        chain = [BreakerLlm(inner=m, model=m.model, timeout_s=settings.llm_attempt_timeout_s,
                            total_timeout_s=settings.llm_timeout_s) if i < len(chain) - 1 else m
                 for i, m in enumerate(chain)]
    if chaos_rate > 0:
        chain[0] = ChaosLlm(inner=chain[0], failure_rate=chaos_rate, model=f"chaos({chain[0].model})")
    from google.adk.models import FallbackModel

    return FallbackModel(models=chain)


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
    )

    # light model first, then the last model of the main chain (different capacity pool)
    candidates = list(dict.fromkeys([settings.model_light, *settings.fallback_models[-1:]]))

    def classify(prompt: str) -> str:
        def call() -> str:
            last_err: Exception | None = None
            for m in candidates:
                try:
                    r = client.models.generate_content(model=m, contents=prompt, config=cfg)
                    return r.text or ""
                except Exception as e:  # noqa: BLE001 - try the next model
                    last_err = e
            raise last_err  # type: ignore[misc]

        return breaker.call(call)

    return classify
