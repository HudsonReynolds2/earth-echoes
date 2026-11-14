"""The spec 9.3 displayed status, in one place (task E3.12; D60).

Spec 9.3: "A device's displayed status derives from its online/offline signal,
reconciliation state (applied/drifted/failed), and active alerts from
Grafana." This module is that derivation, and it is the ONLY one — the
inventory tables, the aggregator card and the Overview roll-up all read it, so
three surfaces cannot quietly disagree about whether a device is healthy.

**This is what lifts D40.** That guard forbade any status chip anywhere in the
UI because E1 and E2 had nothing real to put in one, and an invented status is
worse than none: an operator who learns that the dots are decorative stops
reading them, including on the day one is telling the truth. E3 supplies the
real signals — LWT (E3.8), spec 6.5 liveness (E3.9), revision state (E3.6) —
so the guard is replaced rather than deleted: status renders where real state
exists, and `UNKNOWN` renders where it does not.

**`UNKNOWN` is a first-class answer and must never be painted as healthy.** A
device that has been entered in inventory but has never spoken has no status;
saying so is honest, and defaulting it to healthy would report a deployment
that has never come online as a working one.

Alerts are applied by `with_alerts` (E6.4, D191): an open alert on a device
makes it `alerting`, which outranks everything. E6 feeds it the open
`inventory_alert` rows; E7 adds Grafana alerts to the same count, through
`app/map/alerts.py`, without touching this module again.
"""

from enum import StrEnum

from app.controlplane.liveness import listener_verdict


class DeviceStatus(StrEnum):
    """The closed vocabulary the frontend's `StatusChip` renders, plus
    `UNKNOWN`, which is not a chip — see the module docstring."""

    HEALTHY = "healthy"
    SLEEPING = "sleeping"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    ALERTING = "alerting"  # E7
    DRIFTED = "drifted"
    UNKNOWN = "unknown"


#: Worst-first, for the parent roll-up. Spec 9.3: "a Deployment or Pod marker
#: reflects the worst status among descendants". `UNKNOWN` sits at the BOTTOM:
#: one silent device among ten healthy ones should not make a whole deployment
#: read as unknown, but it must still outrank nothing.
SEVERITY: tuple[DeviceStatus, ...] = (
    DeviceStatus.ALERTING,
    DeviceStatus.OFFLINE,
    DeviceStatus.DEGRADED,
    DeviceStatus.DRIFTED,
    DeviceStatus.SLEEPING,
    DeviceStatus.HEALTHY,
    DeviceStatus.UNKNOWN,
)


def aggregator_status(*, online: bool | None, revision_state: str | None) -> DeviceStatus:
    """One Aggregator's displayed status.

    **Reachability outranks reconciliation**, and the order matters. A device
    that is offline may well also have a drifted revision, but "offline" is
    what an operator must act on: the drift cannot be repaired until the
    device is back, and showing `drifted` would send them to fix a config when
    the box is unplugged.

    `online is None` means no status message has ever arrived (E3.8 writes no
    row until one does), which is different from a device that told us it was
    leaving.
    """
    if online is None:
        return DeviceStatus.UNKNOWN
    if not online:
        return DeviceStatus.OFFLINE
    return _from_revision(revision_state)


def listener_status(*, liveness_state: str | None, revision_state: str | None) -> DeviceStatus:
    """One Listener's displayed status.

    Listeners hold no MQTT session (spec 6.4), so reachability comes from the
    Aggregator-tracked spec 6.5 liveness. **`sleeping` is its own status and is
    NOT a problem** — it is rendered distinctly because an operator seeing a
    duty-cycled fleet at night needs to know the silence is expected, and
    `StatusChip` gives it its own glyph for exactly that reason.
    """
    verdict = listener_verdict(liveness_state)
    if verdict == "unknown":
        return DeviceStatus.UNKNOWN
    if verdict == "offline":
        return DeviceStatus.OFFLINE
    if liveness_state == "sleeping":
        return DeviceStatus.SLEEPING
    return _from_revision(revision_state)


def _from_revision(revision_state: str | None) -> DeviceStatus:
    """The reconciliation half of spec 9.3, for a device that IS reachable.

    `failed` is `degraded` rather than `offline`: the device is talking, it
    just could not apply what it was given. `pending` and `draft` are healthy —
    a config change in flight is not a fault, and painting one as degraded
    would make every routine edit look like an incident.
    """
    if revision_state == "drifted":
        return DeviceStatus.DRIFTED
    if revision_state == "failed":
        return DeviceStatus.DEGRADED
    return DeviceStatus.HEALTHY


def with_alerts(status: DeviceStatus, open_alerts: int) -> DeviceStatus:
    """A device's displayed status once its active alerts are counted
    (spec 9.3: status derives from reachability, reconciliation, AND alerts).

    **An open alert wins over every other state, `unknown` included.** An
    alert is a real signal about this device that someone must act on, and
    `alerting` is the top of `SEVERITY` for exactly that reason. A separate
    function rather than a third argument to `aggregator_status` and
    `listener_status`, so those two keep the signatures E3's suite pins and
    every caller that predates alerts keeps working unchanged.
    """
    return DeviceStatus.ALERTING if open_alerts > 0 else status


def rollup(statuses: list[DeviceStatus]) -> DeviceStatus:
    """The worst status among descendants (spec 9.3), for a Pod, Deployment or
    the Organization. An empty set is `UNKNOWN`: a deployment with no devices
    has no health to report, and calling it healthy would be a claim about
    nothing."""
    if not statuses:
        return DeviceStatus.UNKNOWN
    present = set(statuses)
    for status in SEVERITY:
        if status in present:
            return status
    return DeviceStatus.UNKNOWN
