"""Connection testers for the five spec 16.2 deployment services.

E5.3 ships the **framework**: the `ServiceTester` protocol, the structured
result types, the concurrent runner and its timeout budgets. `REGISTRY` started
EMPTY and E5.4a-e fill it one service at a time - a tester registered before it
exists would make `POST .../services/test` report a verdict nothing computed,
which is worse than reporting that a service has no tester yet.

**Registered so far: `mqtt` (E5.4a).** `influx`, `prometheus`, `grafana` and
`s3` arrive with E5.4b-e and are absent until then.

Testers are written in terms of `app/services/clients/` (phase-5 fixed choice
8), which is the only place a deployment service is dialled from. That is what
makes spec 16.5's "reusing the Section 10 read clients" true by construction
when E7 arrives, rather than a promise someone has to keep.
"""

from app.services.testers.base import (
    DEFAULT_TESTER_BUDGET_SECONDS,
    WHOLE_CALL_BUDGET_SECONDS,
    CheckResult,
    ServiceCredentials,
    ServiceTester,
    TesterOutcome,
    TestResult,
    resolve_credentials,
    run_testers,
)
from app.services.testers.mqtt import MqttTester

#: `service_key` -> the tester for it. E5.4a-e register here; until then a
#: service simply has no tester and the runner says so.
#:
#: Populated at import time rather than by a decorator on each class: the
#: endpoint reads this dict through the MODULE, so what is in it has to be a
#: function of what has been imported, and one literal that a reader can
#: check against `models.SERVICE_KEYS` beats five registrations scattered
#: across five files.
REGISTRY: dict[str, ServiceTester] = {
    "mqtt": MqttTester(),
}

__all__ = [
    "DEFAULT_TESTER_BUDGET_SECONDS",
    "REGISTRY",
    "WHOLE_CALL_BUDGET_SECONDS",
    "CheckResult",
    "MqttTester",
    "ServiceCredentials",
    "ServiceTester",
    "TestResult",
    "TesterOutcome",
    "resolve_credentials",
    "run_testers",
]
