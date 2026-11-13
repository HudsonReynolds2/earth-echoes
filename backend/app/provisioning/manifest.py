"""The bundle's `manifest.json` and human-readable `README.md` (task E4.7; spec
8.3, 14.5; DECISIONS D177). **TEST-CRITICAL.**

The manifest is the only artifact that says what SHOULD be on a card. A bundle
whose manifest disagrees with its own contents is undetectable in the field,
where the card is already written and the device is already in a tree. That is
why spec 14.5 makes it one of the four test-critical components, and why
`tests/test_provisioning_manifest.py` asserts completeness **in both
directions**:

- every archive member except `manifest.json` itself is listed, with a sha256
  that matches its bytes;
- every listed path is an archive member.

The manifest cannot list its own checksum. That is the one exception, and it
is named in the manifest's own `excludes` field rather than left implicit.

## What an entry says (spec 8.3)

For each file: its `path`, its `kind`, its `pod`, the `cards` that take it (the
target MACs, or the Aggregator), a `sha256`, the generating `revisions` (one per
card, as embedded in that card's record), and `expected_contents`, which is
the file's parsed YAML with every secret value replaced by a marker that says
where the real value is. Expected contents never carry a secret: the manifest
sits next to the cards, and the firmware envelope (spec 8.4) would be
pointless if its plaintext were printed beside it.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from app.provisioning.envelope import ALGORITHM, ENVELOPE_FORMAT
from app.provisioning.generate import GeneratedFile

MANIFEST_VERSION = 1
MANIFEST_PATH = "manifest.json"
README_PATH = "README.md"

SEALED_MARKER = "<sealed: firmware ciphertext, see secrets block>"
PLAINTEXT_MARKER = "<plaintext in file by design, spec 16.4>"


@dataclass(frozen=True)
class BundleMeta:
    """What the manifest and README say about the bundle as a whole."""

    bundle_id: uuid.UUID
    deployment_id: uuid.UUID
    deployment_name: str
    pod_names: Mapping[uuid.UUID, str]
    plan: Mapping[str, Mapping[str, Any]]
    template_version: int
    firmware_key_version: int
    bootstrap_credentials: bool


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _redacted(kind: str, content: bytes) -> Any:
    """Expected on-device contents with no secret value in them."""
    document = yaml.safe_load(content)
    if kind in ("pod_file", "listener_file"):
        secrets = document.get("secrets", {})
        for name in ("wrapped_dek", "wifi_password", "stream_key"):
            if name in secrets:
                secrets[name] = SEALED_MARKER
    elif kind == "settings_yaml":
        mqtt = document.get("mqtt", {})
        if "password" in mqtt:
            mqtt["password"] = PLAINTEXT_MARKER
    return document


def _entry(generated: GeneratedFile) -> dict[str, Any]:
    kinds = {record.file_kind for record in generated.records}
    pods = {str(record.pod_id) for record in generated.records}
    assert len(kinds) == 1 and len(pods) == 1, f"{generated.path} mixes kinds or pods"
    (kind,), (pod,) = kinds, pods
    return {
        "path": generated.path,
        "kind": kind,
        "pod": pod,
        "cards": [
            {
                "target_type": record.target_type,
                "target_id": record.target_id,
                "aggregator_uuid": record.aggregator_uuid,
                "revision_id": str(record.config_revision_id)
                if record.config_revision_id is not None
                else None,
                "revision_checksum": record.config_checksum,
            }
            for record in generated.records
        ],
        "sha256": sha256(generated.content),
        "expected_contents": _redacted(kind, generated.content),
    }


def build_readme(meta: BundleMeta, files: Sequence[GeneratedFile]) -> bytes:
    """The field tech's guide: what goes where, and what is deliberate."""
    lines = [
        f"# Provisioning bundle {meta.bundle_id}",
        "",
        f"Deployment: **{meta.deployment_name}**",
        "",
        "Write each file below to the cards it names, **exactly as generated**. "
        "`manifest.json` lists every file with its checksum; check them before you write.",
        "",
        "## Status of these formats",
        "",
        f"- **DRAFT `device_config` v{meta.template_version}, pending firmware sign-off** "
        "(spec 17 item 1). The Listener card format may change before production.",
        "- **DRAFT aggregator `settings.yaml`, pending aggregator-pi sign-off** (DECISIONS D176).",
        f"- Listener secrets are **firmware ciphertext** ({ALGORITHM}, envelope format "
        f"{ENVELOPE_FORMAT}, firmware key version {meta.firmware_key_version}). Only a "
        "Listener's firmware can read them.",
        "- The Aggregator's `settings.yaml` holds its broker login in **plaintext, by design** "
        "(spec 16.4). The Pi's storage travels with the device. If a Pi is lost, revoke its "
        "broker login from the platform.",
        "",
    ]
    if not meta.bootstrap_credentials:
        lines += [
            "## ACTION NEEDED: broker credentials are absent",
            "",
            "This bundle was generated with `EOE_BOOTSTRAP_CREDENTIALS` off. Each Aggregator's "
            "`settings.yaml` lists `mqtt.username` and `mqtt.password` under `missing:`. **The "
            "device cannot reach the broker until they are filled in.**",
            "",
        ]
    lines += ["## What goes where", ""]
    by_pod: dict[str, list[GeneratedFile]] = {}
    for generated in files:
        by_pod.setdefault(str(generated.records[0].pod_id), []).append(generated)
    for pod_id in sorted(by_pod, key=lambda p: meta.pod_names.get(uuid.UUID(p), p)):
        name = meta.pod_names.get(uuid.UUID(pod_id), pod_id)
        mode = meta.plan.get(pod_id, {}).get("mode", "?")
        lines += [f"### {name}", "", f"Pod id `{pod_id}`, mode `{mode}`.", ""]
        for generated in sorted(by_pod[pod_id], key=lambda g: g.path):
            kind = generated.records[0].file_kind
            if kind == "settings_yaml":
                who = f"the Aggregator ({generated.records[0].aggregator_uuid})"
            elif kind == "pod_file":
                who = (
                    f"every card in this Pod except those with their own file "
                    f"({len(generated.records)} cards)"
                )
            else:
                who = f"the Listener {generated.records[0].target_id} only"
            lines.append(f"- `{generated.path}` → {who}")
        lines.append("")
    lines += [
        "## Filling in later",
        "",
        "Shared Pod cards leave each Listener's name, MAC and GPS blank. The device reports "
        "its MAC on first contact, and the name and GPS are filled in from the platform.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def build_manifest(meta: BundleMeta, files: Sequence[GeneratedFile], readme: bytes) -> bytes:
    """`manifest.json`, deterministic for fixed input (sorted, no timestamps).

    `readme` is listed like any other member so the "every member is listed"
    direction holds for it too."""
    entries = sorted((_entry(generated) for generated in files), key=lambda e: e["path"])
    entries.append(
        {
            "path": README_PATH,
            "kind": "readme",
            "pod": None,
            "cards": [],
            "sha256": sha256(readme),
            "expected_contents": None,
        }
    )
    document = {
        "manifest_version": MANIFEST_VERSION,
        "bundle_id": str(meta.bundle_id),
        "deployment": {"id": str(meta.deployment_id), "name": meta.deployment_name},
        "plan": {pod: dict(choice) for pod, choice in sorted(meta.plan.items())},
        "template_version": meta.template_version,
        "envelope": {
            "format": ENVELOPE_FORMAT,
            "algorithm": ALGORITHM,
            "key_version": meta.firmware_key_version,
        },
        "bootstrap_credentials": meta.bootstrap_credentials,
        "excludes": [MANIFEST_PATH],
        "files": entries,
    }
    return (json.dumps(document, indent=2, sort_keys=False) + "\n").encode("utf-8")


def bundle_members(meta: BundleMeta, files: Sequence[GeneratedFile]) -> dict[str, bytes]:
    """Every archive member, by path: the card files, `README.md`, and
    `manifest.json`. Duplicate paths are a generator bug and refused here,
    because a zip would silently keep both and a card writer would pick one."""
    members: dict[str, bytes] = {}
    for generated in files:
        if generated.path in members or generated.path in (README_PATH, MANIFEST_PATH):
            raise ValueError(f"duplicate archive path {generated.path}")
        members[generated.path] = generated.content
    readme = build_readme(meta, files)
    members[README_PATH] = readme
    members[MANIFEST_PATH] = build_manifest(meta, files, readme)
    return members


def verify_members(members: Mapping[str, bytes]) -> list[str]:
    """Every way an assembled bundle disagrees with its own manifest, or [].

    Both directions (spec 14.5): a member the manifest does not list, a listed
    path that is not a member, and a member whose bytes do not match its
    listed sha256. E4.8 runs this on every archive before the bundle is
    recorded, so a manifest that lies never reaches a card writer.
    """
    if MANIFEST_PATH not in members:
        return [f"{MANIFEST_PATH} is missing"]
    document = json.loads(members[MANIFEST_PATH])
    listed = {entry["path"]: entry for entry in document["files"]}
    excluded = set(document.get("excludes", []))
    problems = [
        f"unlisted member {path}"
        for path in sorted(members)
        if path not in listed and path not in excluded
    ]
    problems += [f"listed but absent {path}" for path in sorted(listed) if path not in members]
    problems += [
        f"checksum mismatch {path}"
        for path, entry in sorted(listed.items())
        if path in members and sha256(members[path]) != entry["sha256"]
    ]
    if excluded != {MANIFEST_PATH}:
        problems.append(f"excludes must be exactly [{MANIFEST_PATH}], got {sorted(excluded)}")
    return problems
