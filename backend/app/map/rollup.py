"""The marker rollup (task E6.4; spec 9.3, 16.5; phase-6 fixed choices 3-4; D190, D191).

Pure: statuses and counts in, a marker's status and its per-status counts out.
No database, no I/O, so the table-driven suite can cover every combination
without fixtures.

**Ordering is not decided here.** Worst-of-descendants is
`device_status.rollup`, over `device_status.SEVERITY`. This module only adds
the two things a parent marker has that a device does not:

- **services** (D190): `degraded`, and only `degraded`, adds a `degraded` voice
  to a Deployment's rollup. `unconfigured` and `pending_verification` are a
  separate chip on the marker, not a status: a Deployment that has not set up
  its services yet is new, not broken.
- **counts**: how many devices sit in each status, `unknown` included, which
  the marker label and the Overview cards show.
"""

from collections import Counter
from dataclasses import dataclass, field

from app.controlplane.device_status import SEVERITY, DeviceStatus, rollup

#: The services_status value that degrades a Deployment marker (D190).
SERVICES_DEGRADED = "degraded"


@dataclass(frozen=True)
class MarkerRollup:
    status: DeviceStatus
    #: Every status in `SEVERITY` order, zeros included, so a client renders a
    #: stable distribution without knowing the vocabulary.
    counts: dict[str, int] = field(default_factory=dict)


def roll_up(statuses: list[DeviceStatus], *, services_status: str | None = None) -> MarkerRollup:
    """One parent marker's status and distribution.

    `statuses` are its descendant DEVICES' displayed statuses, alerts already
    applied (`device_status.with_alerts`). Counts describe devices only:
    services degradation colors the marker but is not a device.
    """
    voices = list(statuses)
    if services_status == SERVICES_DEGRADED:
        voices.append(DeviceStatus.DEGRADED)
    tally = Counter(statuses)
    return MarkerRollup(
        status=rollup(voices),
        counts={status.value: tally.get(status, 0) for status in SEVERITY},
    )
