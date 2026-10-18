import pytest
from fastapi.testclient import TestClient

from src.core.config import Settings, settings
from src.core.security import jwt_secret
from src.main import app


def test_insecure_defaults_are_off():
    assert Settings.model_fields["ENABLE_DEV_AUTH"].default is False
    assert Settings.model_fields["JWT_SECRET"].default == ""


@pytest.mark.parametrize(
    "secret",
    ["", "short", "change-me-to-a-long-random-string", "dev-secret-change-me-0123456789abcdef"],
)
def test_jwt_secret_rejects_missing_short_and_placeholder(monkeypatch, secret):
    monkeypatch.setattr(settings, "JWT_SECRET", secret)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        jwt_secret()


def test_startup_fails_without_valid_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "JWT_SECRET", "change-me-to-a-long-random-string")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        with TestClient(app):
            pass


def test_startup_fails_when_database_required_but_missing(monkeypatch):
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "REQUIRE_DATABASE", True)
    with pytest.raises(RuntimeError, match="REQUIRE_DATABASE"):
        with TestClient(app):
            pass


def test_production_refuses_dev_settings(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "ENABLE_DEV_AUTH", True)
    monkeypatch.setattr(settings, "CORS_ORIGINS", "*")
    monkeypatch.setattr(settings, "DATABASE_URL", "")
    with pytest.raises(RuntimeError) as excinfo:
        with TestClient(app):
            pass
    message = str(excinfo.value)
    assert "ENABLE_DEV_AUTH" in message
    assert "CORS_ORIGINS" in message
    assert "DATABASE_URL" in message


def test_production_config_problems_empty_when_hardened(monkeypatch):
    from src.main import production_config_problems

    monkeypatch.setattr(settings, "ENABLE_DEV_AUTH", False)
    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://ui.example.com")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://u:p@db/x")
    assert production_config_problems() == []
