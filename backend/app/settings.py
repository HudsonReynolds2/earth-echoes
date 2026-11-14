"""Application settings (task E0.3; spec section 15.3).

Precedence, highest first: environment variables, then an optional TOML file
named by EOE_CONFIG_FILE (decision D5), then coded defaults. Required values
with no source fail loudly at startup; nothing secret has a default.
"""

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

CONFIG_FILE_ENV = "EOE_CONFIG_FILE"


def parse_bounds(raw: str) -> tuple[float, float, float, float]:
    """`EOE_MAP_IMAGE_BOUNDS` as (south, west, north, east).

    Strict on purpose: a swapped pair would put the image on the wrong side of
    the planet, and a silent clamp would hide that.
    """
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("EOE_MAP_IMAGE_BOUNDS must be four numbers: south,west,north,east")
    try:
        south, west, north, east = (float(part) for part in parts)
    except ValueError as error:
        raise ValueError(
            "EOE_MAP_IMAGE_BOUNDS must be four numbers: south,west,north,east"
        ) from error
    if not (-90 <= south < north <= 90):
        raise ValueError("EOE_MAP_IMAGE_BOUNDS: need -90 <= south < north <= 90")
    if not (-180 <= west < east <= 180):
        raise ValueError("EOE_MAP_IMAGE_BOUNDS: need -180 <= west < east <= 180")
    return south, west, north, east


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
    # E4.5 (spec 8.4; phase-4 fixed choice 3; D175): the FIRMWARE key-encryption
    # key that wraps each Pod's data key on a provisioning card. Optional HERE
    # and required by `create_app`: only the API generates bundles, and the
    # worker, the seed CLI and the devbroker CLI also build Settings without
    # ever touching a card. A missing or malformed value fails the API at
    # construction, naming the variable and never echoing it.
    firmware_kek: str | None = Field(default=None, validation_alias="EOE_FIRMWARE_KEK")
    # Which firmware KEK a card was wrapped under, written into every card's
    # `secrets.key_version`. Rotation of the KEK itself is spec 17 item 7 (open).
    firmware_key_version: int = Field(default=1, validation_alias="EOE_FIRMWARE_KEY_VERSION")
    # E4.6 (spec 16.4; D167): embed each Aggregator's own broker login in its
    # settings.yaml. ON by default: dynsec is required for v1 (D116), so a card
    # without credentials would never reach the broker. Off writes the block
    # without them, and the file and README name what is missing.
    bootstrap_credentials: bool = Field(default=True, validation_alias="EOE_BOOTSTRAP_CREDENTIALS")
    redis_url: str | None = Field(default=None, validation_alias="REDIS_URL")
    cors_origins: str = Field(default="", validation_alias="EOE_CORS_ORIGINS")
    build_sha: str = Field(default="dev", validation_alias="EOE_BUILD_SHA")
    session_ttl_seconds: int = Field(default=43200, validation_alias="EOE_SESSION_TTL_SECONDS")
    # E2.6 (D56): gates E3's publish call-through. E2's apply stops at draft
    # revisions unconditionally; E3.13 wired its apply to publication and
    # flipped this default ON (D61, task E3.13). Publication still only
    # reaches a deployment that HAS a `deployment_service` broker row, and
    # the flag stays settable per environment for a deployment that wants
    # to stage config without touching devices.
    publish_enabled: bool = Field(default=True, validation_alias="EOE_PUBLISH_ENABLED")
    # E3.7 (D59): the reconciliation worker runs as its own process in the dev
    # and production stacks. This flag runs it inside the API process instead,
    # from the same module - one deployment mode for the simplest self-hosted
    # install (spec 15.1), and OFF by default so an API replica never quietly
    # becomes a second worker competing for the same revisions.
    worker_in_api: bool = Field(default=False, validation_alias="EOE_WORKER_IN_API")
    # How often the worker fails out timed-out pending revisions (spec 6.4
    # item 4). Well under the 300s default window, so the window is what
    # decides a timeout and the cadence only decides how promptly it is seen.
    timeout_sweep_seconds: int = Field(default=30, validation_alias="EOE_TIMEOUT_SWEEP_SECONDS")
    # How often the worker re-compares applied devices (spec 6.4 item 5).
    # Slower than the timeout sweep on purpose: it recomputes effective config
    # per applied device, and a device that diverges tells us so on its next
    # report anyway - this sweep is the backstop for the one that does not.
    drift_sweep_seconds: int = Field(default=300, validation_alias="EOE_DRIFT_SWEEP_SECONDS")
    # E5.7b, spec 16.4: how often the worker delivers a deployment's service
    # settings to devices that do not have them yet, and retries any broker
    # credential whose revocation could not reach its broker (D133). Its whole
    # job is the Aggregator created AFTER the operator saved their services, so
    # the cadence is "how long a newly registered Pi waits", not a correctness
    # bound - the services save itself already reached every device that
    # existed at the time. A minute keeps that wait short, and a pass over an
    # up-to-date fleet writes and publishes nothing.
    service_config_sweep_seconds: int = Field(
        default=60, validation_alias="EOE_SERVICE_CONFIG_SWEEP_SECONDS"
    )
    # E5.7b: how often both hosts re-read the `deployment_service` broker rows
    # and reconcile their connections. Faster than the config sweep because
    # nothing else notices a new deployment at all - until this ticks, that
    # deployment's control plane does not exist. It is one query and a
    # frozen-dataclass comparison; a no-change tick starts and stops nothing.
    broker_refresh_seconds: int = Field(default=30, validation_alias="EOE_BROKER_REFRESH_SECONDS")
    # Where the standalone worker writes its liveness stamp. The compose
    # healthcheck reads the file's age; the worker serves no port, and
    # opening one purely to answer a probe would add a socket, a framework
    # and a route to a process that only talks to Postgres and a broker.
    worker_heartbeat_path: str = Field(
        default="/tmp/eoe-worker.heartbeat", validation_alias="EOE_WORKER_HEARTBEAT_PATH"
    )

    # E6.1 (spec 9.1, 14.5, 15.1; phase-6 fixed choice 1; D188): which map
    # basemaps the frontend offers, served at runtime by `GET /map/config` so
    # an operator changes basemaps without rebuilding the frontend.
    # `map_offline` removes every source that fetches from the internet.
    map_offline: bool = Field(default=False, validation_alias="EOE_MAP_OFFLINE")
    # A Google Maps Platform key for the Map Tiles API. A BROWSER key, not a
    # secret: it must reach the browser to fetch tiles, so it does not go
    # through SecretStore, and it must be HTTP-referrer-restricted in the
    # Google console. Unset means no Google basemap is offered.
    map_google_api_key: str | None = Field(default=None, validation_alias="EOE_MAP_GOOGLE_API_KEY")
    # A .pmtiles file the browser can fetch with HTTP range requests.
    map_pmtiles_url: str | None = Field(default=None, validation_alias="EOE_MAP_PMTILES_URL")
    # An operator-supplied georeferenced image and its bounds, as
    # "south,west,north,east" in decimal degrees. Both or neither.
    map_image_url: str | None = Field(default=None, validation_alias="EOE_MAP_IMAGE_URL")
    map_image_bounds: str | None = Field(default=None, validation_alias="EOE_MAP_IMAGE_BOUNDS")

    @field_validator(
        "map_google_api_key", "map_pmtiles_url", "map_image_url", "map_image_bounds", mode="before"
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # Compose passes `${EOE_MAP_IMAGE_URL:-}` as an empty string when the
        # operator sets nothing; that means "not configured", not "configured
        # as empty".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _map_image_pair(self) -> "Settings":
        # Fail at startup, naming the variables, rather than serve a basemap
        # the browser cannot place.
        if (self.map_image_url is None) != (self.map_image_bounds is None):
            raise ValueError("EOE_MAP_IMAGE_URL and EOE_MAP_IMAGE_BOUNDS must be set together")
        if self.map_image_bounds is not None:
            parse_bounds(self.map_image_bounds)
        return self

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
