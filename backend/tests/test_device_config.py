"""E4.2: the `device_config` v1 renderer (spec 5.4, 8.2; phase-4 fixed choice 2;
addendum PHASE4-2-02; D166).

What is pinned, and why each one is a test rather than a code-review item:

1. **The bytes.** `golden/device_config_v1.yaml` is the renderer's output for
   `SAMPLE`, byte for byte. A change to the on-card format must be a
   deliberate act that edits that file in the same commit, because every card
   already written was written in the old bytes.
2. **Every emitted key is a catalog key**, asserted against `CATALOG` by
   walking the PARSED output, not by trusting `CARD_KEYS`.
3. **No secret in plaintext, by scanning the bytes** for planted secret values,
   since E4.3 will hand this renderer the plaintext from `effective_resolved`.
4. **The card carries spec 5.4's set and nothing from the Aggregator side.**
"""

from pathlib import Path

import pytest
import yaml

from app.config.catalog import CATALOG, CATALOG_BY_KEY
from app.provisioning.device_config import (
    CARD_KEYS,
    PLAINTEXT_CARD_KEYS,
    TEMPLATE_VERSION,
    DeviceConfigError,
    render_device_config_v1,
)

GOLDEN = Path(__file__).parent / "golden" / "device_config_v1.yaml"

#: Plaintext that must never appear in a rendered card. Distinctive enough that
#: a substring hit can only mean a leak.
PLANTED_PSK = "planted-psk-7f3a9c-never-on-a-card"
PLANTED_STREAM_KEY = "planted-stream-key-41be0d-never-on-a-card"
PLANTED_S3_CREDENTIAL = "planted-s3-secret-9d2e77-never-on-a-card"

#: A full effective config as `effective_resolved` would hand it over: every
#: catalog key, the catalog default where unset, and plaintext secrets.
SAMPLE: dict[str, object] = {entry.key: entry.default for entry in CATALOG} | {
    "network.wifi_ssid": "alder-creek-halow",
    "network.wifi_password": PLANTED_PSK,
    "network.wifi_security": "WPA3",
    "network.stream_key": PLANTED_STREAM_KEY,
    "network.aggregator_ip": "10.42.0.1",
    "network.stream_endpoint": "10.42.0.1:8765/stream",
    "identity.name": "alder-creek-07",
    "identity.mac": "02:EE:0E:01:01:07",
    "location.gps_lat": 41.7756,
    "location.gps_lon": -124.1987,
    "capture.schedule": {"on": "0 5 * * *", "off": "0 9 * * *"},
    "upload.s3_secret_key": PLANTED_S3_CREDENTIAL,
    "upload.s3_bucket": "aggregator-only-bucket",
}

#: Spec 5.4, transcribed: "MAC, name, sample rate, bit depth, capture settings,
#: SD buffering, log verbosity, Aggregator IP, and stream endpoint", plus the
#: per-pod network block spec 8.2 adds and the GPS pair spec 8.6 fills later.
SPEC_5_4_PLUS_NETWORK = {
    "identity.mac",
    "identity.name",
    "audio.sample_rate_hz",
    "audio.bits_per_sample",
    "audio.channels",
    "capture.mode",
    "capture.duty_on_seconds",
    "capture.duty_off_seconds",
    "capture.schedule",
    "buffering.sd_enabled",
    "buffering.sd_max_bytes",
    "logging.verbosity",
    "network.aggregator_ip",
    "network.stream_endpoint",
    "network.wifi_ssid",
    "network.wifi_security",
    "network.stream_protocol",
    "location.gps_lat",
    "location.gps_lon",
}


def _flatten(document: dict) -> dict[str, object]:
    flat: dict[str, object] = {}
    for group, members in document.items():
        if group in ("schema_version", "template"):
            continue
        for name, value in members.items():
            flat[f"{group}.{name}"] = value
    return flat


def test_output_matches_the_golden_file_byte_for_byte():
    assert render_device_config_v1(SAMPLE) == GOLDEN.read_bytes()


def test_rendering_is_deterministic():
    assert render_device_config_v1(SAMPLE) == render_device_config_v1(dict(SAMPLE))


def test_every_emitted_key_is_a_catalog_key_and_the_set_is_spec_5_4s():
    document = yaml.safe_load(render_device_config_v1(SAMPLE))
    emitted = _flatten(document)
    assert set(emitted) <= set(CATALOG_BY_KEY), set(emitted) - set(CATALOG_BY_KEY)
    assert set(emitted) == SPEC_5_4_PLUS_NETWORK


def test_no_secret_value_appears_anywhere_in_the_bytes():
    rendered = render_device_config_v1(SAMPLE)
    for planted in (PLANTED_PSK, PLANTED_STREAM_KEY, PLANTED_S3_CREDENTIAL):
        assert planted.encode() not in rendered
    # The secret KEY names are absent too: v1 carries no secret slot at all
    # (PHASE4-2-02). E4.5 adds the encrypted block and moves the golden file.
    for entry in CATALOG:
        if entry.secret:
            assert entry.key.split(".", 1)[1].encode() not in rendered, entry.key


def test_aggregator_side_and_service_keys_never_reach_a_card():
    emitted = set(_flatten(yaml.safe_load(render_device_config_v1(SAMPLE))))
    for key in (
        "listener.wake_grace_seconds",
        "analysis.model_id",
        "upload.s3_bucket",
        "telemetry.influx_url",
        "services.credentials_generation",
    ):
        assert key not in emitted
    assert b"aggregator-only-bucket" not in render_device_config_v1(SAMPLE)


def test_card_keys_are_derived_from_the_catalog():
    """The derivation rule, restated against the catalog's own flags so a new
    card-level key joins the card without anyone editing a list."""
    assert (
        tuple(
            e.key
            for e in CATALOG
            if e.lowest_level in ("pod", "listener", "any") and e.write_restricted is None
        )
        == CARD_KEYS
    )
    assert set(CARD_KEYS) - set(PLAINTEXT_CARD_KEYS) == {
        "network.wifi_password",
        "network.stream_key",
    }


def test_the_file_is_versioned_and_visibly_draft():
    rendered = render_device_config_v1(SAMPLE)
    document = yaml.safe_load(rendered)
    assert document["schema_version"] == TEMPLATE_VERSION == 1
    assert document["template"] == "device_config"
    assert rendered.splitlines()[1].startswith(b"# DRAFT pending firmware sign-off")


def test_blank_keys_render_as_explicit_nulls():
    """A per-pod card leaves the inventory keys blank (spec 8.2)."""
    inventory = frozenset(e.key for e in CATALOG if e.resolution == "inventory")
    flat = _flatten(yaml.safe_load(render_device_config_v1(SAMPLE, blank=inventory)))
    assert {key: flat[key] for key in inventory} == dict.fromkeys(inventory)
    assert flat["network.wifi_ssid"] == "alder-creek-halow"


def test_a_missing_card_value_is_an_error_not_a_silent_null():
    partial = {k: v for k, v in SAMPLE.items() if k != "network.stream_endpoint"}
    with pytest.raises(DeviceConfigError, match="network.stream_endpoint"):
        render_device_config_v1(partial)


def test_blanking_a_key_that_is_not_on_the_card_is_refused():
    with pytest.raises(DeviceConfigError, match="analysis.model_id"):
        render_device_config_v1(SAMPLE, blank=frozenset({"analysis.model_id"}))
    with pytest.raises(DeviceConfigError, match="network.wifi_password"):
        render_device_config_v1(SAMPLE, blank=frozenset({"network.wifi_password"}))


@pytest.mark.parametrize("mac", ["12:34:56:12:34:56", "00:00:00:00:00:00", "02:EE:0E:01:01:07"])
def test_a_mac_round_trips_as_a_string(mac):
    """YAML 1.1 reads an all-digit `12:34:56` as a base-60 INTEGER. A MAC that
    came back as 2_715_456_... would match no device on first contact, so the
    renderer must quote it whenever a reader could misread it."""
    rendered = render_device_config_v1(SAMPLE | {"identity.mac": mac})
    assert yaml.safe_load(rendered)["identity"]["mac"] == mac
