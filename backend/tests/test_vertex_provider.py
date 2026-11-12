"""Vertex AI provider: regional endpoint, Application Default Credentials, token refresh,
credential failures mapped to stable codes, selection, pricing and staging validation.

No network and no Google libraries needed: ``google.auth`` is replaced by a stub and the
OpenAI-compatible client by the one from test_gemini_provider.
"""

import sys
import types
from typing import Any

import pytest

from app.core import settings
from app.processing import ProcessingError, VertexProvider, llm, llm_model
from tests.test_gemini_provider import SCHEMA, StubClient, stub_gemini  # noqa: F401  (fixture)
from tests.test_staging_settings import staging_settings


class FakeCredentials:
    refreshes = 0

    def __init__(self) -> None:
        self.token: str | None = None
        self.valid = False

    def refresh(self, _request: object) -> None:
        FakeCredentials.refreshes += 1
        self.token = f"ya29.test-{FakeCredentials.refreshes}"
        self.valid = True


@pytest.fixture
def vertex(monkeypatch: pytest.MonkeyPatch, stub_gemini: type[StubClient]) -> dict[str, Any]:  # noqa: F811
    seen: dict[str, Any] = {"default_calls": 0}

    def default(scopes: list[str]) -> tuple[FakeCredentials, str]:
        seen["default_calls"] += 1
        seen["scopes"] = scopes
        if seen.get("fail"):
            raise RuntimeError("/secret/path/key.json not found for sa@project.iam")
        return FakeCredentials(), "docmind-test-project"

    google = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    auth.default = default  # type: ignore[attr-defined]
    transport = types.ModuleType("google.auth.transport")
    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.Request = lambda: object()  # type: ignore[attr-defined]
    google.auth = auth  # type: ignore[attr-defined]
    for name, module in {
        "google": google, "google.auth": auth, "google.auth.transport": transport,
        "google.auth.transport.requests": requests_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(VertexProvider, "_credentials", None)
    FakeCredentials.refreshes = 0
    monkeypatch.setattr(settings(), "llm_provider", "vertex")
    monkeypatch.setattr(settings(), "vertex_project", "docmind-test-project")
    monkeypatch.setattr(settings(), "vertex_location", "southamerica-east1")
    monkeypatch.setattr(settings(), "vertex_model", None)
    monkeypatch.setattr(settings(), "gemini_api_key", None)  # Vertex never uses the API key
    return seen


def test_request_goes_to_the_regional_vertex_endpoint_with_a_bearer_token(vertex, stub_gemini) -> None:  # noqa: F811
    result = VertexProvider().extract("Invoice INV-1", SCHEMA)
    assert result["fields"] == {"invoice_number": "INV-1"}
    assert stub_gemini.init["base_url"] == (
        "https://southamerica-east1-aiplatform.googleapis.com/v1/projects/docmind-test-project"
        "/locations/southamerica-east1/endpoints/openapi"
    )
    assert stub_gemini.init["api_key"] == "ya29.test-1"
    assert stub_gemini.calls[0]["model"] == f"google/{settings().gemini_model}"
    assert vertex["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]


def test_global_location_uses_the_global_host(vertex, stub_gemini, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(settings(), "vertex_location", "global")
    monkeypatch.setattr(settings(), "vertex_model", "gemini-pro-test")
    VertexProvider().extract("text", SCHEMA)
    assert stub_gemini.init["base_url"].startswith("https://aiplatform.googleapis.com/v1/projects/")
    assert stub_gemini.calls[0]["model"] == "google/gemini-pro-test"


def test_credentials_are_loaded_once_and_refreshed_only_when_expired(vertex, stub_gemini) -> None:  # noqa: F811
    VertexProvider().extract("a", SCHEMA)
    VertexProvider().extract("b", SCHEMA)
    assert vertex["default_calls"] == 1 and FakeCredentials.refreshes == 1
    VertexProvider._credentials.valid = False  # token expired
    VertexProvider().extract("c", SCHEMA)
    assert FakeCredentials.refreshes == 2
    assert stub_gemini.init["api_key"] == "ya29.test-2"


def test_credential_failure_is_retryable_and_leaks_nothing(vertex, stub_gemini, caplog) -> None:  # noqa: F811
    vertex["fail"] = True
    with pytest.raises(ProcessingError) as error:
        VertexProvider().extract("text", SCHEMA)
    assert error.value.code == "LLM_UNAVAILABLE" and error.value.retryable
    assert stub_gemini.calls == []
    assert "secret" not in caplog.text and "sa@project" not in caplog.text


def test_missing_google_auth_package_is_a_stable_error(vertex, stub_gemini, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setitem(sys.modules, "google.auth", None)
    with pytest.raises(ProcessingError) as error:
        VertexProvider().extract("text", SCHEMA)
    assert error.value.code == "LLM_REQUEST_FAILED"


def test_missing_project_fails_before_any_request(vertex, stub_gemini, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(settings(), "vertex_project", None)
    with pytest.raises(RuntimeError, match="VERTEX_PROJECT"):
        VertexProvider().extract("text", SCHEMA)
    assert stub_gemini.calls == []


def test_selection_model_name_and_pricing(vertex, monkeypatch) -> None:
    monkeypatch.setattr(settings(), "gemini_input_price_per_1m", 0.3)
    assert isinstance(llm(), VertexProvider)
    assert llm_model() == settings().gemini_model
    from app.processing import estimate_cost

    assert estimate_cost(1_000_000, 0) == 0.3


def test_staging_requires_a_vertex_project() -> None:
    with pytest.raises(ValueError, match="VERTEX_PROJECT"):
        staging_settings(llm_provider="vertex")
    assert staging_settings(llm_provider="vertex", vertex_project="p").vertex_project == "p"


def test_vertex_check_script_reports_generation_and_extraction(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """scripts/vertex_check.py logic with a stand-in model (proves the script, not the model: the real
    check needs credentials, docs/OWNER_SETUP.md section 1)."""
    import importlib.util
    import sys
    from pathlib import Path

    from app import processing
    from app.core import settings
    from app.rag import generation

    path = Path(__file__).resolve().parents[1] / "scripts" / "vertex_check.py"
    spec = importlib.util.spec_from_file_location("vertex_check", path)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    monkeypatch.setattr(settings(), "llm_provider", "vertex")
    monkeypatch.setattr(settings(), "vertex_project", "placeholder-project")

    def complete(self, system: str, prompt: str) -> str:
        self.calls, self.prompt_tokens, self.completion_tokens = 1, 120, 20
        return "El contrato de mantenimiento ficticio tiene un plazo de 24 meses [1]."

    class Extractor:
        def extract(self, text: str, schema: dict, prompt_instructions: str | None = None) -> dict:
            return {"fields": {"invoice_number": "F-2026-0042", "total": "1.452,00 EUR"},
                    "usage": {"prompt_tokens": 80, "completion_tokens": 15, "estimated_cost": 0.00001}}

    monkeypatch.setattr(generation.ChatGenerator, "complete", complete)
    monkeypatch.setattr(processing, "llm", lambda: Extractor())
    monkeypatch.setattr(sys, "argv", ["vertex_check.py"])
    assert module.main() == 0  # dry run: no call without --yes
    assert "dry run" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["vertex_check.py", "--yes"])
    assert module.main() == 0
    out = capsys.readouterr().out
    assert '"verified_claims": 1' in out and '"invoice_number_correct": true' in out
    assert "placeholder-project" not in out  # identifiers and credentials are never printed
