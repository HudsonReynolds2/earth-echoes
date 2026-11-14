"""Which basemaps the map offers (task E6.1; spec 9.1, 14.5, 15.1; D188).

The server decides **which** sources are offered and supplies the
operator-specific parameters: a PMTiles URL, an image and its bounds, a
Google key. The frontend's `lib/basemaps.ts` holds each source's tile URL
template, attribution and zoom limits. That split keeps attribution text in
one place, next to the code that draws it, and keeps the operator's choices
in deployment configuration, where an operator can change them without a
rebuild.

The id set is closed and mirrored in `lib/basemaps.ts`. `test_map_config.py`
and `basemaps.test.ts` both pin it, so a new source cannot reach one side
without the other.
"""

from typing import Literal

from pydantic import BaseModel

from app.settings import Settings, parse_bounds

BasemapId = Literal["esri_imagery", "osm", "google_satellite", "pmtiles", "image", "none"]

#: Display order in the switcher. The default is chosen separately.
BASEMAP_IDS: tuple[BasemapId, ...] = (
    "esri_imagery",
    "osm",
    "google_satellite",
    "pmtiles",
    "image",
    "none",
)

#: Sources that fetch from the internet. `EOE_MAP_OFFLINE` removes these.
ONLINE: frozenset[BasemapId] = frozenset({"esri_imagery", "osm", "google_satellite"})


class BasemapOut(BaseModel):
    id: BasemapId
    #: PMTiles file or image URL, when the source needs one.
    url: str | None = None
    #: The image's [south, west, north, east], for `image` only.
    bounds: tuple[float, float, float, float] | None = None
    #: The Google Maps Platform browser key, for `google_satellite` only. A
    #: browser key is public by design (D188); the referrer restriction in the
    #: Google console is what protects it, not secrecy.
    api_key: str | None = None


class MapConfigOut(BaseModel):
    offline: bool
    default: BasemapId
    sources: list[BasemapOut]


def map_config(settings: Settings) -> MapConfigOut:
    """The offered sources, in switcher order, and the default.

    Online, the default is satellite (owner's choice, D188). Offline, it is
    the first configured offline source; with none configured it is `none`, a
    plain background, so markers still draw on a host with no basemap at all.
    """
    candidates: dict[BasemapId, BasemapOut] = {
        "esri_imagery": BasemapOut(id="esri_imagery"),
        "osm": BasemapOut(id="osm"),
    }
    if settings.map_google_api_key:
        candidates["google_satellite"] = BasemapOut(
            id="google_satellite", api_key=settings.map_google_api_key
        )
    if settings.map_pmtiles_url:
        candidates["pmtiles"] = BasemapOut(id="pmtiles", url=settings.map_pmtiles_url)
    if settings.map_image_url and settings.map_image_bounds:
        candidates["image"] = BasemapOut(
            id="image", url=settings.map_image_url, bounds=parse_bounds(settings.map_image_bounds)
        )
    candidates["none"] = BasemapOut(id="none")

    sources = [
        candidates[basemap_id]
        for basemap_id in BASEMAP_IDS
        if basemap_id in candidates and not (settings.map_offline and basemap_id in ONLINE)
    ]
    if settings.map_offline:
        default: BasemapId = next(
            (source.id for source in sources if source.id in ("pmtiles", "image")), "none"
        )
    else:
        default = "esri_imagery"
    return MapConfigOut(offline=settings.map_offline, default=default, sources=sources)
