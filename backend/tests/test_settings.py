"""Gate 3: settings precedence checks (task E0.3; decision D5).

Precedence, highest first: environment variables, TOML file named by
EOE_CONFIG_FILE, coded defaults. Missing required values and malformed files
fail loudly.
"""

import pytest
from pydantic import ValidationError

from app.settings import CONFIG_FILE_ENV, Settings

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://env-user:x@localhost:5432/envdb",
    "EOE_SESSION_SECRET": "env-session",
    "EOE_KEK": "env-kek",
}


def _set_required(monkeypatch):
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def _clear_all(monkeypatch):
    for name in (*REQUIRED_ENV, "REDIS_URL", "EOE_CORS_ORIGINS", "EOE_BUILD_SHA", CONFIG_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


def test_env_overrides_file(monkeypatch, tmp_path):
    _clear_all(monkeypatch)
    _set_required(monkeypatch)
    config = tmp_path / "eoe.toml"
    config.write_text('database_url = "postgresql+psycopg://file-user:x@localhost:5432/filedb"\n')
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    assert Settings().database_url == REQUIRED_ENV["DATABASE_URL"]


def test_file_overrides_default(monkeypatch, tmp_path):
    _clear_all(monkeypatch)
    _set_required(monkeypatch)
    config = tmp_path / "eoe.toml"
    config.write_text('cors_origins = "http://from-file.test"\n')
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    settings = Settings()
    assert settings.cors_origins == "http://from-file.test"
    assert settings.cors_origin_list == ["http://from-file.test"]


def test_file_absent_env_and_defaults_apply(monkeypatch):
    _clear_all(monkeypatch)
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.database_url == REQUIRED_ENV["DATABASE_URL"]
    assert settings.cors_origins == ""
    assert settings.build_sha == "dev"
    assert settings.redis_url is None


def test_malformed_file_fails_loudly(monkeypatch, tmp_path):
    _clear_all(monkeypatch)
    _set_required(monkeypatch)
    config = tmp_path / "eoe.toml"
    config.write_text("this is [not toml\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(config))
    with pytest.raises(RuntimeError, match="malformed config file"):
        Settings()


def test_missing_config_file_fails_loudly(monkeypatch, tmp_path):
    _clear_all(monkeypatch)
    _set_required(monkeypatch)
    monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "nope.toml"))
    with pytest.raises(RuntimeError, match="missing file"):
        Settings()


def test_missing_required_settings_fail_loudly(monkeypatch):
    _clear_all(monkeypatch)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    message = str(excinfo.value)
    # Pydantic reports the env-var alias, which is exactly what an operator
    # needs to see to fix the deployment.
    for alias in ("DATABASE_URL", "EOE_SESSION_SECRET", "EOE_KEK"):
        assert alias in message, f"startup failure does not name {alias}"
