"""E5.4d: the Grafana tester, against a real Grafana.

**This is the one tester that mutates a system the operator also edits by
hand**, so the assertion that matters is not "it worked" but "running it twice
leaves exactly one of everything" - and it is diffed against Grafana's OWN
listing rather than against what the tester believes it did. A tester that
graded its own idempotence could agree with itself.

The other property this file pins is that **provisioning is never a side effect
of a test**. Spec 16.2 has this step OFFER to provision missing datasources;
the phase document requires the offer and the act to be separate calls. So the
suite asserts a full `run()` against a Grafana with no datasources creates
none, and that `provision_datasource` - a different method, called
deliberately - is what creates them.
"""

import asyncio

import pytest
from conftest import free_port

from app.services.clients.grafana import CONTACT_POINT_NAME, WEBHOOK_PATH, GrafanaClient
from app.services.testers.base import ServiceCredentials
from app.services.testers.grafana import GrafanaTester

# `rig` is NOT imported: it is a fixture defined in conftest.py and pytest
# discovers it automatically. Importing it binds a SEPARATE fixture object
# into this module, and "session" scope then applies per copy - which built
# the five-container rig once per module and silently undid the phase-5
# section 5 gate-time design (measured: 15 containers, not 5).
pytestmark = pytest.mark.integration


def credentials(base_url: str, token: str) -> ServiceCredentials:
    return ServiceCredentials(
        service_key="grafana",
        settings={"base_url": base_url},
        secrets={"service_account_token": token},
    )


def checks_by_name(result):
    return {check.name: check for check in result.checks}


def run(creds: ServiceCredentials):
    return asyncio.run(GrafanaTester().run(creds))


def listing(rig, path: str):
    """Ask Grafana itself what exists, on a fresh client."""
    client = GrafanaClient(base_url=rig.grafana.url, token=rig.grafana_token)

    async def fetch():
        async with client.session() as session:
            if path == "contact-points":
                return await client.contact_points(session)
            return await client.datasources(session)

    return asyncio.run(fetch())


def test_a_healthy_grafana_passes_every_check(rig):
    result = run(credentials(rig.grafana.url, rig.grafana_token))
    assert result.outcome == "pass", result.checks
    assert set(checks_by_name(result)) == {"health", "datasources", "contact_point"}


def test_running_twice_leaves_exactly_one_contact_point(rig):
    """The acceptance criterion, diffed against Grafana's own listing.

    This is the assertion the whole unit is built around: the platform writes
    into a system a human also edits, so the second run must find the first
    run's object rather than add another. Duplicates would deliver every alert
    more than once.
    """
    run(credentials(rig.grafana.url, rig.grafana_token))
    after_first = [
        row for row in listing(rig, "contact-points") if row.get("name") == CONTACT_POINT_NAME
    ]
    run(credentials(rig.grafana.url, rig.grafana_token))
    after_second = [
        row for row in listing(rig, "contact-points") if row.get("name") == CONTACT_POINT_NAME
    ]

    assert len(after_first) == 1, after_first
    assert len(after_second) == 1, after_second


def test_the_contact_point_targets_the_platform_webhook_route(rig):
    """The URL points at `POST /webhooks/grafana-alerts`, **which E7.6
    implements and this phase does not**. Pinned so the route E7.6 has to
    build is written down in a test as well as in a comment."""
    run(credentials(rig.grafana.url, rig.grafana_token))
    ours = [row for row in listing(rig, "contact-points") if row.get("name") == CONTACT_POINT_NAME]
    assert len(ours) == 1
    assert ours[0]["settings"]["url"].endswith(WEBHOOK_PATH)


def test_the_second_run_reports_the_contact_point_as_already_present(rig):
    run(credentials(rig.grafana.url, rig.grafana_token))
    second = checks_by_name(run(credentials(rig.grafana.url, rig.grafana_token)))["contact_point"]
    assert second.passed
    assert "present" in second.detail


def test_a_test_run_provisions_no_datasources(rig):
    """Provisioning is never a side effect of a test (phase document, E5.4d).

    The rig's Grafana has no datasources. A full run reports what it COULD
    provision and creates nothing, which is the difference between offering
    and doing.
    """
    before = listing(rig, "datasources")
    result = run(credentials(rig.grafana.url, rig.grafana_token))
    after = listing(rig, "datasources")

    assert len(after) == len(before)
    datasources = checks_by_name(result)["datasources"]
    # Missing datasources do NOT fail the tester: an offer is not a verdict,
    # and file-provisioned datasources are a correct configuration.
    assert datasources.passed


def test_provisioning_is_a_separate_deliberate_call_and_is_idempotent(rig):
    client = GrafanaClient(base_url=rig.grafana.url, token=rig.grafana_token)

    async def provision_twice():
        async with client.session() as session:
            first = await client.provision_datasource(
                session, "eoe-prometheus", "prometheus", rig.prometheus.url
            )
            second = await client.provision_datasource(
                session, "eoe-prometheus", "prometheus", rig.prometheus.url
            )
            return first, second

    first, second = asyncio.run(provision_twice())
    assert first == "created"
    assert second == "present"

    of_type = [row for row in listing(rig, "datasources") if row.get("type") == "prometheus"]
    assert len(of_type) == 1, of_type


def test_a_bad_token_fails_on_a_check_that_is_not_health(rig):
    """`/api/health` needs no token, so it still passes - which is exactly the
    point of running it first. The failure lands on the checks that do need
    one, and the remedy names the token rather than the URL."""
    result = run(credentials(rig.grafana.url, "glsa_not_a_real_token"))
    assert result.outcome == "fail"
    named = checks_by_name(result)
    assert named["health"].passed
    assert not named["datasources"].passed
    assert "token" in named["datasources"].remedy.lower()


def test_a_url_that_is_not_grafana_fails_at_health_and_stops(rig):
    """Nothing after health can mean anything if the URL is not a Grafana, so
    the tester returns one check rather than three variations of the same
    failure."""
    result = run(credentials(f"http://127.0.0.1:{free_port()}", rig.grafana_token))
    assert result.outcome == "fail"
    assert [check.name for check in result.checks] == ["health"]
    assert result.checks[0].remedy


def test_no_token_appears_in_any_result_or_log(rig, caplog):
    with caplog.at_level("DEBUG"):
        results = [
            run(credentials(rig.grafana.url, rig.grafana_token)),
            run(credentials(rig.grafana.url, "glsa_wrong")),
        ]
    blob = repr(results) + "\n".join(record.getMessage() for record in caplog.records)
    assert rig.grafana_token not in blob


def test_every_failing_check_carries_a_remedy(rig):
    for creds in (
        credentials(rig.grafana.url, "glsa_wrong"),
        credentials("not-a-url", rig.grafana_token),
    ):
        for check in run(creds).checks:
            if not check.passed:
                assert check.remedy.strip(), check
