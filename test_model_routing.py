"""Point it at a provider and have it actually go there.

Two live failures sit behind this file:

  - browser/task_flow built an OllamaAdapter directly, so form-field
    mapping stayed pinned to the local daemon while every other component
    ran on OpenRouter. With the Ollama quota spent it returned no fills
    and the page was reported as having no fillable fields -- a provider
    quota presenting as "the form could not be read".
  - the slash convention ("vendor/model" means OpenAI-compatible) is
    right for a catalogue and wrong for a first-party endpoint. DeepSeek's
    own API calls its models `deepseek-chat`; under the slash rule that
    routes to Ollama, which does not have it, and the error blames the
    model instead of the routing.
"""
from __future__ import annotations

import pytest

from backend.app.models import adapter_pool


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("MODEL_PROVIDER", "OPENAI_BASE_URL", "OPENROUTER_API_KEY",
                "OPENAI_API_KEY", "OLLAMA_HOST", "OLLAMA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    adapter_pool.reset()
    yield
    adapter_pool.reset()


def _kind(name):
    return type(adapter_pool._build(name)).__name__


def test_a_vendor_prefixed_name_goes_to_the_openai_endpoint():
    assert _kind("deepseek/deepseek-v4-flash") == "OpenAICompatAdapter"


def test_a_bare_name_still_means_the_local_daemon():
    assert _kind("phi3:latest") == "OllamaAdapter"


def test_an_explicit_base_url_beats_the_naming_convention(monkeypatch):
    """DeepSeek's first-party endpoint names its models without a vendor
    prefix. Nobody sets OPENAI_BASE_URL by accident."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    assert _kind("deepseek-chat") == "OpenAICompatAdapter"
    assert _kind("deepseek-reasoner") == "OpenAICompatAdapter"


def test_an_explicit_provider_still_outranks_everything(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert _kind("some-self-hosted-vllm-model") == "OpenAICompatAdapter"


def test_the_browser_form_mapper_uses_the_pool_not_a_hardcoded_provider():
    """The bug: an OllamaAdapter built inline, ignoring every model
    setting in the system."""
    import inspect
    from backend.app.browser import task_flow
    src = inspect.getsource(task_flow._get_adapter)
    assert "adapter_pool" in src or "get_adapter" in src
    assert "OllamaAdapter" not in src


def test_the_browser_form_mapper_follows_the_loop_model(monkeypatch):
    from backend.app.browser.task_flow import _model_name
    monkeypatch.setenv("AGENT_LOOP_MODEL", "deepseek/deepseek-v4-flash")
    assert _model_name() == "deepseek/deepseek-v4-flash"


def test_the_defaults_are_not_a_free_tier_model():
    """Both free backends were exhausted in one afternoon of testing, and
    a browser run spends 13-22 model calls. A default that buys two runs
    is not a default."""
    from backend.app.api.routes import DEFAULT_MODEL
    from backend.app.browser.task_flow import DEFAULT_MODEL as FORM_MODEL
    for name in (DEFAULT_MODEL, FORM_MODEL):
        assert "cloud" not in name, f"{name} is an Ollama free-tier model"
        assert ":free" not in name, f"{name} is a rate-capped free model"
        # Checked by ROUTING, not by spelling. An earlier version of this
        # asserted the name contained a slash, which was only ever a
        # proxy for "goes to a real provider" -- and it broke the moment
        # the default became DeepSeek's own unprefixed `deepseek-v4-flash`.
        assert type(adapter_pool._build(name)).__name__ == "OpenAICompatAdapter", (
            f"{name} routes to the local daemon, not a paid provider")
        adapter_pool.reset()


# ------------- DeepSeek is spelled two ways for two endpoints

def test_a_first_party_deepseek_name_reaches_deepseek(monkeypatch):
    """`deepseek-v4-flash` is DeepSeek's own spelling;
    `deepseek/deepseek-v4-flash` is OpenRouter's catalogue name for the
    same model. Same model, different endpoints, and a name is all the
    system gets."""
    from backend.app.models.adapter_pool import DEEPSEEK_BASE_URL, _build
    assert _build("deepseek-v4-flash").base_url == DEEPSEEK_BASE_URL


def test_the_prefixed_spelling_still_reaches_openrouter():
    from backend.app.models.adapter_pool import OPENROUTER_BASE_URL, _build
    a = _build("deepseek/deepseek-v4-flash")
    assert a.base_url == OPENROUTER_BASE_URL


def test_an_explicit_base_url_beats_the_deepseek_shortcut(monkeypatch):
    """A proxy or self-hosted gateway must stay one variable away."""
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.internal/v1")
    from backend.app.models.adapter_pool import _build
    assert _build("deepseek-v4-flash").base_url == "https://gateway.internal/v1"


def test_the_deepseek_key_is_read_for_a_deepseek_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    from backend.app.models.adapter_pool import _build
    assert _build("deepseek-v4-flash").api_key == "sk-deepseek"


def test_the_deepseek_key_is_not_sent_to_openrouter(monkeypatch):
    """Sending one provider's key to another leaks it into their logs."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-1")
    from backend.app.models.adapter_pool import _build
    assert _build("deepseek/deepseek-v4-flash").api_key == "sk-or-1"


def test_a_bare_first_party_name_is_not_an_ollama_model(monkeypatch):
    """FOUND IN THE APP, not in a test. The chat path tested only for a
    vendor prefix, so "deepseek-v4-flash" -- the product's own default --
    fell through to the local daemon, got "model not found", tripped the
    health probe and fell back to the mock. A founder with a valid
    DeepSeek key was told to run `ollama serve`.

    adapter_pool had routed bare deepseek-* correctly since the model
    migration. This branch simply never asked it.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    from backend.app.main import MockAdapter, _build_adapter
    adapter = _build_adapter("deepseek-v4-flash", use_mock=False)
    assert not isinstance(adapter, MockAdapter)
    assert "Ollama" not in type(adapter).__name__


def test_the_two_routing_rules_agree(monkeypatch):
    """One question, two callers. They must not drift again."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    from backend.app.main import MockAdapter, _build_adapter
    from backend.app.models.adapter_pool import _is_first_party_deepseek
    for name in ("deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"):
        assert _is_first_party_deepseek(name)
        assert not isinstance(_build_adapter(name, use_mock=False), MockAdapter)
