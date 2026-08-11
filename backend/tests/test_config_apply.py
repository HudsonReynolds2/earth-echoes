"""Gate 36: E2.6 bulk preview/apply + the revisions read surface (spec 5.2,
6.1, 14.4; DECISIONS D55-D56).

The load-bearing acceptances: preview matches what apply then produces
(same body, same plan builder - asserted end to end against revision
snapshots and checksums); apply is transactional across the selection;
every revision is draft and the publish flag exists and is off; write-at-
level resolves the single common ancestor or 422s on a split; no-op
devices appear in preview and get no revision; one audit row per affected
deployment; snapshots keep secrets as markers and exclude service keys
from listener payloads.
"""

import uuid

import pytest
from conftest import make_kek
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_auth import PASSWORD, pg_url  # noqa: F401  (module fixture reuse)

from app.auth.passwords import hash_password
from app.main import API_PREFIX, create_app
from app.models import (
    Aggregator,
    AuditLog,
    ConfigRevision,
    Deployment,
    EntityOverride,
    Listener,
    Organization,
    Pod,
    RoleAssignment,
    User,
)
from app.settings import Settings

pytestmark = pytest.mark.integration

OWNER = "apply-owner@example.com"
OP_A = "apply-op-a@example.com"

MACS_P1 = ["02:E2:06:00:01:01", "02:E2:06:00:01:02"]
MAC_P2 = "02:E2:06:00:02:01"
MAC_B = "02:E2:06:00:03:01"


@pytest.fixture(scope="module")
def apply_app(pg_url):  # noqa: F811
    app = create_app(
        Settings(
            database_url=pg_url,
            session_secret="gate36-test-secret",
            kek=make_kek(),
            cors_origins="",
        )
    )
    factory = app.state.session_factory
    with factory() as db:
        org = Organization(name="apply-org")
        db.add(org)
        db.flush()
        dep_a = Deployment(organization_id=org.id, name="apply-dep-a", slug="apply-dep-a")
        dep_b = Deployment(organization_id=org.id, name="apply-dep-b", slug="apply-dep-b")
        db.add_all([dep_a, dep_b])
        db.flush()
        pod_1 = Pod(deployment_id=dep_a.id, name="apply-pod-1")
        pod_2 = Pod(deployment_id=dep_a.id, name="apply-pod-2")
        pod_b = Pod(deployment_id=dep_b.id, name="apply-pod-b")
        db.add_all([pod_1, pod_2, pod_b])
        db.flush()
        agg_1 = Aggregator(pod_id=pod_1.id, aggregator_uuid="apply-agg-1")
        agg_2 = Aggregator(pod_id=pod_2.id, aggregator_uuid="apply-agg-2")
        agg_b = Aggregator(pod_id=pod_b.id, aggregator_uuid="apply-agg-b")
        db.add_all([agg_1, agg_2, agg_b])
        db.flush()
        for index, mac in enumerate(MACS_P1):
            db.add(
                Listener(
                    mac=mac,
                    name=f"apply-lst-1{index + 1}",
                    aggregator_id=agg_1.id,
                    deployment_id=dep_a.id,
                    tags=["wave"],
                )
            )
        db.add_all(
            [
                Listener(
                    mac=MAC_P2,
                    name="apply-lst-21",
                    aggregator_id=agg_2.id,
                    deployment_id=dep_a.id,
                ),
                Listener(
                    mac=MAC_B,
                    name="apply-lst-b1",
                    aggregator_id=agg_b.id,
                    deployment_id=dep_b.id,
                    tags=["wave"],
                ),
            ]
        )
        for email, role, scope in (
            (OWNER, "owner", None),
            (OP_A, "deployment_operator", dep_a.id),
        ):
            user = User(email=email, password_hash=hash_password(PASSWORD))
            user.role_assignments.append(RoleAssignment(role=role, deployment_id=scope))
            db.add(user)
        db.commit()
        app.state.ids = {
            "org": str(org.id),
            "dep_a": str(dep_a.id),
            "dep_b": str(dep_b.id),
            "pod_1": str(pod_1.id),
            "pod_2": str(pod_2.id),
            "agg_1": str(agg_1.id),
        }
    return app


def _login(app, email: str) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"{API_PREFIX}/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["eoe_csrf"]}


def _preview(client, body, **params):
    return client.post(f"{API_PREFIX}/config/preview", json=body, params=params)


def _apply(client, body):
    return client.post(f"{API_PREFIX}/config/apply", json=body, headers=_csrf(client))


# =========================================================================
# Preview == apply
# =========================================================================


def test_preview_matches_what_apply_then_produces(apply_app):
    owner = _login(apply_app, OWNER)
    body = {
        "selection": {"entity_type": "listener", "where": {"ids": [MACS_P1[0]]}},
        "changes": {"capture.duty_on_seconds": 120, "logging.verbosity": "debug"},
        "level": "target",
    }
    preview = _preview(owner, body)
    assert preview.status_code == 200
    (device,) = preview.json()["items"]
    assert device["target_id"] == MACS_P1[0]
    assert device["changed_keys"] == ["capture.duty_on_seconds", "logging.verbosity"]
    assert device["no_op"] is False
    assert device["pod_name"] == "apply-pod-1"
    assert device["before"]["capture.duty_on_seconds"]["value"] == 60
    assert device["after"]["capture.duty_on_seconds"] == {
        "value": 120,
        "source": "listener",
        "source_entity_id": MACS_P1[0],
    }

    applied = _apply(owner, body)
    assert applied.status_code == 200
    result = applied.json()
    assert result["state"] == "draft"
    # E3.13 flipped the default on (D61); this app holds no broker
    # connection, so nothing was published and draft is the honest answer.
    assert result["publish_enabled"] is True
    assert result["published"] == 0
    (revision,) = result["revisions"]
    assert revision["target_id"] == MACS_P1[0]
    assert revision["changed_keys"] == ["capture.duty_on_seconds", "logging.verbosity"]

    # The revision snapshot IS the previewed after-state, values verbatim.
    factory = apply_app.state.session_factory
    with factory() as db:
        row = db.get(ConfigRevision, uuid.UUID(revision["revision_id"]))
        assert row is not None
        assert row.state == "draft"
        assert row.schema_version == 1
        assert row.checksum == revision["checksum"]
        assert row.checksum.startswith("sha256:")
        assert row.snapshot["capture.duty_on_seconds"] == 120
        assert row.snapshot["logging.verbosity"] == "debug"
    # And the live effective config now equals the previewed after.
    effective = owner.get(f"{API_PREFIX}/listeners/{MACS_P1[0]}/config/effective").json()
    assert effective["config"]["capture.duty_on_seconds"]["value"] == 120


def test_write_at_common_ancestor_one_override_row_full_blast_radius(apply_app):
    owner = _login(apply_app, OWNER)
    ids = apply_app.state.ids
    body = {
        "selection": {"entity_type": "listener", "where": {"ids": MACS_P1}},
        "changes": {"network.wifi_security": "WPA2"},
        "level": "pod",
    }
    preview = _preview(owner, body)
    assert preview.status_code == 200
    items = preview.json()["items"]
    # The pod write reaches the pod's aggregator AND both listeners - the
    # honest blast radius, not just the matched pair.
    assert [(item["target_type"], item["target_id"]) for item in items] == [
        ("aggregator", ids["agg_1"]),
        ("listener", MACS_P1[0]),
        ("listener", MACS_P1[1]),
    ]
    applied = _apply(owner, body)
    assert applied.status_code == 200
    assert len(applied.json()["revisions"]) == 3

    factory = apply_app.state.session_factory
    with factory() as db:
        row = db.scalar(
            select(EntityOverride).where(
                EntityOverride.entity_type == "pod", EntityOverride.entity_id == ids["pod_1"]
            )
        )
        assert row is not None and row.overrides["network.wifi_security"] == "WPA2"


def test_split_ancestor_is_a_422_naming_the_candidates(apply_app):
    owner = _login(apply_app, OWNER)
    ids = apply_app.state.ids
    response = _preview(
        owner,
        {
            "selection": {"entity_type": "listener", "where": {"ids": [MACS_P1[0], MAC_P2]}},
            "changes": {"network.wifi_security": "WPA2"},
            "level": "pod",
        },
    )
    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert detail["level"] == "pod"
    assert sorted(detail["ancestors"]) == sorted([ids["pod_1"], ids["pod_2"]])


def test_changes_validate_at_the_write_level(apply_app):
    owner = _login(apply_app, OWNER)
    response = _preview(
        owner,
        {
            "selection": {"entity_type": "listener", "where": {"ids": [MACS_P1[0]]}},
            "changes": {"network.wifi_ssid": "nope"},  # pod-lowest key at listener level
            "level": "target",
        },
    )
    assert response.status_code == 422
    (error,) = response.json()["error"]["detail"]["errors"]
    assert error["code"] == "level_rule"
    assert "network.wifi_ssid" in error["message"]


def test_org_level_requires_org_wide_grant(apply_app):
    operator = _login(apply_app, OP_A)
    response = _apply(
        operator,
        {
            "selection": {"entity_type": "listener"},
            "changes": {"logging.verbosity": "warn"},
            "level": "organization",
        },
    )
    assert response.status_code == 403


def test_no_op_devices_previewed_but_never_revisioned(apply_app):
    owner = _login(apply_app, OWNER)
    body = {
        "selection": {"entity_type": "listener", "where": {"ids": [MAC_P2]}},
        "changes": {"capture.mode": "duty_cycle"},  # already the default
        "level": "target",
    }
    preview = _preview(owner, body)
    (device,) = preview.json()["items"]
    assert device["no_op"] is True
    assert device["changed_keys"] == []
    applied = _apply(owner, body)
    assert applied.status_code == 200
    assert applied.json()["revisions"] == []


def test_cross_deployment_apply_audits_once_per_deployment(apply_app):
    owner = _login(apply_app, OWNER)
    ids = apply_app.state.ids
    body = {
        "selection": {"entity_type": "listener", "where": {"tag": "wave"}},
        "changes": {"buffering.sd_max_bytes": 4096},
        "level": "target",
    }
    applied = _apply(owner, body)
    assert applied.status_code == 200
    revisions = applied.json()["revisions"]
    assert {r["deployment_id"] for r in revisions} == {ids["dep_a"], ids["dep_b"]}

    factory = apply_app.state.session_factory
    with factory() as db:
        rows = db.scalars(
            select(AuditLog).where(AuditLog.action == "config.apply").order_by(AuditLog.at)
        ).all()
        latest = rows[-2:]
    assert {str(row.scope) for row in latest} == {ids["dep_a"], ids["dep_b"]}
    for row in latest:
        assert row.detail is not None
        assert row.detail["level"] == "target"
        assert row.detail["changed_keys"] == ["buffering.sd_max_bytes"]
        assert row.detail["revision_ids"]
        assert "4096" not in str(row.detail["changed_keys"])  # names, never values


def test_apply_is_transactional_across_the_selection(apply_app, monkeypatch):
    owner = _login(apply_app, OWNER)
    factory = apply_app.state.session_factory
    with factory() as db:
        overrides_before = db.scalar(select(func.count()).select_from(EntityOverride)) or 0
        revisions_before = db.scalar(select(func.count()).select_from(ConfigRevision)) or 0
        audits_before = (
            db.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "config.apply")
            )
            or 0
        )

    import app.config.plan as plan_module

    calls = {"n": 0}
    real = plan_module.config_checksum

    def explode_on_second(snapshot):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("injected failure mid-apply")
        return real(snapshot)

    monkeypatch.setattr(plan_module, "config_checksum", explode_on_second)
    response = _apply(
        owner,
        {
            "selection": {"entity_type": "listener", "where": {"ids": MACS_P1}},
            "changes": {"audio.bits_per_sample": 24},
            "level": "target",
        },
    )
    assert response.status_code == 500
    with factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(EntityOverride)) or 0
        ) == overrides_before
        assert (
            db.scalar(select(func.count()).select_from(ConfigRevision)) or 0
        ) == revisions_before
        audits_after = (
            db.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "config.apply")
            )
            or 0
        )
        assert audits_after == audits_before
    # And the change never reached effective config.
    effective = owner.get(f"{API_PREFIX}/listeners/{MACS_P1[0]}/config/effective").json()
    assert effective["config"]["audio.bits_per_sample"]["value"] == 16


def test_secret_changes_snapshot_as_markers_and_split_by_target_type(apply_app):
    owner = _login(apply_app, OWNER)
    ids = apply_app.state.ids
    plaintext = f"bulk-psk-{uuid.uuid4().hex}"
    body = {
        "selection": {"entity_type": "listener", "where": {"ids": MACS_P1}},
        "changes": {"network.stream_key": plaintext},
        "level": "pod",
    }
    applied = _apply(owner, body)
    assert applied.status_code == 200
    assert plaintext not in applied.text
    factory = apply_app.state.session_factory
    with factory() as db:
        rows = db.scalars(
            select(ConfigRevision).where(
                ConfigRevision.id.in_(
                    [uuid.UUID(r["revision_id"]) for r in applied.json()["revisions"]]
                )
            )
        ).all()
    marker = {"$secret": f"config:pod:{ids['pod_1']}:network.stream_key"}
    for row in rows:
        assert row.snapshot["network.stream_key"] == marker
        assert plaintext not in str(row.snapshot)
        if row.target_type == "listener":
            # Spec 5.4: service keys never reach listener-bound payloads.
            assert "telemetry.influx_url" not in row.snapshot
            assert "identity.mac" in row.snapshot
        else:
            assert "telemetry.influx_url" in row.snapshot


def test_selection_ref_resolves_and_missing_ref_404s(apply_app):
    owner = _login(apply_app, OWNER)
    created = owner.post(
        f"{API_PREFIX}/selections",
        json={
            "name": "apply wave",
            "query": {"entity_type": "listener", "where": {"tag": "wave"}},
        },
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    by_ref = _preview(
        owner,
        {
            "selection": {"selection_id": created.json()["id"]},
            "changes": {"logging.verbosity": "info"},
        },
    )
    assert by_ref.status_code == 200
    assert by_ref.json()["total"] == 3  # MACS_P1 (wave) + MAC_B (wave)

    missing = _preview(
        owner,
        {
            "selection": {"selection_id": str(uuid.uuid4())},
            "changes": {"logging.verbosity": "info"},
        },
    )
    assert missing.status_code == 404


def test_scoped_operator_blast_radius_excludes_other_deployments(apply_app):
    operator = _login(apply_app, OP_A)
    preview = _preview(
        operator,
        {
            "selection": {"entity_type": "listener", "where": {"tag": "wave"}},
            "changes": {"buffering.sd_enabled": False},
        },
    )
    assert preview.status_code == 200
    assert {item["deployment_id"] for item in preview.json()["items"]} == {
        apply_app.state.ids["dep_a"]
    }


# =========================================================================
# Revisions read surface
# =========================================================================


def test_revisions_list_and_item_with_scope_discipline(apply_app):
    owner = _login(apply_app, OWNER)
    listed = owner.get(f"{API_PREFIX}/listeners/{MACS_P1[0]}/revisions")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 2
    stamps = [item["created_at"] for item in body["items"]]
    assert stamps == sorted(stamps, reverse=True)  # -created_at default
    for item in body["items"]:
        assert item["state"] == "draft"
        assert "snapshot" not in item  # list items stay light

    drafts = owner.get(f"{API_PREFIX}/listeners/{MACS_P1[0]}/revisions", params={"state": "draft"})
    assert drafts.json()["total"] == body["total"]
    none = owner.get(f"{API_PREFIX}/listeners/{MACS_P1[0]}/revisions", params={"state": "applied"})
    assert none.json()["total"] == 0

    aggregator_listed = owner.get(
        f"{API_PREFIX}/aggregators/{apply_app.state.ids['agg_1']}/revisions"
    )
    assert aggregator_listed.status_code == 200
    assert aggregator_listed.json()["total"] >= 1

    item_id = body["items"][0]["id"]
    item = owner.get(f"{API_PREFIX}/revisions/{item_id}")
    assert item.status_code == 200
    assert item.json()["snapshot"]

    operator = _login(apply_app, OP_A)
    out_of_scope = operator.get(f"{API_PREFIX}/listeners/{MAC_B}/revisions")
    missing = operator.get(f"{API_PREFIX}/listeners/02:00:00:00:00:99/revisions")
    assert out_of_scope.status_code == missing.status_code == 404
    assert out_of_scope.json()["error"] == missing.json()["error"]
    assert operator.get(f"{API_PREFIX}/revisions/{uuid.uuid4()}").status_code == 404


def test_publish_is_enabled_by_default_and_apply_still_stops_at_draft_without_a_broker(
    apply_app,
):
    """E3.13 flipped this default ON (D61); this test was
    `test_publish_flag_exists_and_is_off` until then.

    The flag being on is not enough to reach a device: this app never ran its
    lifespan, so it holds no outbound connection (D86) and every revision
    stays `draft` and says so. That is the same answer a real API gives when
    its broker is down, which is why apply reports per-revision state rather
    than assuming success.
    """
    assert apply_app.state.settings.publish_enabled is True
    assert getattr(apply_app.state, "mqtt", None) is None
