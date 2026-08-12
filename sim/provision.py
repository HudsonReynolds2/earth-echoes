"""Provisioning a simulated fleet the way an operator would (task SIM.4).

A fleet needs two things before a single device can connect: **inventory rows**,
so the platform knows the hardware exists, and **broker credentials**, so the
hardware can say anything at all. This module creates both, and it creates them
through the platform's own front doors:

* inventory over the **REST API** — `POST /deployments`, `POST /pods` with E1.3's
  inline aggregator block, `POST /listeners/import` — with a session cookie and
  `X-CSRF-Token` on every mutation, exactly as any client must. No direct
  database writes anywhere: a harness that seeded its own rows would not notice
  the day the API stopped accepting them, which is the day the harness stops
  being evidence about the platform;
* credentials by running **`app.devbroker` as a subprocess**, the same command
  the README gives an operator. The harness never writes a broker credential
  itself and never invents a username — it reads `accounts.json`, which E3.1
  made deliberately the one place dev passwords live.

**Why a subprocess and not an import.** `test_harness_boundaries` holds every
module in `/sim` to importing `app.contracts.mqtt` and nothing else from the
platform, and that rule is worth more than the convenience: a harness that
imported `app.devbroker` for `load_manifest`/`device_username` would be one
import away from reaching into a session factory to make a scenario work. So
the generator is invoked, its manifest is read as the documented file interface
it is, and a device account is matched by its `aggregator_uuid` field rather
than by re-deriving the `dev-{uuid}` username recipe here (see D112).

**Idempotence is the interface.** Every step asks the API what already exists
and creates only the remainder, because the realistic operator sequence is
"run it again with more Aggregators" and because a fleet run must not depend on
a clean database. Re-running is therefore cheap and safe; re-running with a
LARGER fleet extends it.
"""

import argparse
import hashlib
import http.cookiejar
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import TypeAdapter, ValidationError

from device import BrokerLogin

# The published contract reaches this module by path, as it does `device.py`
# (see that module's note). Imported here for the MAC type: a generated MAC is
# validated against the same rule the wire enforces, so a fleet whose shape
# could not exist on the wire is refused here rather than by a broker later.
_BACKEND = str(Path(__file__).resolve().parent.parent / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.contracts.mqtt import MacAddress  # noqa: E402  (the path above has to be set first)

log = logging.getLogger(__name__)

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

#: The API's mount point and its two auth mechanics, spelled out here for the
#: same reason `frontend/src/lib/http.ts` spells them out: this is a CLIENT, and
#: a client knows the wire names, not the server's Python constants. They are
#: published in INTERFACES.md ("API conventions", "Auth and session mechanics").
API_PREFIX: Final = "/api/v1"
CSRF_COOKIE: Final = "eoe_csrf"
CSRF_HEADER: Final = "X-CSRF-Token"

#: D7's list envelope caps `limit` at 500, so reading 600 Listeners back is a
#: paged read. Asking for more would be a 422 about a query parameter, at hour
#: two of nothing in particular.
PAGE_LIMIT: Final = 500

#: E1.6 caps an import at 1000 rows and 1 MiB. Half of that, so a fleet larger
#: than the documented 20 × 30 keeps working without anybody rediscovering the
#: limit from a 422.
IMPORT_CHUNK: Final = 500

#: Defaults for the dev stack as `deploy/docker-compose.yml` publishes it
#: (PHASE0-2-02's 1xxxx host ports).
DEFAULT_API: Final = "http://localhost:18000"
DEFAULT_DEPLOYMENT: Final = "sim-fleet"
DEFAULT_CERTS: Final = REPO_ROOT / "deploy" / "dev-certs"

#: What the PLATFORM dials, which is not what a device on the host dials: inside
#: the compose network the broker is `mosquitto:8883`, and that is the value
#: `app.devbroker` writes onto the `deployment_service` row.
DEFAULT_PLATFORM_BROKER: Final = "mosquitto:8883"

#: `uv run` from the backend project, which is the command the README gives an
#: operator for this generator. Overridable because the test suite runs it on
#: the interpreter it is already inside — there is no uv in a pytest process
#: and no network to fetch one with.
DEVBROKER_COMMAND: Final = (
    "uv",
    "run",
    "--directory",
    str(REPO_ROOT / "backend"),
    "python",
    "-m",
    "app.devbroker",
)

#: E1.9's demo fixture owns this prefix, and a fleet is routinely provisioned
#: into a database that already holds the demo. A derived prefix that landed
#: here would not be a duplicate-identity SCENARIO — it would be an import that
#: conflicts halfway through and a half-created fleet.
DEMO_MAC_PREFIX: Final = "02:EE:0E"

#: What a fleet is told to do so that every Aggregator has something to apply.
#: `listener.wake_grace_seconds` is genuinely an Aggregator setting (spec 5.3,
#: `lowest_level="aggregator"`) and genuinely drives device behaviour — SIM.2's
#: wake sweep reads it off the applied config — so the revision this publishes
#: is a real instruction rather than a ping with a value in it.
FLEET_SETTING: Final = "listener.wake_grace_seconds"

_MAC = TypeAdapter(MacAddress)


class ProvisionError(RuntimeError):
    """The platform refused something, or answered something unusable.

    One exception for the whole module, carrying the API's own words: a
    provisioning failure is read by whoever is trying to start a fleet, and
    "409 conflict: pod name already used in this deployment" is the entire
    diagnosis. `code` is the D8 envelope code where there was one.
    """

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------
# The platform's REST API, dialled as a client
# ---------------------------------------------------------------------------


class Operator:
    """A logged-in operator session against the platform's HTTP API.

    Deliberately built on `urllib` from the standard library rather than on an
    HTTP client library. `/sim`'s runtime dependency set is a DEVICE's — an
    MQTT client and a payload parser — and the phase's fixed choice is that a
    dependency a real Aggregator would not carry does not go in it. A hundred
    lines of `urllib` is the price of that rule, and the rule is what keeps the
    harness installable as firmware-shaped rather than platform-shaped.

    Cookies are handled by a real cookie jar, so the session cookie and the
    JS-readable CSRF cookie behave exactly as they do in a browser: the jar
    stores them, `X-CSRF-Token` echoes the second one back on every mutation
    (D4's double submit), and nothing here has to know how they are signed.
    """

    def __init__(self, base_url: str = DEFAULT_API, *, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._email: str | None = None

    def __str__(self) -> str:
        return f"{self._email or 'anonymous'}@{self.base_url}"

    # --- auth ---------------------------------------------------------------

    def login(self, email: str, password: str) -> None:
        """Exchange credentials for a session. The password is never logged and
        never stored on this object: it goes into one request body and is
        forgotten, because the only thing worth keeping is the cookie."""
        self.post("/auth/login", {"email": email, "password": password}, authenticated=False)
        if self._csrf_token() is None:
            raise ProvisionError(
                f"login as {email} returned no {CSRF_COOKIE} cookie; "
                "every mutation after this would be a 403"
            )
        self._email = email
        log.info("logged in to %s as %s", self.base_url, email)

    def _csrf_token(self) -> str | None:
        for cookie in self._jar:
            if cookie.name == CSRF_COOKIE:
                return cookie.value
        return None

    # --- verbs --------------------------------------------------------------

    def get(self, path: str, **params: object) -> Any:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        body: object,
        *,
        authenticated: bool = True,
        content_type: str = "application/json",
        **params: object,
    ) -> Any:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self._request(
            "POST",
            path,
            body=payload,
            params=params,
            content_type=content_type,
            authenticated=authenticated,
        )

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def page(self, path: str, **params: object) -> Iterator[dict[str, Any]]:
        """Every item of a D7 list endpoint, one page at a time.

        Paged rather than "ask for everything": `limit` is capped at 500 and a
        20 × 30 fleet is 600 Listeners, so the naive read is a 422 that only
        appears at the scale nobody tests at.
        """
        offset = 0
        while True:
            page = self.get(path, limit=PAGE_LIMIT, offset=offset, **params)
            items: list[dict[str, Any]] = page["items"]
            yield from items
            offset += len(items)
            if not items or offset >= int(page["total"]):
                return

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        params: Mapping[str, object] | None = None,
        content_type: str = "application/json",
        authenticated: bool = True,
    ) -> Any:
        url = f"{self.base_url}{API_PREFIX}{path}"
        query = {key: str(value) for key, value in (params or {}).items() if value is not None}
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            request.add_header("Content-Type", content_type)
        if authenticated and method != "GET":
            token = self._csrf_token()
            if token is None:
                raise ProvisionError(f"{method} {path} needs a session; call login() first")
            request.add_header(CSRF_HEADER, token)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise self._refusal(method, path, error) from error
        except OSError as error:
            raise ProvisionError(
                f"{method} {path}: cannot reach the platform at {self.base_url}: {error}"
            ) from error
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as error:
            raise ProvisionError(
                f"{method} {path}: answered {raw[:200]!r}, which is not JSON"
            ) from error

    @staticmethod
    def _refusal(method: str, path: str, error: urllib.error.HTTPError) -> ProvisionError:
        """The platform's refusal, in the platform's own words.

        The D8 envelope carries a code and a message written for whoever has to
        fix it; discarding them in favour of "HTTP 409" would make every
        provisioning failure a research project.
        """
        code: str | None = None
        message = error.reason or "no reason given"
        try:
            payload = json.loads(error.read() or b"{}")
            envelope = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(envelope, dict):
                code = envelope.get("code")
                message = envelope.get("message") or message
                if envelope.get("detail") is not None:
                    message = f"{message} ({json.dumps(envelope['detail'])})"
        except (ValueError, OSError):  # a body that is not the envelope; the status still is news
            pass
        return ProvisionError(
            f"{method} {path}: {error.code} {code or 'error'}: {message}",
            code=code,
            status=error.code,
        )


# ---------------------------------------------------------------------------
# What a fleet looks like before it exists
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FleetPlan:
    """The names and identities of a fleet, computed rather than remembered.

    Every identifier is a pure function of the deployment slug and two indices,
    which is what makes provisioning idempotent and a fleet re-attachable: a
    second run computes the same names, finds them, and creates only what is
    missing. Nothing is random, for E1.9's reason — a fleet whose MACs changed
    per run could not be reasoned about across two commands.
    """

    aggregators: int = 20
    listeners_per_aggregator: int = 30
    deployment_slug: str = DEFAULT_DEPLOYMENT
    deployment_name: str | None = None
    #: The first four octets of every Listener MAC in this fleet. None derives
    #: them from the deployment slug (see `mac_head`), which is what stops two
    #: fleets in two deployments from claiming the same addresses — `listener.mac`
    #: is the PRIMARY KEY and is GLOBAL (E1.1/D31), so a shared prefix is not a
    #: near miss, it is an import that conflicts halfway through.
    mac_prefix: str | None = None

    def __post_init__(self) -> None:
        if self.aggregators < 1 or self.listeners_per_aggregator < 0:
            raise ProvisionError(
                f"a fleet of {self.aggregators} Aggregators × "
                f"{self.listeners_per_aggregator} Listeners is not a fleet"
            )
        # One MAC octet per index each way. Refused HERE rather than by a CHECK
        # constraint on row 256, so a scale nobody planned for fails before any
        # of it is half-created.
        if self.aggregators > 0xFF:
            raise ProvisionError(f"{self.aggregators} Aggregators exceeds this MAC layout's 255")
        if self.listeners_per_aggregator > 0xFF:
            raise ProvisionError(
                f"{self.listeners_per_aggregator} Listeners per Aggregator exceeds this "
                "MAC layout's 255"
            )
        uuid = self.aggregator_uuid(self.aggregators - 1)
        if len(uuid) > 64:  # `aggregator.aggregator_uuid` is String(64) (E1.1)
            raise ProvisionError(f"{uuid!r} is longer than the 64 characters inventory allows")
        # Validated through the contract's own MAC type, so a prefix that could
        # never appear on the wire fails at plan time.
        self.listener_mac(self.aggregators - 1, max(self.listeners_per_aggregator - 1, 0))

    @property
    def name(self) -> str:
        return self.deployment_name or f"SIM Fleet {self.deployment_slug}"

    @property
    def listeners(self) -> int:
        return self.aggregators * self.listeners_per_aggregator

    def __str__(self) -> str:
        return (
            f"{self.aggregators} × {self.listeners_per_aggregator} "
            f"({self.listeners} Listeners) in {self.deployment_slug}"
        )

    def aggregator_uuid(self, index: int) -> str:
        """The `aggregator_uuid` of one fleet member (E1.1: globally unique).

        Deployment-scoped so two fleets never collide, and carrying `sim` so a
        simulated Aggregator is recognisable as one in an operator's own
        inventory even when the fleet was provisioned into a deployment full of
        real hardware. The slug comes FIRST so the default reads `sim-fleet-sim-000`
        rather than stuttering.
        """
        return f"{self.deployment_slug}-sim-{index:03d}"

    def pod_name(self, index: int) -> str:
        return f"SIM Pod {index:03d}"

    def listener_name(self, index: int, listener_index: int) -> str:
        return f"sim-{index:03d}-{listener_index:03d}"

    @property
    def mac_head(self) -> str:
        """The four octets every Listener MAC in this fleet begins with.

        `02` is the locally administered bit — what an address nobody bought
        looks like — followed by three bytes derived from the deployment slug.
        DERIVED, because `listener.mac` is a global primary key: two fleets with
        the same shape in two deployments would otherwise claim the same
        addresses, and the second one's import would conflict on row 1 with a
        message about hardware neither of them has.

        A digest rather than a counter because a plan has to be computable from
        the slug alone, with nothing to look up and nothing to remember — that
        is what makes provisioning idempotent across two separate commands.
        """
        if self.mac_prefix is not None:
            head = self.mac_prefix.upper()
        else:
            digest = hashlib.sha256(self.deployment_slug.encode()).digest()
            head = f"02:{digest[0]:02X}:{digest[1]:02X}:{digest[2]:02X}"
        # Checked on BOTH paths: the demo prefix is reserved, and an operator
        # who typed it deliberately wanted twenty-eight duplicate-MAC conflicts
        # even less than the digest that landed there by accident did.
        if head.startswith(DEMO_MAC_PREFIX):
            raise ProvisionError(
                f"the MAC prefix for slug {self.deployment_slug!r} is {head}, which collides "
                f"with the E1.9 demo fixture's {DEMO_MAC_PREFIX}. Pass a different explicit "
                "mac_prefix (four octets) for this deployment."
            )
        return head

    def listener_mac(self, index: int, listener_index: int) -> str:
        mac = f"{self.mac_head}:{index:02X}:{listener_index:02X}".upper()
        try:
            return str(_MAC.validate_python(mac))
        except ValidationError as error:
            raise ProvisionError(
                f"{mac!r} is not a MAC address the wire contract accepts; "
                f"check mac_prefix={self.mac_prefix!r} (four octets, e.g. 02:51:4D:00)"
            ) from error

    def macs(self, index: int) -> tuple[str, ...]:
        return tuple(
            self.listener_mac(index, listener) for listener in range(self.listeners_per_aggregator)
        )

    def indices(self) -> range:
        return range(self.aggregators)


@dataclass(frozen=True)
class ProvisionedAggregator:
    """One Aggregator that now exists in inventory, with its own Listeners."""

    index: int
    aggregator_uuid: str
    aggregator_id: str
    pod_id: str
    macs: tuple[str, ...]


@dataclass(frozen=True)
class ProvisionedFleet:
    """A fleet the platform now knows about, ready to be given credentials."""

    deployment_id: str
    deployment_slug: str
    aggregators: tuple[ProvisionedAggregator, ...]

    @property
    def aggregator_ids(self) -> list[str]:
        return [aggregator.aggregator_id for aggregator in self.aggregators]

    @property
    def listeners(self) -> int:
        return sum(len(aggregator.macs) for aggregator in self.aggregators)


# ---------------------------------------------------------------------------
# Creating it, over the API
# ---------------------------------------------------------------------------


def provision_hierarchy(operator: Operator, plan: FleetPlan) -> ProvisionedFleet:
    """Create (or find) the whole hierarchy for `plan` over the REST API.

    Idempotent at every step, and idempotent by ASKING rather than by
    swallowing conflicts: a 409 that this function caused is a bug, so it is
    never caught. What is caught is nothing — every refusal the platform makes
    reaches the caller, because "the fleet is half provisioned and the API said
    no" is the one outcome a load run must not paper over.
    """
    organization_id = _organization(operator)
    deployment_id = _deployment(operator, organization_id, plan)
    aggregators = tuple(
        _aggregator(operator, deployment_id, plan, index) for index in plan.indices()
    )
    fleet = ProvisionedFleet(
        deployment_id=deployment_id,
        deployment_slug=plan.deployment_slug,
        aggregators=aggregators,
    )
    _import_listeners(operator, plan, fleet)
    log.info(
        "provisioned %s: deployment %s, %d Aggregators, %d Listeners",
        plan,
        deployment_id,
        len(fleet.aggregators),
        fleet.listeners,
    )
    return fleet


def _organization(operator: Operator) -> str:
    """The organization to hang the fleet off. v1 is single-organization (spec
    12.1) and `POST /organizations` is clamped to one, so the only correct
    behaviour is to use the one that exists and to create one only when the
    database is genuinely empty."""
    existing = operator.get("/organizations", limit=1)["items"]
    if existing:
        return str(existing[0]["id"])
    created = operator.post("/organizations", {"name": "SIM"})
    log.info("created organization %s", created["id"])
    return str(created["id"])


def _deployment(operator: Operator, organization_id: str, plan: FleetPlan) -> str:
    """The fleet's own deployment, found by SLUG.

    By slug because the slug is the `{dep}` MQTT topic segment (E1.1): the
    deployment a device publishes into is identified by exactly this string, so
    it is the only identifier worth matching on here.
    """
    found = operator.get("/deployments", slug=plan.deployment_slug, limit=1)["items"]
    if found:
        return str(found[0]["id"])
    created = operator.post(
        "/deployments",
        {"organization_id": organization_id, "name": plan.name, "slug": plan.deployment_slug},
    )
    log.info("created deployment %s (%s)", created["slug"], created["id"])
    return str(created["id"])


def _aggregator(
    operator: Operator, deployment_id: str, plan: FleetPlan, index: int
) -> ProvisionedAggregator:
    """One Pod and its single Aggregator, in one call (E1.3).

    `POST /pods` with the inline aggregator block, because one Aggregator per
    Pod is a database constraint and E1.3 exists precisely so a client does not
    have to make two calls and handle the half-done state between them.
    """
    aggregator_uuid = plan.aggregator_uuid(index)
    found = operator.get("/aggregators", aggregator_uuid=aggregator_uuid, limit=1)["items"]
    if found:
        return ProvisionedAggregator(
            index=index,
            aggregator_uuid=aggregator_uuid,
            aggregator_id=str(found[0]["id"]),
            pod_id=str(found[0]["pod_id"]),
            macs=plan.macs(index),
        )
    pod = operator.post(
        "/pods",
        {
            "deployment_id": deployment_id,
            "name": plan.pod_name(index),
            "aggregator": {"aggregator_uuid": aggregator_uuid},
        },
    )
    aggregator = pod["aggregator"]
    log.info("created pod %s with aggregator %s", pod["name"], aggregator_uuid)
    return ProvisionedAggregator(
        index=index,
        aggregator_uuid=aggregator_uuid,
        aggregator_id=str(aggregator["id"]),
        pod_id=str(pod["id"]),
        macs=plan.macs(index),
    )


def _import_listeners(operator: Operator, plan: FleetPlan, fleet: ProvisionedFleet) -> None:
    """Import every Listener that does not exist yet, through E1.6.

    All-or-nothing (`partial` left at its default false) and only the missing
    rows, which is the combination that makes this both idempotent and honest:
    an import of rows that already exist would report row-level conflicts a
    caller would have to learn to ignore, and ignoring row-level errors is how
    a fleet ends up 40 Listeners short with a green log.
    """
    existing = {
        str(row["mac"]).upper()
        for row in operator.page("/listeners", deployment_id=fleet.deployment_id)
    }
    rows = [
        {
            "mac": mac,
            "name": plan.listener_name(aggregator.index, listener_index),
            "aggregator_uuid": aggregator.aggregator_uuid,
            "gps_lat": None,
            "gps_lon": None,
            "tags": ["sim"],
        }
        for aggregator in fleet.aggregators
        for listener_index, mac in enumerate(aggregator.macs)
        if mac not in existing
    ]
    if not rows:
        log.info("all %d Listeners already exist", fleet.listeners)
        return
    for chunk in _chunks(rows, IMPORT_CHUNK):
        report = operator.post("/listeners/import", {"rows": chunk})
        if not report["committed"] or report["failed"]:
            failures = [row for row in report["rows"] if row["status"] == "error"][:5]
            raise ProvisionError(
                f"importing {len(chunk)} Listeners committed nothing "
                f"({report['failed']} row error(s)); first few: {json.dumps(failures)}. "
                "A row conflicting on a MAC means another fleet already owns this prefix: "
                f"{plan.mac_head} is derived from the deployment slug, so pass an explicit "
                "mac_prefix if two fleets genuinely have to share a deployment."
            )
        log.info("imported %d Listeners", report["created"])


def _chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def apply_fleet_config(
    operator: Operator, aggregator_ids: Sequence[str], value: int
) -> list[dict[str, Any]]:
    """Publish one Aggregator-level revision to the whole fleet (E2.6, E3.13).

    This is what gives every Aggregator something to apply, and it is a real
    setting: `listener.wake_grace_seconds` is what SIM.2's wake sweep reads off
    its applied config, so a fleet that reaches `applied` here has genuinely
    been reconfigured rather than pinged.

    Returns every revision the apply created. An Aggregator-level change moves
    its Listeners' effective config too (the key is inherited, not filtered
    out), so E2 cuts one revision per affected device and all of them go out —
    which is the cost the SIM ledger records and the reason a full-scale run's
    time-to-all-applied is dominated by publishes rather than by devices.
    """
    if not aggregator_ids:
        return []
    response = operator.post(
        "/config/apply",
        {
            "selection": {"entity_type": "aggregator", "where": {"ids": list(aggregator_ids)}},
            "changes": {FLEET_SETTING: value},
            "level": "target",
        },
    )
    revisions: list[dict[str, Any]] = response["revisions"]
    published = response.get("published")
    if published is None:
        # The field E3.13 added. Its absence is not a puzzle to debug at the
        # wire level: it means the API answering is older than the publish path,
        # which in practice means a stale container image. Said plainly, because
        # the alternative is a KeyError from inside a provisioning helper — which
        # is exactly how this was found.
        raise ProvisionError(
            "the apply response carries no 'published' count, so this API predates E3.13 and "
            "will not publish anything a device could apply. Rebuild the stack: "
            "`docker compose up -d --build`."
        )
    if published != len(revisions):
        raise ProvisionError(
            f"apply cut {len(revisions)} revision(s) but published {published}; "
            f"state is {response.get('state')!r} — a broker was down, and the devices will "
            "never hear about a revision that never left"
        )
    log.info("published %d revision(s) setting %s=%s", len(revisions), FLEET_SETTING, value)
    return revisions


# ---------------------------------------------------------------------------
# Broker credentials
# ---------------------------------------------------------------------------


def mint_credentials(
    out_dir: Path,
    *,
    database_url: str,
    kek: str,
    host: str | None = None,
    port: int | None = None,
    keep_tls: bool = True,
    command: Sequence[str] = DEVBROKER_COMMAND,
    cwd: Path | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Run `app.devbroker` and return the manifest it wrote.

    A SUBPROCESS, not an import: the platform's own generator, run the way the
    README tells an operator to run it, so the accounts, the ACL and the
    `deployment_service` rows are all made by the code that owns them. The
    harness's part is to read the result.

    `keep_tls` defaults to True, unlike the generator's own default, because
    re-running this against a LIVE dev stack is the normal case here — a fresh
    CA would invalidate the certificate the running Mosquitto already loaded,
    turning "the fleet grew by five Aggregators" into "nothing can connect".
    A directory with no `ca.crt` gets fresh material either way.

    `command` and `cwd` are parameters because the default is `uv run` from the
    backend project and the test suite cannot use it: there is no uv inside a
    pytest process and no network to fetch one with, so the suite passes its own
    interpreter and `backend/` as the working directory instead. The generator
    that runs is the platform's either way.
    """
    argv = [
        *command,
        "--out",
        str(out_dir),
        *(("--keep-tls",) if keep_tls else ()),
        *(("--host", host) if host else ()),
        *(("--port", str(port)) if port else ()),
    ]
    log.info("minting broker credentials: %s", " ".join(argv))
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        cwd=None if cwd is None else str(cwd),
        # DATABASE_URL and EOE_KEK are the generator's inputs; EOE_SESSION_SECRET
        # is required by `Settings` even though nothing here holds a session.
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "EOE_KEK": kek,
            "EOE_SESSION_SECRET": os.environ.get("EOE_SESSION_SECRET", "sim-provisioning"),
        },
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise ProvisionError(
            f"app.devbroker exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    log.info("%s", result.stdout.strip())
    return load_accounts(out_dir)


def load_accounts(out_dir: Path = DEFAULT_CERTS) -> dict[str, Any]:
    """Read `accounts.json` — E3.1's one place dev broker passwords live.

    Read as a file rather than through `app.devbroker.load_manifest` for the
    boundary reason in this module's docstring (D112). The shape is documented
    in INTERFACES.md under "The development broker".
    """
    path = out_dir / "accounts.json"
    try:
        manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ProvisionError(
            f"{path} does not exist: no dev broker has been provisioned here. "
            "Run `uv run python -m app.devbroker` from backend/, or "
            "`python provision.py` from sim/."
        ) from error
    except (OSError, ValueError) as error:
        raise ProvisionError(f"{path}: cannot be read as a manifest: {error}") from error
    return manifest


def device_login(
    manifest: Mapping[str, Any],
    aggregator_uuid: str,
    *,
    host: str,
    port: int,
    ca_cert: Path | None,
) -> BrokerLogin:
    """One Aggregator's own credential, matched on `aggregator_uuid`.

    Matched on the identity rather than on the username, so this module never
    reproduces the platform's `dev-{uuid}` recipe (D112): the manifest already
    records which account belongs to which Aggregator, and a rename of the
    recipe would then be invisible here rather than silently wrong.

    The CA comes from the caller's certificate DIRECTORY, not from the
    manifest's `ca_cert` path — that path is absolute on the machine that
    generated it, and the sim container reads the same files from a different
    mount point.
    """
    accounts: list[Mapping[str, Any]] = manifest.get("accounts", [])
    for account in accounts:
        if account.get("kind") == "device" and account.get("aggregator_uuid") == aggregator_uuid:
            return BrokerLogin(
                host=host,
                port=port,
                username=str(account["username"]),
                password=str(account["password"]),
                ca_cert=ca_cert,
            )
    raise ProvisionError(
        f"no broker account for {aggregator_uuid} in the manifest. Accounts are minted from "
        "INVENTORY, so an Aggregator provisioned after the last `app.devbroker` run has none: "
        "re-run provisioning and then reload the broker "
        "(`docker compose restart mosquitto`)."
    )


def broker_endpoint(value: str, *, default_port: int) -> tuple[str, int]:
    """Split a `HOST:PORT` argument, or apply the default port.

    Its own function because both CLIs take one and a silently mis-parsed port
    is a connection refused with no explanation attached.
    """
    host, separator, port = value.rpartition(":")
    if not separator:
        return value, default_port
    try:
        return host, int(port)
    except ValueError as error:
        raise ProvisionError(f"{value!r} is not HOST:PORT: {port!r} is not a port") from error


# ---------------------------------------------------------------------------
# CLI: the operator's one-time setup
# ---------------------------------------------------------------------------


def add_fleet_arguments(parser: argparse.ArgumentParser) -> None:
    """The arguments that describe a fleet, shared with `fleet.py`.

    Shared so the two commands cannot disagree about what `--aggregators 20`
    means: a runner pointed at a differently-shaped fleet than the one that was
    provisioned would fail at connect with a credential error and a very long
    detour.

    Every default is overridable by environment variable, because the compose
    service configures itself that way and a container that had to be given a
    command line would put the fleet's shape in two files.
    """
    parser.add_argument(
        "--aggregators",
        type=int,
        default=int(os.environ.get("EOE_SIM_AGGREGATORS", "20")),
        help="Aggregators in the fleet (default 20, spec 14.2 as PLAN-3-02 reads it)",
    )
    parser.add_argument(
        "--listeners-per-aggregator",
        type=int,
        default=int(os.environ.get("EOE_SIM_LISTENERS", "30")),
        help="Listeners under each Aggregator (default 30)",
    )
    parser.add_argument(
        "--deployment",
        default=os.environ.get("EOE_SIM_DEPLOYMENT", DEFAULT_DEPLOYMENT),
        help=f"deployment slug to provision into (default {DEFAULT_DEPLOYMENT})",
    )
    parser.add_argument(
        "--api",
        default=os.environ.get("EOE_SIM_API", DEFAULT_API),
        help=f"platform API base URL (default {DEFAULT_API})",
    )
    parser.add_argument(
        "--operator",
        default=os.environ.get("EOE_SIM_OPERATOR", ""),
        help="operator email; the password comes from EOE_SIM_PASSWORD and never from a flag",
    )
    parser.add_argument(
        "--certs",
        type=Path,
        default=Path(os.environ.get("EOE_SIM_CERTS", str(DEFAULT_CERTS))),
        help=f"dev broker material directory (default {DEFAULT_CERTS})",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("EOE_SIM_LOG_LEVEL", "INFO"),
        help="root log level (default INFO)",
    )


def plan_from(args: argparse.Namespace) -> FleetPlan:
    return FleetPlan(
        aggregators=args.aggregators,
        listeners_per_aggregator=args.listeners_per_aggregator,
        deployment_slug=args.deployment,
    )


def operator_from(args: argparse.Namespace) -> Operator:
    """A logged-in `Operator`, with the password taken from the environment.

    **Never from a flag** (rule R2): an argument is in the process table, in
    the shell history and in every `docker inspect` of the container that ran
    it. `EOE_SIM_PASSWORD` is documented in `deploy/.env.example` by name.
    """
    email = args.operator or os.environ.get("EOE_SIM_OPERATOR", "")
    password = os.environ.get("EOE_SIM_PASSWORD", "")
    if not email or not password:
        raise ProvisionError(
            "provisioning needs an operator: pass --operator EMAIL (or set EOE_SIM_OPERATOR) "
            "and put the password in EOE_SIM_PASSWORD. The account needs manage_devices and "
            "manage_config in the fleet's deployment."
        )
    operator = Operator(args.api)
    operator.login(email, password)
    return operator


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision a simulated fleet: inventory over REST, credentials via devbroker",
        epilog=(
            "Broker credentials are minted from INVENTORY, so this runs the API calls first. "
            "Mosquitto reloads passwd/acl only on restart: finish with "
            "`docker compose restart mosquitto`."
        ),
    )
    add_fleet_arguments(parser)
    parser.add_argument(
        "--platform-broker",
        default=os.environ.get("EOE_SIM_PLATFORM_BROKER", DEFAULT_PLATFORM_BROKER),
        help=(
            "HOST:PORT the PLATFORM dials, written onto deployment_service "
            f"(default {DEFAULT_PLATFORM_BROKER}; inside compose the broker is not localhost)"
        ),
    )
    parser.add_argument(
        "--no-credentials",
        action="store_true",
        help="create inventory only; do not run app.devbroker",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")

    try:
        plan = plan_from(args)
        fleet = provision_hierarchy(operator_from(args), plan)
        if args.no_credentials:
            print(f"inventory for {plan} is in place; credentials not minted (--no-credentials)")
            return 0
        database_url = os.environ.get("DATABASE_URL", "")
        kek = os.environ.get("EOE_KEK", "")
        if not database_url or not kek:
            raise ProvisionError(
                "minting credentials runs app.devbroker, which needs DATABASE_URL and EOE_KEK "
                "(the same values the API runs with). Set them, or pass --no-credentials."
            )
        host, port = broker_endpoint(args.platform_broker, default_port=8883)
        manifest = mint_credentials(
            args.certs, database_url=database_url, kek=kek, host=host, port=port
        )
    except ProvisionError as error:
        print(f"provisioning failed: {error}", file=sys.stderr)
        return 1
    devices = sum(1 for account in manifest["accounts"] if account["kind"] == "device")
    print(
        f"provisioned {plan}: {len(fleet.aggregators)} Aggregators, {fleet.listeners} Listeners, "
        f"{devices} device account(s) in {args.certs}"
    )
    print("now reload the broker so it reads the new accounts: docker compose restart mosquitto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
