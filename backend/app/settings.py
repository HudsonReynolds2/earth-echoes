"""Application settings (task E0.3; spec section 15.3).

Precedence, highest first: environment variables, then an optional TOML file
named by EOE_CONFIG_FILE (decision D5), then coded defaults. Required values
with no source fail loudly at startup; nothing secret has a default.
"""

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

CONFIG_FILE_ENV = "EOE_CONFIG_FILE"


class TomlFileSource(PydanticBaseSettingsSource):
    """Reads the optional TOML file; malformed content fails loudly."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        raw_path = os.environ.get(CONFIG_FILE_ENV)
        if raw_path:
            path = Path(raw_path)
            if not path.is_file():
                raise RuntimeError(f"{CONFIG_FILE_ENV} points to a missing file: {raw_path}")
            try:
                self._data = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as error:
                raise RuntimeError(f"malformed config file {raw_path}: {error}") from error

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {name: value for name, value in self._data.items() if value is not None}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    database_url: str = Field(validation_alias="DATABASE_URL")
    session_secret: str = Field(validation_alias="EOE_SESSION_SECRET")
    kek: str = Field(validation_alias="EOE_KEK")
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")
    cors_origins: str = Field(default="", validation_alias="EOE_CORS_ORIGINS")
    build_sha: str = Field(default="dev", validation_alias="EOE_BUILD_SHA")
    session_ttl_seconds: int = Field(default=43200, validation_alias="EOE_SESSION_TTL_SECONDS")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first: explicit constructor args (tests), env vars,
        # the optional TOML file, then field defaults (D5).
        return (init_settings, env_settings, TomlFileSource(settings_cls))
