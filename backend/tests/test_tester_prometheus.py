"""E5.4c: the Prometheus tester, against two real Prometheus servers.

**Two containers, not one reconfigured.** The whole acceptance criterion is
telling receiver-disabled from credentials-rejected from accepted, and those
are properties of how a server was STARTED - `--web.enable-remote-write-receiver`
is a command-line flag with no runtime equivalent. A fixture that restarted one
container between assertions could not hold both states at once, and every test
here that compares them would have to trust the restart rather than observe it.

Measured against `prom/prometheus:v3.5.0`, and the ordering matters:

| server                    | credentials | POST /api/v1/write   |
| ------------------------- | ----------- | -------------------- |
| with the receiver flag    | good        | 204                  |
| with the receiver flag    | bad         | 401                  |
| WITHOUT the receiver flag | good        | 404 "remote write receiver needs to be enabled..." |
| WITHOUT the receiver flag | bad         | **401**              |

The last row is why the probe reads 401 before it reads 404: Prometheus checks
basic auth before it routes, so a wrong password against a correctly configured
server and a wrong password against a misconfigured one are the same answer.
Reversed, the tester would tell an operator with a typo'd password to go and
edit their Prometheus command line.
"""

import asyncio

import pytest
from conftest import (
    RIG_PASSWORD,
    RIG_USER,
    free_port,
)

from app.services.testers.base import ServiceCredentials
from app.services.testers.prometheus import PrometheusTester

# `rig` is NOT imported: it is a fixture defined in conftest.py and pytest
# discovers it automatically. Importing it binds a SEPARATE fixture object
# into this module, and "session" scope then applies per copy - which built
# the five-container rig once per module and silently undid the phase-5
# section 5 gate-time design (measured: 15 containers, not 5).
pytestmark = pytest.mark.integration


def credentials(read_url: str, write_url: str, user: str, password: str) -> ServiceCredentials:
    return ServiceCredentials(
        service_key="prometheus",
        settings={
            "read_url": read_url,
            "remote_write_url": write_url,
            "remote_write_user": user,
        },
        secrets={"remote_write_password": password},
    )


def for_rig(service, user: str = RIG_USER, password: str = RIG_PASSWORD) -> ServiceCredentials:
    return credentials(service.url, f"{service.url}/api/v1/write", user, password)


def checks_by_name(result):
    return {check.name: check for check in result.checks}


def run(creds: ServiceCredentials):
    return asyncio.run(PrometheusTester().run(creds))


def test_a_prometheus_with_the_receiver_enabled_passes(rig):
    result = run(for_rig(rig.prometheus))
    assert result.outcome == "pass", result.checks
    assert set(checks_by_name(result)) == {"read_query", "remote_write"}


def test_the_three_remote_write_states_are_distinguishable(rig):
    """The acceptance criterion in one assertion.

    Accepted, credentials-rejected and receiver-disabled must be three
    different answers with three different remedies - a boolean "remote write
    ok" would collapse the last two, and they are fixed by editing completely
    different things.
    """
    accepted = checks_by_name(run(for_rig(rig.prometheus)))["remote_write"]
    rejected = checks_by_name(run(for_rig(rig.prometheus, password="wrong")))["remote_write"]
    disabled = checks_by_name(run(for_rig(rig.prometheus_closed)))["remote_write"]

    assert accepted.passed
    assert not rejected.passed
    assert not disabled.passed

    details = {accepted.detail, rejected.detail, disabled.detail}
    assert len(details) == 3, details

    # The remedies are the part the operator acts on, so they must differ too.
    assert rejected.remedy != disabled.remedy
    assert "password" in rejected.remedy.lower()
    assert "--web.enable-remote-write-receiver" in disabled.remedy


def test_a_disabled_receiver_is_not_reported_as_a_credentials_problem(rig):
    """The specific misdiagnosis this unit exists to prevent."""
    disabled = checks_by_name(run(for_rig(rig.prometheus_closed)))["remote_write"]
    assert "404" in disabled.detail
    assert "password" not in disabled.detail.lower()
    assert "credential" not in disabled.detail.lower()


def test_bad_credentials_read_as_auth_on_both_builds(rig):
    """Prometheus checks auth BEFORE routing, so a wrong password looks the
    same whether or not the receiver is enabled. Pinned because the probe's
    branch order depends on it, and a future Prometheus that changed this
    should fail here rather than silently start misreporting."""
    for service in (rig.prometheus, rig.prometheus_closed):
        check = checks_by_name(run(for_rig(service, password="wrong")))["remote_write"]
        assert not check.passed
        assert "credential" in check.detail.lower() or "reject" in check.detail.lower()


def test_the_read_query_fails_on_bad_credentials(rig):
    result = run(for_rig(rig.prometheus, password="wrong"))
    read = checks_by_name(result)["read_query"]
    assert not read.passed
    assert read.remedy


def test_the_up_query_returns_the_self_scrape_series(rig):
    """`up` is the read because every Prometheus scraping anything has it.
    The rig scrapes itself, so a healthy server answers with at least one
    series and the detail says how many."""
    read = checks_by_name(run(for_rig(rig.prometheus)))["read_query"]
    assert read.passed
    assert "0 series" not in read.detail


def test_the_probe_writes_no_samples(rig):
    """**Asserted either way, deliberately** (phase document, E5.4c): whether
    the probe leaves a sample visible to the read query is pinned, so changing
    it later has to be a decision rather than an accident.

    Today it writes nothing - the probe sends an empty remote-write body - so
    nothing the platform's connection test does can appear in an operator's
    monitoring data.
    """
    import httpx

    run(for_rig(rig.prometheus))
    with httpx.Client(auth=(RIG_USER, RIG_PASSWORD), timeout=10.0) as client:
        response = client.get(
            f"{rig.prometheus.url}/api/v1/query",
            params={"query": '{__name__=~"eoe.*"}'},
        )
    assert response.status_code == 200
    assert response.json()["data"]["result"] == [], (
        "the remote-write probe left a series behind; it is supposed to send an empty payload"
    )


def test_an_unreachable_prometheus_fails_both_checks_with_remedies(rig):
    port = free_port()
    result = run(
        credentials(
            f"http://127.0.0.1:{port}",
            f"http://127.0.0.1:{port}/api/v1/write",
            RIG_USER,
            RIG_PASSWORD,
        )
    )
    assert result.outcome == "fail"
    for check in result.checks:
        assert not check.passed
        assert check.remedy.strip()


def test_no_password_appears_in_any_result_or_log(rig, caplog):
    with caplog.at_level("DEBUG"):
        results = [
            run(for_rig(rig.prometheus)),
            run(for_rig(rig.prometheus, password="wrong")),
            run(for_rig(rig.prometheus_closed)),
        ]
    blob = repr(results) + "\n".join(record.getMessage() for record in caplog.records)
    assert RIG_PASSWORD not in blob


def test_every_failing_check_carries_a_remedy(rig):
    for creds in (
        for_rig(rig.prometheus, password="wrong"),
        for_rig(rig.prometheus_closed),
        credentials("not-a-url", "also-not-a-url", RIG_USER, RIG_PASSWORD),
    ):
        result = run(creds)
        for check in result.checks:
            if not check.passed:
                assert check.remedy.strip(), check
