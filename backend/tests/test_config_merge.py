"""Gate 33: E2.3 effective-config merge engine — TEST-CRITICAL (spec 14.5).

THIS SUITE IS THE DOCUMENTATION OF THE MERGE SEMANTICS (D52-D53) and one of
the four suites no later session may weaken (rule R0). Everything here is
pure: no database, no app fixtures. Each test states one semantic; the
golden checksums at the bottom are the frozen byte-stability contract E3's
reconciliation and E4's bundles depend on — if one of those constants ever
needs to change, that is a wire-protocol break, not a test update.

The semantics in one paragraph (D53): spec 5.1's "deep merge" IS the level
cascade. Keys are flat dotted strings; per key the DEEPEST chain level that
sets it wins, else the catalog default; the winning value replaces
wholesale (capture.schedule objects included — the E1.7 replace-never-merge
precedent). Inventory keys materialize only at listener level, from
listener columns. Secret markers ride the cascade; redaction and resolution
are explicit second passes.
"""

import copy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.config.canonical import canonical_config_bytes, config_checksum
from app.config.catalog import CATALOG_BY_KEY, LEVELS
from app.config.merge import (
    LevelOverrides,
    ResolvedValue,
    effective_config,
    redact_secrets,
    resolve_secret_refs,
)

INVENTORY = {
    "identity.name": "golden",
    "identity.mac": "02:00:00:00:00:01",
    "location.gps_lat": 47.5,
    "location.gps_lon": -121.5,
}
MAC = "02:00:00:00:00:01"

IDS = {level: f"{level}-id" for level in LEVELS}


def _link(level: str, overrides: dict) -> LevelOverrides:
    return LevelOverrides(level=level, entity_id=IDS[level], overrides=overrides)


def _merge(chain, target_level="listener", inventory=None, inventory_entity_id=None):
    if target_level == "listener" and inventory is None:
        inventory, inventory_entity_id = INVENTORY, MAC
    return effective_config(
        chain,
        CATALOG_BY_KEY,
        target_level=target_level,
        inventory=inventory,
        inventory_entity_id=inventory_entity_id,
    )


# =========================================================================
# 1-2: defaults and single-level wins
# =========================================================================


@pytest.mark.parametrize("level", LEVELS)
def test_empty_chain_yields_catalog_defaults_at_every_level(level):
    """No overrides anywhere -> every key resolves to its catalog default
    with source 'default' and no source entity."""
    config = _merge([], target_level=level)
    for key, rv in config.items():
        if CATALOG_BY_KEY[key].resolution == "inventory":
            continue
        assert rv == ResolvedValue(
            value=CATALOG_BY_KEY[key].default, source="default", source_entity_id=None
        ), key
    # Every non-inventory catalog key appears — an ancestor's effective value
    # is exactly what its descendants inherit, so nothing is filtered by level.
    expected = {k for k, e in CATALOG_BY_KEY.items() if e.resolution != "inventory"}
    if level == "listener":
        expected |= {k for k, e in CATALOG_BY_KEY.items() if e.resolution == "inventory"}
    assert set(config) == expected


@pytest.mark.parametrize("level", LEVELS)
def test_single_level_override_beats_the_default(level):
    """One override at any single level wins over the default, with
    provenance naming that level and entity."""
    config = _merge([_link(level, {"logging.verbosity": "trace"})], target_level=level)
    assert config["logging.verbosity"] == ResolvedValue(
        value="trace", source=level, source_entity_id=IDS[level]
    )


# =========================================================================
# 3-6: the cascade
# =========================================================================


def test_partial_override_deepest_setter_wins():
    """org and pod both set a key -> pod (deeper) wins; keys set nowhere
    stay default; keys set at one level only resolve there."""
    chain = [
        _link("organization", {"audio.sample_rate_hz": 96000, "capture.mode": "continuous"}),
        _link("pod", {"audio.sample_rate_hz": 192000}),
    ]
    config = _merge(chain)
    assert config["audio.sample_rate_hz"].value == 192000
    assert config["audio.sample_rate_hz"].source == "pod"
    assert config["capture.mode"] == ResolvedValue(
        value="continuous", source="organization", source_entity_id=IDS["organization"]
    )
    assert config["audio.bits_per_sample"].source == "default"


def test_full_shadowing_every_level_sets_the_same_key():
    """All five levels set one key -> the listener wins and provenance names
    the listener entity."""
    chain = [
        _link(level, {"logging.verbosity": verbosity})
        for level, verbosity in zip(
            LEVELS, ("error", "warn", "info", "debug", "trace"), strict=True
        )
    ]
    config = _merge(chain)
    assert config["logging.verbosity"] == ResolvedValue(
        value="trace", source="listener", source_entity_id=IDS["listener"]
    )


def test_empty_middle_levels_are_transparent():
    """A level with an empty override map (or no row at all — same thing)
    neither wins nor blocks: the org value flows through pod and aggregator
    to the listener with provenance still naming the organization."""
    chain = [
        _link("organization", {"capture.duty_on_seconds": 90}),
        _link("pod", {}),
        _link("aggregator", {}),
    ]
    config = _merge(chain)
    assert config["capture.duty_on_seconds"] == ResolvedValue(
        value=90, source="organization", source_entity_id=IDS["organization"]
    )


def test_chain_truncation_merges_at_every_entity_level():
    """The same tree, viewed at each level, merges only the chain down to
    that level — and a below-lowest-level key still resolves at an ancestor
    (a deployment's audio.sample_rate_hz is what its listeners inherit)."""
    org = _link("organization", {"logging.verbosity": "warn"})
    dep = _link("deployment", {"audio.sample_rate_hz": 96000})
    pod = _link("pod", {"network.wifi_ssid": "eoe-field"})

    at_org = _merge([org], target_level="organization")
    assert at_org["logging.verbosity"].value == "warn"
    assert at_org["audio.sample_rate_hz"].source == "default"

    at_dep = _merge([org, dep], target_level="deployment")
    assert at_dep["audio.sample_rate_hz"].value == 96000
    assert at_dep["logging.verbosity"].source == "organization"

    at_pod = _merge([org, dep, pod], target_level="pod")
    assert at_pod["network.wifi_ssid"].value == "eoe-field"
    assert at_pod["audio.sample_rate_hz"].value == 96000


def test_malformed_chains_fail_loud():
    """Out-of-order, duplicate, or below-target chains are caller bugs,
    never silently tolerated data."""
    with pytest.raises(ValueError):
        _merge([_link("pod", {}), _link("organization", {})])
    with pytest.raises(ValueError):
        _merge([_link("pod", {}), _link("pod", {})])
    with pytest.raises(ValueError):
        _merge([_link("listener", {})], target_level="pod")
    with pytest.raises(ValueError):
        effective_config([], CATALOG_BY_KEY, target_level="fleet")


# =========================================================================
# 7: wholesale replacement
# =========================================================================


def test_object_values_replace_wholesale_never_field_merge():
    """capture.schedule is an opaque object: a deeper level's value replaces
    the whole thing (E1.7 precedent, D53). No key union, no deep patch."""
    chain = [
        _link("deployment", {"capture.schedule": {"windows": [{"start": "06:00"}], "tz": "UTC"}}),
        _link("listener", {"capture.schedule": {"windows": [{"start": "21:00"}]}}),
    ]
    config = _merge(chain)
    assert config["capture.schedule"].value == {"windows": [{"start": "21:00"}]}
    assert "tz" not in config["capture.schedule"].value


# =========================================================================
# 8-9: secrets
# =========================================================================


def test_secret_modes_raw_redacted_resolved():
    """Raw mode carries the marker verbatim; redact_secrets renders the keep
    sentinel; resolve_secret_refs substitutes plaintext through the injected
    getter. Plaintext never appears in the redacted view."""
    marker = {"$secret": "config:pod:pod-id:network.wifi_password"}
    chain = [_link("pod", {"network.wifi_password": marker})]
    raw = _merge(chain)
    assert raw["network.wifi_password"].value == marker

    redacted = redact_secrets(raw, CATALOG_BY_KEY)
    assert redacted["network.wifi_password"] == ResolvedValue(
        value={"$secret_set": True}, source="pod", source_entity_id=IDS["pod"]
    )
    assert "hunter2" not in repr(redacted)

    resolved = resolve_secret_refs(raw, CATALOG_BY_KEY, {marker["$secret"]: "hunter2"}.__getitem__)
    assert resolved["network.wifi_password"].value == "hunter2"
    # Non-secret keys pass through both transforms untouched.
    assert redacted["audio.sample_rate_hz"] == raw["audio.sample_rate_hz"]
    assert resolved["audio.sample_rate_hz"] == raw["audio.sample_rate_hz"]


def test_unset_secret_stays_none_in_every_mode():
    """A secret nobody set resolves to its default (None) and stays None
    when redacted — the sentinel means SET, absence of it means unset."""
    raw = _merge([])
    assert raw["network.stream_key"] == ResolvedValue(
        value=None, source="default", source_entity_id=None
    )
    redacted = redact_secrets(raw, CATALOG_BY_KEY)
    assert redacted["network.stream_key"].value is None


# =========================================================================
# 10: inventory resolution
# =========================================================================


def test_inventory_keys_resolve_from_listener_columns_only():
    """At listener level the four inventory keys come from the listener row
    with source 'inventory' and the MAC as source entity; chain overrides of
    them are ignored (defense in depth — storage already rejects them); at
    every other level they are omitted entirely."""
    chain = [_link("pod", {"identity.name": "smuggled"})]
    config = _merge(chain)
    assert config["identity.name"] == ResolvedValue(
        value="golden", source="inventory", source_entity_id=MAC
    )
    assert config["location.gps_lat"].value == 47.5
    assert config["identity.mac"].value == MAC

    at_pod = _merge([], target_level="pod")
    for key in ("identity.name", "identity.mac", "location.gps_lat", "location.gps_lon"):
        assert key not in at_pod


# =========================================================================
# 11-13: determinism, purity, tolerance
# =========================================================================


def test_determinism_identical_calls_identical_results():
    chain = [
        _link("organization", {"logging.verbosity": "warn"}),
        _link("pod", {"network.wifi_ssid": "eoe-a"}),
    ]
    first = _merge(chain)
    second = _merge(chain)
    assert first == second
    snapshot = {key: rv.value for key, rv in first.items()}
    again = {key: rv.value for key, rv in second.items()}
    assert canonical_config_bytes(snapshot) == canonical_config_bytes(again)


def test_side_effect_freedom_inputs_never_mutated():
    chain = [_link("deployment", {"capture.schedule": {"windows": []}})]
    chain_before = copy.deepcopy(chain)
    inventory_before = copy.deepcopy(INVENTORY)
    config = _merge(chain)
    assert chain == chain_before
    assert inventory_before == INVENTORY
    # Mutating the output must not reach back into the inputs.
    config["capture.schedule"].value["windows"].append({"start": "00:00"})
    assert chain[0].overrides["capture.schedule"] == {"windows": []}


def test_unknown_override_keys_are_ignored_not_errors():
    """Storage validates writes (E2.2); the merge is defensive on read — a
    key the catalog no longer names simply does not resolve."""
    config = _merge([_link("pod", {"legacy.removed_key": 1, "network.wifi_ssid": "eoe-a"})])
    assert "legacy.removed_key" not in config
    assert config["network.wifi_ssid"].value == "eoe-a"


# =========================================================================
# 14-15: the frozen byte contract (D52)
# =========================================================================


def test_canonicalization_recipe_exact_bytes():
    """Keys sorted at every depth, compact separators, non-ASCII preserved,
    UTF-8, no trailing newline. These bytes are the contract."""
    assert canonical_config_bytes({"b": 1, "a": {"y": 2, "x": [3, 1]}}) == (
        b'{"a":{"x":[3,1],"y":2},"b":1}'
    )


def test_golden_checksums_are_frozen():
    """Pinned hex digests. If any of these change, the wire protocol E3
    matches acks against has changed — that is never a routine test fix.

    A: the full defaults-only listener snapshot (raw effective values, all
       38 keys, the INVENTORY constant above).
    B: non-ASCII strings (locks ensure_ascii=False).
    C: float and int representations (locks Python's repr behavior).

    **Digest A was re-frozen once, at E5.11, and that is the only time.** Spec
    5.3 gained a thirty-eighth row (`services.credentials_generation`, addendum
    SPEC-5-01, D134), so the defaults-only snapshot legitimately gained a key
    and this digest had to move with it — see D137. It is re-frozen only
    because the change was proven to be exactly that and nothing more: the
    assertion below removes the new key and reproduces the ORIGINAL digest
    byte for byte, so a merge-semantics regression hiding inside the re-freeze
    would fail it. Any FUTURE change to these constants is still a
    wire-protocol break rather than a test update.
    """
    config = _merge([])
    snapshot = {key: rv.value for key, rv in config.items()}
    assert len(snapshot) == 38
    assert config_checksum(snapshot) == (
        "sha256:91ff585cd3d0af1e054ad32fd0d2f30034817389e6eae643785036033bd82374"
    )
    # The re-freeze is falsifiable: drop E5.11's key and the pre-E5.11 digest
    # must come back unchanged. This is what makes the line above a recorded
    # addition rather than an unexplained new constant.
    pre_e511 = {k: v for k, v in snapshot.items() if k != "services.credentials_generation"}
    assert len(pre_e511) == 37
    assert config_checksum(pre_e511) == (
        "sha256:3f23f0376667b44ee2f4e819757314afa329a67b5880e29857814d6c8a9153b8"
    )
    assert config_checksum({"network.wifi_ssid": "tømmer-skog", "note": "Okabe–Ito"}) == (
        "sha256:37737241f080280ffdd4b693eb83234ea367c87fb8f0217b6ca6a34865f652c4"
    )
    assert (
        config_checksum({"analysis.confidence_threshold": 0.5, "tiny": 1e-05, "big": 384000})
        == "sha256:a9996994213ba50c462037ae565071353aa9827e7d142ed9424558a596a2e8fc"
    )


def test_checksum_is_key_order_invariant():
    forward = {"a": 1, "b": {"c": 2, "d": 3}}
    backward = {"b": {"d": 3, "c": 2}, "a": 1}
    assert config_checksum(forward) == config_checksum(backward)


# =========================================================================
# Property cases (hypothesis; derandomized via the conftest profile)
# =========================================================================

_PROPERTY_KEYS = {
    "logging.verbosity": st.sampled_from(["error", "warn", "info", "debug", "trace"]),
    "capture.duty_on_seconds": st.integers(min_value=0, max_value=10**6),
    "audio.sample_rate_hz": st.sampled_from([8000, 16000, 48000, 96000, 384000]),
}


@st.composite
def chains(draw, max_depth=5):
    """Storage-legal chains: only listener-lowest / any-level keys, so every
    level may set every key (the at-or-above rule holds trivially)."""
    depth = draw(st.integers(min_value=0, max_value=max_depth))
    links = []
    for level in LEVELS[:depth]:
        overrides = {
            key: draw(strategy) for key, strategy in _PROPERTY_KEYS.items() if draw(st.booleans())
        }
        links.append(LevelOverrides(level=level, entity_id=IDS[level], overrides=overrides))
    return links


@given(chains())
def test_property_deepest_setter_wins_else_default(chain):
    config = _merge(chain, target_level="listener")
    for key in _PROPERTY_KEYS:
        setters = [link for link in chain if key in link.overrides]
        if setters:
            assert config[key].value == setters[-1].overrides[key]
            assert config[key].source == setters[-1].level
            assert config[key].source_entity_id == setters[-1].entity_id
        else:
            assert config[key].source == "default"
            assert config[key].value == CATALOG_BY_KEY[key].default


@given(chains())
def test_property_merge_is_idempotent(chain):
    assert _merge(chain) == _merge(chain)


@given(chains(max_depth=4), st.sampled_from(sorted(_PROPERTY_KEYS)))
def test_property_appending_a_setter_changes_only_that_key(chain, key):
    """Adding a listener-level link that sets exactly one key changes that
    key's resolution and nothing else."""
    before = _merge(chain)
    extended = [
        *chain,
        LevelOverrides(level="listener", entity_id="lst", overrides={key: _pick(key)}),
    ]
    after = _merge(extended)
    for other in before:
        if other == key:
            assert after[other] == ResolvedValue(
                value=_pick(key), source="listener", source_entity_id="lst"
            )
        else:
            assert after[other] == before[other]


def _pick(key):
    return {
        "logging.verbosity": "trace",
        "capture.duty_on_seconds": 7,
        "audio.sample_rate_hz": 8000,
    }[key]


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=8),
        st.one_of(
            st.integers(), st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=8)
        ),
        max_size=8,
    )
)
def test_property_canonical_bytes_ignore_insertion_order(mapping):
    shuffled = dict(reversed(list(mapping.items())))
    assert canonical_config_bytes(mapping) == canonical_config_bytes(shuffled)
