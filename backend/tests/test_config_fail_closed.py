"""Configuration must fail closed: only an explicit development or test environment gets relaxed defaults.

Reproduced before the fix: ``ENVIRONMENT=prod`` (a misspelling) started with no JWT_SECRET, so tokens were
signed with a key published in the source code.
"""

import pytest
from pydantic import ValidationError

from app.core import Settings


@pytest.mark.parametrize(
    "environment", ["prod", "live", "Production ", "staging-eu", "", "customer-a"]
)
def test_unknown_environment_names_get_the_strict_rules(environment: str) -> None:
    with pytest.raises(ValidationError):
        Settings(environment=environment, _env_file=None)


@pytest.mark.parametrize("environment", ["development", "test", "DEVELOPMENT"])
def test_explicit_development_and_test_keep_local_defaults(environment: str) -> None:
    settings = Settings(environment=environment, jwt_secret=None, _env_file=None)  # explicit None beats the env
    assert settings.is_production is False
    assert settings.effective_jwt_secret == "local-development-only-not-for-deployment"


def test_missing_jwt_secret_never_falls_back_outside_development() -> None:
    settings = Settings.model_construct(environment="prod", jwt_secret=None)  # bypasses validation on purpose
    with pytest.raises(RuntimeError, match="JWT_SECRET is required"):
        _ = settings.effective_jwt_secret


def test_cookies_are_secure_unless_the_environment_is_explicitly_development() -> None:
    assert Settings(environment="development", cookie_secure=None, _env_file=None).effective_cookie_secure is False
    assert (
        Settings.model_construct(environment="prod", cookie_secure=None).effective_cookie_secure
        is True
    )


def test_production_flag_follows_the_environment() -> None:
    assert Settings.model_construct(environment="staging").is_production is True
    assert Settings.model_construct(environment="production").is_production is True
