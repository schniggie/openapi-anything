"""Static config values must be overridable via environment variables (compose stack).

All values are read at construction/call time (not import time) so the compose
environment and tests can both override them without module reloads.
"""


from openapi_anything.generator.llm_client import LLMClient
from openapi_anything.generator.pipeline import PipelineOrchestrator, PipelineState
from openapi_anything.generator.websearch import SearxNGClient
from openapi_anything.gateway.proxy import GatewayProxy


def test_llm_client_constructs_without_key_configured(monkeypatch):
    """Construction must never raise just because no key is configured —
    every LLM call site in this codebase wraps calls in try/except and
    degrades gracefully (default design, heuristic inspection, etc.); a
    hard crash at construction time would break that pattern. The actual
    auth failure surfaces later, at the real API call, where callers
    already handle it. Regression: newer openai SDK versions (2.4x+)
    validate credentials eagerly and raise on a falsy api_key — verified
    locally against openai==2.46.0 in a clean env."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    LLMClient()  # must not raise


def test_llm_model_default_is_glm_5_2(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    assert LLMClient().model == "GLM-5.2"


def test_llm_model_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "Kimi-K2.6")
    assert LLMClient().model == "Kimi-K2.6"


def test_llm_model_explicit_arg_beats_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "Kimi-K2.6")
    assert LLMClient(model="GLM-5").model == "GLM-5"


def test_llm_timeout_from_env(monkeypatch):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "45.5")
    assert LLMClient().client.timeout == 45.5


def test_pipeline_max_retries_from_env(monkeypatch):
    monkeypatch.setenv("PIPELINE_MAX_RETRIES", "2")
    state = PipelineState(wrapper_id="w", target_description="d")
    assert state.max_retries == 2


def test_pipeline_max_retries_default(monkeypatch):
    monkeypatch.delenv("PIPELINE_MAX_RETRIES", raising=False)
    state = PipelineState(wrapper_id="w", target_description="d")
    assert state.max_retries == 5


def test_output_base_from_env(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    monkeypatch.setenv("WRAPPER_OUTPUT_BASE", str(tmp_path / "custom-wrappers"))
    orch = PipelineOrchestrator(MagicMock())
    assert orch.output_base == tmp_path / "custom-wrappers"
    assert orch.output_base.exists()  # still auto-created


def test_searxng_timeout_and_max_results_from_env(monkeypatch):
    monkeypatch.setenv("SEARXNG_TIMEOUT", "3.5")
    monkeypatch.setenv("SEARXNG_MAX_RESULTS", "2")
    client = SearxNGClient()
    assert client.timeout == 3.5
    assert client.default_max_results == 2


def test_proxy_timeout_from_env(monkeypatch):
    monkeypatch.setenv("PROXY_TIMEOUT", "12")
    proxy = GatewayProxy()
    assert proxy.client.timeout.read == 12.0


def test_health_probe_timeout_from_env(monkeypatch):
    from openapi_anything.gateway import health

    monkeypatch.setenv("HEALTH_PROBE_TIMEOUT", "0.7")
    assert health.probe_timeout() == 0.7


def test_health_sweep_interval_from_env(monkeypatch):
    from openapi_anything.gateway import health

    monkeypatch.setenv("HEALTH_SWEEP_INTERVAL", "7")
    assert health.sweep_interval() == 7.0
