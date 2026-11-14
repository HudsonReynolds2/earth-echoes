"""E6.1: the basemap configuration (spec 9.1, 14.5, 15.1; phase-6 fixed choice 1; D188).

The server decides WHICH sources are offered. These tests hold the four rules
that matter to an operator:

- online, satellite is the default (the owner's choice);
- a source is offered only when its settings are present, and Google only
  with a key;
- offline mode removes every source that fetches from the internet;
- a half-configured image fails at startup, not in a browser.
"""

import pytest
from conftest import ephemeral_postgres, make_kek
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auth import PASSWORD

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.map.basemaps import BASEMAP_IDS, ONLINE, map_config
from app.models import Deployment, RoleAssignment, User
from app.seed import seed_demo_hierarchy
from app.settings import Settings, parse_bounds

BASE = {
    "database_url": "postgresql+psycopg://unused/unused",
    "session_secret": "e6-map-config",
    "kek": make_kek(),
}

BOUNDS = "41.0,-124.2,41.4,-123.8"


def config(**overrides):
    return map_config(Settings(**BASE, **overrides))


def ids(cfg) -> list[str]:
    return [source.id for source in cfg.sources]


# --- The rules, without a database -------------------------------------------


def test_the_closed_id_set_is_pinned():
    """Mirrored by `lib/basemaps.ts` and pinned there too (basemaps.test.ts).
    Adding a source means adding it on both sides, deliberately."""
    assert BASEMAP_IDS == ("esri_imagery", "osm", "google_satellite", "pmtiles", "image", "none")
    assert frozenset({"esri_imagery", "osm", "google_satellite"}) == ONLINE


def test_online_defaults_to_satellite_and_offers_no_google_without_a_key():
    cfg = config()
    assert cfg.offline is False
    assert cfg.default == "esri_imagery"
    assert ids(cfg) == ["esri_imagery", "osm", "none"]


def test_google_is_offered_only_with_a_key_and_carries_it():
    cfg = config(map_google_api_key="browser-key-abc")
    assert ids(cfg) == ["esri_imagery", "osm", "google_satellite", "none"]
    google = next(source for source in cfg.sources if source.id == "google_satellite")
    assert google.api_key == "browser-key-abc"
    # The key appears on its own source and nowhere else.
    assert all(source.api_key is None for source in cfg.sources if source.id != "google_satellite")
    # Satellite stays the default: Google is opt-in, not a takeover.
    assert cfg.default == "esri_imagery"


def test_offline_sources_are_offered_when_configured():
    cfg = config(
        map_pmtiles_url="/tiles/site.pmtiles",
        map_image_url="/img/site.png",
        map_image_bounds=BOUNDS,
    )
    assert ids(cfg) == ["esri_imagery", "osm", "pmtiles", "image", "none"]
    image = next(source for source in cfg.sources if source.id == "image")
    assert image.bounds == (41.0, -124.2, 41.4, -123.8)
    assert cfg.default == "esri_imagery"


def test_offline_mode_removes_every_online_source_even_google():
    cfg = config(map_offline=True, map_google_api_key="k", map_pmtiles_url="/t.pmtiles")
    assert cfg.offline is True
    assert not set(ids(cfg)) & ONLINE
    assert ids(cfg) == ["pmtiles", "none"]
    assert cfg.default == "pmtiles"


def test_offline_prefers_pmtiles_then_image_then_a_plain_background():
    image_only = config(map_offline=True, map_image_url="/i.png", map_image_bounds=BOUNDS)
    assert image_only.default == "image"
    bare = config(map_offline=True)
    assert ids(bare) == ["none"]
    assert bare.default == "none"


@pytest.mark.parametrize(
    "overrides",
    [
        {"map_image_url": "/i.png"},
        {"map_image_bounds": BOUNDS},
        {"map_image_url": "/i.png", "map_image_bounds": "1,2,3"},
        {"map_image_url": "/i.png", "map_image_bounds": "41.4,-124.2,41.0,-123.8"},  # south > north
        {"map_image_url": "/i.png", "map_image_bounds": "41.0,-123.8,41.4,-124.2"},  # west > east
        {"map_image_url": "/i.png", "map_image_bounds": "a,b,c,d"},
    ],
)
def test_a_half_configured_or_malformed_image_fails_at_startup(overrides):
    with pytest.raises(ValueError, match="EOE_MAP_IMAGE"):
        Settings(**BASE, **overrides)


def test_bounds_parse_strictly():
    assert parse_bounds(" 41 , -124.2,41.4, -123.8 ") == (41.0, -124.2, 41.4, -123.8)


# --- The endpoint ------------------------------------------------------------

SCOPED = "map-cfg-scoped@example.com"
NOBODY = "map-cfg-nobody@example.com"


@pytest.fixture(scope="module")
def api_app():
    with ephemeral_postgres() as url:
        app = create_app(
            Settings(
                database_url=url,
                session_secret="e6-map-config",
                kek=make_kek(),
                firmware_kek=make_kek(),
                cors_origins="",
                map_google_api_key="browser-key-abc",
            )
        )
        with app.state.session_factory() as db:
            seed_demo_hierarchy(db)
            db.flush()
            deployment_id = db.scalars(select(Deployment.id).limit(1)).one()
            scoped = User(email=SCOPED, password_hash=hash_password(PASSWORD))
            scoped.role_assignments.append(
                RoleAssignment(role="deployment_operator", deployment_id=deployment_id)
            )
            db.add(scoped)
            db.add(User(email=NOBODY, password_hash=hash_password(PASSWORD)))
            db.commit()
        yield app


def login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


@pytest.mark.integration
def test_a_deployment_scoped_operator_reads_the_config(api_app):
    """An operator scoped to ONE Deployment still needs a basemap to see it,
    so the gate is "any assignment", not an org-level permission."""
    response = login(api_app, SCOPED).get(f"{API_PREFIX}/map/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["default"] == "esri_imagery"
    assert [source["id"] for source in body["sources"]] == [
        "esri_imagery",
        "osm",
        "google_satellite",
        "none",
    ]


@pytest.mark.integration
def test_the_config_needs_a_session_and_an_assignment(api_app):
    anonymous = TestClient(api_app, raise_server_exceptions=False)
    assert anonymous.get(f"{API_PREFIX}/map/config").status_code == 401
    assert login(api_app, NOBODY).get(f"{API_PREFIX}/map/config").status_code == 403


def test_blank_values_from_compose_mean_unset():
    """Compose passes `${EOE_MAP_IMAGE_BOUNDS:-}` as "" when nothing is set.
    That must read as "not configured", not crash startup or offer a source
    with an empty URL."""
    cfg = config(map_google_api_key="", map_pmtiles_url=" ", map_image_url="", map_image_bounds="")
    assert ids(cfg) == ["esri_imagery", "osm", "none"]
