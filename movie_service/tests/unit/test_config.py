import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture(autouse=True)
def no_api_key_env(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)


def test_api_key_is_required():
    with pytest.raises(ValidationError, match="api_key"):
        Settings(_env_file=None)


@pytest.mark.parametrize("key", ["", "dev-api-key"])
def test_short_api_key_is_rejected_without_echoing_it(key):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, api_key=key)
    assert "api_key" in str(exc.value)
    if key:
        assert key not in str(exc.value)


def test_strong_api_key_is_accepted():
    key = "k" * 32
    assert Settings(_env_file=None, api_key=key).api_key.get_secret_value() == key
