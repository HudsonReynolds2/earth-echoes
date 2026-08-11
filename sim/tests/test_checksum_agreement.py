"""SIM.1 acceptance: sim's checksum agrees with the platform's, byte for byte.

This is the test the "reimplement, do not import" fixed choice exists for. The
harness stands in for firmware, and firmware has the D52 recipe written down,
not the platform's function to call. If the two implementations ever disagree
— on key order at depth, on how a non-ASCII string is encoded, on whether a
float is `1.0` or `1`, on a trailing newline — then the recipe as WRITTEN does
not produce what the platform computes, and every device in the field would
report a checksum that can never match. That failure surfaces here, in a
second, rather than in a fleet that silently reads as drifted forever.

`app.config.canonical` is the platform's implementation and the reference;
`checksum` is sim's, written from the description in DECISIONS D52.
"""

import re
from pathlib import Path

from app.config.canonical import canonical_config_bytes
from app.config.canonical import config_checksum as platform_checksum
from hypothesis import given
from hypothesis import strategies as st

from checksum import canonical_bytes, config_checksum

#: The shape the contract's `Checksum` field type pins. A recipe that produced
#: anything else would be rejected at the wire boundary rather than mismatch.
CHECKSUM_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Snapshots chosen for the ways two JSON encoders diverge, spelled out rather
#: than generated so the intent survives: key order at every depth, non-ASCII
#: left alone, floats versus ints, empty containers, nulls inside `config`
#: (which are DATA and must not be dropped), and the secret markers a real
#: snapshot carries (spec 5.4).
TRICKY_SNAPSHOTS: list[dict[str, object]] = [
    {},
    {"logging.verbosity": "debug"},
    {"z": 1, "a": 2, "m": 3},
    {"outer": {"z": 1, "a": {"z": True, "a": None}}, "aardvark": []},
    {"site.name": "Réserve d'Ouvéa", "note": "日本語", "emoji": "🌍"},
    {"gain": 1.0, "count": 1, "ratio": 0.1, "negative_zero": -0.0, "big": 10**18},
    {"capture.enabled": False, "capture.window": [None, 1, "two", {"b": 2, "a": 1}]},
    {"mqtt.password": {"$secret": "config:aggregator:demo-agg-rc-01:mqtt.password"}},
    {"empty_map": {}, "empty_list": [], "empty_string": ""},
]

_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=12,
)
_snapshots = st.dictionaries(st.text(min_size=1), _json_values, max_size=8)


def test_the_two_implementations_agree_on_the_hard_cases():
    """ACCEPTANCE (phase doc SIM.1): byte-for-byte agreement, on inputs chosen
    for where encoders differ rather than for where they are the same."""
    for snapshot in TRICKY_SNAPSHOTS:
        assert canonical_bytes(snapshot) == canonical_config_bytes(snapshot), snapshot
        assert config_checksum(snapshot) == platform_checksum(snapshot), snapshot


@given(_snapshots)
def test_the_two_implementations_agree_on_generated_snapshots(snapshot):
    """The same claim over generated nested structures, under the backend's
    derandomized hypothesis profile (loaded by importing its conftest), so
    this is green or red for a reason rather than by luck."""
    assert canonical_bytes(snapshot) == canonical_config_bytes(snapshot)
    assert config_checksum(snapshot) == platform_checksum(snapshot)


def test_the_checksum_has_the_shape_the_wire_contract_pins():
    for snapshot in TRICKY_SNAPSHOTS:
        assert CHECKSUM_SHAPE.match(config_checksum(snapshot)), snapshot


def test_the_canonical_bytes_carry_no_trailing_newline():
    """Named separately because it is the easiest clause of the recipe to lose
    to a helpful editor: one `\\n` appended and every digest in the fleet moves."""
    assert not canonical_bytes({"a": 1}).endswith(b"\n")
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_sim_computes_the_checksum_itself():
    """The fixed choice, enforced rather than promised: `sim/checksum.py` must
    not reach for `app.config.canonical`. A simulator that called the
    platform's own function would prove only that the function is
    self-consistent, and would pass on the day the written recipe and the code
    stopped matching."""
    source = (Path(__file__).resolve().parents[1] / "checksum.py").read_text(encoding="utf-8")
    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "app" in line.split()[1].split(".")], imports
