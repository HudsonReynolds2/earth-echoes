# E3 Verification Walkthrough

A hands-on test platform for everything Epic E3 ships — the device control plane: the MQTT
broker, the desired/reported reconciliation loop, device status, and the timeline. Work top
to bottom, ticking each box. Every step says what to do and what you should see; if you see
anything else, that is a finding worth writing down.

This is a living acceptance document (rule R1): E3 ships it, and any later epic that
invalidates an assertion here amends it in the same batch. Siblings:
[the E1 walkthrough](e1-verification.md) covers hierarchy and inventory,
[the E2 walkthrough](e2-verification.md) covers the configuration model.

**Sections land task by task.** Anything not yet written below has not shipped yet.

## 0. Start the platform

- [ ] From the repo root: `.\qa-stack.ps1` (PowerShell). It now also generates the dev
      broker's TLS material before starting the stack, and provisions broker accounts after
      seeding — watch for the `== dev broker TLS material ==` and
      `== broker accounts and ACLs ==` banners.
- [ ] The script ends with `QA STACK READY`.

POSIX, by hand:

```bash
cd backend && uv run python -m app.devbroker --certs-only
```

then `docker compose -f deploy/docker-compose.yml up -d --build`, then
`uv run python -m app.seed --demo`, then:

```bash
cd backend && uv run python -m app.devbroker --host mosquitto --keep-tls
```

and finally `docker compose -f deploy/docker-compose.yml restart mosquitto`.

> **Before you ever run the test gate** (`.\gate.ps1` / `make gate`): run
> `.\qa-stack.ps1 down` first. The gate's container tests bind the same host ports and will
> go red against a running QA stack (D44).

## 1. The development broker (E3.1)

The control plane's security model is worth seeing directly rather than trusting: spec 7.1
puts every Aggregator behind its own credential, restricted by broker ACL to its own topic
subtree, over TLS.

- [ ] `docker compose -f deploy/docker-compose.yml -p eoe-qa ps` lists a **mosquitto**
      service, running, publishing `18883:8883`.
- [ ] `ls deploy/dev-certs` shows `ca.crt`, `server.crt`, `server.key`, `passwd`, `acl`, and
      `accounts.json`. **`git status` must not list any of them** — the whole directory is
      gitignored, and a private key showing up as untracked is a finding.
- [ ] Open `deploy/dev-certs/acl`. Each aggregator gets exactly seven grants, and they read
      as the spec 7.2 table's Direction column: `read` on `desired`, `cmd` and
      `lst/+/desired`; `write` on `reported`, `status`, `event` and `lst/+/reported`.
- [ ] Open `deploy/dev-certs/passwd`. Every line is `username:$7$101$...` — a PBKDF2-SHA512
      hash. No plaintext password appears anywhere in the file.

Now prove the isolation with the broker's own client tools. Take the two passwords from
`deploy/dev-certs/accounts.json` (`platform-redwood-coast` and `dev-demo-agg-rc-01`), then
run each command inside the broker container:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u platform-redwood-coast -P PLATFORM_PASSWORD -t eoe/redwood-coast/agg/demo-agg-rc-02/desired -m hello -r
```

- [ ] **The platform reaches its whole namespace.** Subscribing as
      `platform-redwood-coast` to `eoe/redwood-coast/#` (add `-W 3 -v` to the
      `mosquitto_sub` form of the command above) prints the retained `hello` you just
      published, on `demo-agg-rc-02`'s topic.
- [ ] **A device cannot read its neighbour.** The same subscription as
      `dev-demo-agg-rc-01` prints **nothing** and exits with `Timed out`. That is what
      denial looks like: Mosquitto accepts the subscription and then never delivers.
- [ ] **A device can read its own.** Publish a retained message to
      `eoe/redwood-coast/agg/demo-agg-rc-01/desired` as the platform account, then
      subscribe to it as `dev-demo-agg-rc-01` — it arrives immediately.
- [ ] **The listener is TLS-only.** Drop `--cafile` from any of the commands above and it
      fails with a protocol error rather than connecting in plaintext.
- [ ] **A wrong password is refused.** Any command with a mangled `-P` value returns
      `Connection Refused: not authorised`.

Finally, the platform's own record of the broker:

- [ ] In the database, `select deployment_id, service_key, host, port, tls_enabled,
      username, password_secret_name from deployment_service;` returns one `mqtt` row per
      deployment, with `host` = `mosquitto`, `port` = **8883** and `tls_enabled` = true.
      8883, not 18883, is correct: these are the coordinates the API container dials inside
      the compose network, where every service still uses its standard port. 18883 is only
      the host-side publication you connect to from your own machine.
- [ ] **No password appears in that table.** The row names a `deployment:<id>:mqtt_password`
      entry; the value itself lives encrypted in the `secret` table (rule R2). Confirm with
      `select name from secret;` — names only, and `select * from deployment_service;`
      contains no credential material beyond the username.

## 2. The client manager survives a broker restart (E3.2)

The platform's side of the connection. The `worker` container now runs one of these for
real (§7, E3.7), but this section drives its own by hand, which is the clearest way to see
the property it exists for: **a broker outage is not something message-handling code
notices.**

Save this as `check-manager.py` in `backend/` (delete it afterwards; it is a scratch probe,
not a shipped tool):

```python
import asyncio
import logging
from dataclasses import replace

from app.contracts.mqtt import deployment_subscriptions
from app.controlplane.broker import MqttClientManager, load_broker_coordinates
from app.db import create_session_factory
from app.secrets import SecretStore
from app.settings import Settings

# The manager reports connects, losses and retries at INFO. Nothing configures
# logging outside the API process, so a bare script has to ask for it.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

settings = Settings()
_, factory = create_session_factory(settings.database_url)
coordinates = [
    # The rows say mosquitto:8883 — the coordinates the API CONTAINER dials.
    # From your own machine the same broker is localhost:18883 (see §1).
    replace(c, host="localhost", port=18883)
    for c in load_broker_coordinates(factory, SecretStore(factory, settings.kek))
]
print("brokers:", [str(c) for c in coordinates])


async def show(message):
    print(f"  <- {message.deployment_slug}  {message.topic}  {message.payload!r}")


async def main():
    manager = MqttClientManager(lambda: coordinates)
    manager.subscribe(deployment_subscriptions, show)
    async with manager:
        await asyncio.sleep(180)


asyncio.run(main())
```

Run it with the same environment as the seed step (`DATABASE_URL` pointing at
`localhost:15432`, plus `EOE_SESSION_SECRET` and `EOE_KEK` from `deploy/.env`):

```bash
cd backend && uv run python check-manager.py
```

- [ ] It prints one `brokers:` line naming **both** deployments, as
      `redwood-coast broker at localhost:18883` — **and no password anywhere**, which is the
      point of printing coordinates at all (rule R2).
- [ ] Within a second, two `connected to the ... broker` log lines appear. That is TLS
      verified against the CA stored on the `deployment_service` row — no trust-store
      shortcut, no `tls_insecure`.

Now, in a second terminal, publish as a device (the `dev-demo-agg-rc-01` password comes from
`deploy/dev-certs/accounts.json`, as in §1):

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -t eoe/redwood-coast/agg/demo-agg-rc-01/reported -m before -q 1
```

- [ ] The first terminal prints `<- redwood-coast  eoe/redwood-coast/agg/demo-agg-rc-01/reported  b'before'`.
- [ ] Publish to that aggregator's **`desired`** topic as `platform-redwood-coast` instead.
      **Nothing is printed.** The platform subscribes to device-to-platform topics only; if it
      swept `eoe/{dep}/#` it would read its own publishes back as if they were device reports.

Now break it:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa restart mosquitto
```

- [ ] The first terminal logs `lost the redwood-coast broker at ...`, then
      `reconnecting to the ... in 1.0s (attempt 1)`, then `connected to the ...` again. The
      delays grow (1s, 2s, 4s …) if the broker takes a while, and are jittered — two
      deployments do not retry in lockstep.
- [ ] Repeat the device publish above with `-m after`. It prints, on **the same handler**,
      which was registered once before any of this and was never told the connection dropped.
      That is the whole acceptance criterion: reconnect *and* resubscribe, invisibly.
- [ ] Stop the probe with Ctrl-C and delete `backend/check-manager.py`.

## 3. The wire contract (E3.3)

`backend/app/contracts/mqtt.py` is the whole surface between the platform and a device: the
spec 7.2 topics and the spec 7.3 payloads. It is also the module the simulation harness (SIM)
imports and firmware is written against, so it is worth reading once and poking at directly.
Nothing here needs the stack running — this is a library.

- [ ] Open the file and read the module docstring. It says, in its first line, that the
      module is a **published interface** and that change is additive only. That sentence is
      the reason the rest of E3 never builds a topic string by hand.

```bash
cd backend && uv run python
```

```python
>>> import datetime as dt, uuid
>>> from app.contracts import mqtt
>>> mqtt.desired_topic("redwood-coast", "demo-agg-rc-01")
'eoe/redwood-coast/agg/demo-agg-rc-01/desired'
```

- [ ] **Identifiers are checked, not trusted.** `mqtt.deployment_root("redwood+coast")` raises
      `TopicError`. A `+` or `#` reaching a topic string unchecked is how one device would end
      up subscribed to another's subtree.

```python
>>> cfg = mqtt.DesiredConfig(
...     revision_id=uuid.uuid4(),
...     generated_at=dt.datetime.now(dt.UTC),
...     target=mqtt.DesiredTarget(type="aggregator", id="demo-agg-rc-01"),
...     config={"logging.verbosity": "info", "analysis.confidence_threshold": 0.6},
...     checksum="sha256:" + "ab12" * 16,
... )
>>> mqtt.encode(cfg)
```

- [ ] The bytes carry a top-level `"schema_version":1` and timestamps ending in **`Z`**, not
      `+00:00` — the form every spec 7.3 example prints.
- [ ] `mqtt.decode(mqtt.DesiredConfig, mqtt.encode(cfg)) == cfg`. Round-tripping is the whole
      point: the platform builds these and a device reads them, and SIM does both.

Now the rules that are easy to get wrong and expensive to get wrong:

- [ ] **A sleeping Listener must say when it will be back.**
      `mqtt.ListenerLiveness(state="sleeping")` raises, naming `expected_wake_at`. Spec 6.5
      has the platform storing the Listener's own declared wake time and never recomputing a
      schedule, so a sleeping report without one leaves nothing to tell healthy sleep from
      silence. `mqtt.ListenerLiveness(state="streaming", expected_wake_at=...)` raises too —
      a leftover wake time is a stale promise.
- [ ] **A naive timestamp is refused.**
      `mqtt.StatusMessage(state="online", at=dt.datetime.now())` (no `tzinfo`) raises. Spec
      7.4 drops stale reports by comparing timestamps; an instant with no zone cannot be
      compared, and guessing UTC would make that silently wrong.
- [ ] **A device may run ahead of the platform.** Decoding a `StatusMessage` whose JSON
      carries an extra `"fw": "2.1"` succeeds and ignores the field. Try the same trick on
      `DesiredConfig` — it raises, because in that direction an unexpected key is a bug on
      the platform's side about to reach every device.
- [ ] **A decode failure does not repeat the payload back.** Decode a
      `ReportedAggregatorState` whose `config` holds a `secret:...` marker and whose
      `checksum` is missing. The error names `checksum` and does **not** contain the marker —
      Pydantic's own message would have, which is why `decode` rebuilds it.
- [ ] **Two commands are never the same command.**
      `mqtt.Command(at=dt.datetime.now(dt.UTC), command="restart").command_id` differs on
      every construction, so a device can deduplicate its own retries (spec 7.4) without
      swallowing an operator's deliberate second attempt.

## 4. The revision state machine (E3.6)

Every config revision in the platform moves through spec 6.2's six states, and
`backend/app/controlplane/revision_state.py` is the only code that decides whether a given
move is allowed. It is **test-critical** (spec 14.5): its suite is the documentation of the
lifecycle, and no later session may weaken it. Like §3, this needs no stack — it is a library.

```bash
cd backend && uv run python
```

```python
>>> from app.controlplane.revision_state import *
>>> sorted(RevisionState)
[<RevisionState.APPLIED: 'applied'>, ..., <RevisionState.SUPERSEDED: 'superseded'>]
>>> legal_targets(RevisionState.PENDING)
frozenset({<RevisionState.APPLIED: 'applied'>, <RevisionState.FAILED: 'failed'>, <RevisionState.SUPERSEDED: 'superseded'>})
```

- [ ] Open the module and read the docstring, then open spec 6.2 beside it. The `TRANSITIONS`
      table and the spec's table are the same rows, and each carries the spec's own Trigger
      text in `spec_trigger`. `sorted(row.spec_trigger for row in TRANSITIONS)` reads like the
      document's Trigger column.
- [ ] **`superseded` is the only dead end.** `legal_targets(RevisionState.SUPERSEDED)` is
      empty, and every other state has somewhere to go. That is what makes the spec 6.2
      diagram's phrase "any non-terminal" well defined.

Now the rules that are easy to get wrong:

- [ ] **A transition is a triple, not a pair.**
      `is_legal(RevisionState.PENDING, RevisionState.FAILED, Trigger.TIMEOUT)` is `True` and
      `is_legal(RevisionState.PENDING, RevisionState.FAILED, Trigger.RETRY)` is `False` — same
      pair, and the second is "operator retries" read backwards. Checking the pair alone would
      accept it.
- [ ] **The guard's message tells you which of the three mistakes you made.** Run each of
      these and read what comes back:

```python
>>> check(RevisionState.PENDING, RevisionState.PENDING, Trigger.PUBLISH)   # "to itself"
>>> check(RevisionState.PENDING, RevisionState.FAILED, Trigger.RETRY)      # names report_error, timeout
>>> check(RevisionState.DRAFT, RevisionState.APPLIED, Trigger.REPORT_MATCH)  # lists where draft CAN go
>>> check(RevisionState.SUPERSEDED, RevisionState.PENDING, Trigger.RETRY)  # "terminal"
```

- [ ] Each raises `IllegalTransition` with a different, specific message. A no-op means the
      caller is missing an idempotency check; a wrong trigger means the right intent under the
      wrong cause; an impossible pair is a genuine lifecycle error. One generic "invalid
      transition" would have hidden the difference.
- [ ] **A `timeout` means silence and nothing else** (D70).
      `{(r.source, r.target) for r in TRANSITIONS if r.trigger is Trigger.TIMEOUT}` holds
      exactly one pair. A device that acknowledges a revision and reports the *wrong* config
      fails immediately as `report_error` (E3.5) rather than waiting out the 300-second window
      and then being reported as a timeout it never was.
- [ ] **An unrecognized stored state refuses to move.** `parse_state("apllied")` raises
      `UnknownRevisionState` naming the six legal values. Being lenient here — treating an
      unknown state as `draft` — would republish live config to a device.

Finally, the deviation this task recorded. Spec 6.2's table lists only `pending` and `applied`
as sources for `superseded`; the diagram directly below it says *any* non-terminal state.

- [ ] `is_legal(RevisionState.FAILED, RevisionState.SUPERSEDED, Trigger.NEWER_REVISION)` is
      `True` (D69, the diagram). Picture the alternative: an operator's config fails, they fix
      it and publish a new revision, and the old row sits at `failed` forever next to an
      `applied` one with nothing saying which is live.
- [ ] Open `backend/tests/test_revision_state.py`. `SPEC_6_2_TABLE` and
      `SPEC_6_2_DIAGRAM_EXTRA` are separate constants on purpose, so each spec statement stays
      attributable rather than being merged into one undifferentiated list.

## 5. Config reaches a device (E3.4)

This is where the previous four sections meet: a config revision built by E2 becomes a
retained MQTT message on the topic its device reads. `publish_revision` is the only way that
happens. An operator reaches it through `POST /revisions/{id}/publish` (§7, E3.7); E2's bulk
apply still stops at `draft` until E3.13 wires the call-through. This section calls the
function directly, which is the clearest way to see the property that matters: **the device
does not have to be listening.**

You need the QA stack from §0 running.

### Make a draft revision

- [ ] In the UI, go to **Configuration → Redwood Coast → Pod 01 · Alder Creek →** the
      aggregator `demo-agg-rc-01`, change one setting (`capture.sample_rate_hz` is a safe
      one), and apply.
- [ ] The apply response says `state: "draft"` and `publish_enabled: false`. That is E2's
      contract and it has not changed: applying config writes a revision, it does not talk to
      a device.
- [ ] Open the **Revisions** tab. The new revision is there, `draft`, with its checksum.

### Publish it

Save this as `check-publish.py` in `backend/` (a scratch probe, like §2 — delete it
afterwards):

```python
import asyncio
from dataclasses import replace

from sqlalchemy import select

from app.controlplane.broker import MqttClientManager, load_broker_coordinates
from app.controlplane.publisher import publish_revision
from app.db import create_session_factory
from app.models import Aggregator, ConfigRevision
from app.secrets import SecretStore
from app.settings import Settings

AGG = "demo-agg-rc-01"

settings = Settings()
_, factory = create_session_factory(settings.database_url)
coordinates = [
    # As in §2: the rows say mosquitto:8883, which is what the API container
    # dials. From your machine the same broker is localhost:18883.
    replace(c, host="localhost", port=18883)
    for c in load_broker_coordinates(factory, SecretStore(factory, settings.kek))
]

with factory() as db:
    # An aggregator revision's target_id is the PLATFORM UUID (aggregator.id),
    # not the aggregator_uuid that appears in the topic. Spec 4.2 keeps the
    # two apart and so does this lookup (D75).
    platform_uuid = db.scalars(
        select(Aggregator.id).where(Aggregator.aggregator_uuid == AGG)
    ).one()
    revision = db.scalars(
        select(ConfigRevision)
        .where(ConfigRevision.target_id == str(platform_uuid))
        .order_by(ConfigRevision.created_at.desc(), ConfigRevision.id.desc())
        .limit(1)
    ).one()
    revision_id, was = revision.id, revision.state
print(f"newest revision for {AGG} ({platform_uuid}): {revision_id} ({was})")


async def main():
    manager = MqttClientManager(lambda: coordinates)
    async with manager:
        # Wait for THIS deployment, by name. `coordinates` is ordered by slug,
        # so coordinates[0] is high-desert and waiting on it would let the
        # publish race ahead of the redwood-coast connection and raise
        # BrokerUnavailable.
        await manager.wait_connected(
            next(c.deployment_id for c in coordinates if c.slug == "redwood-coast")
        )
        outcome = await publish_revision(
            factory, manager, revision_id, publish_enabled=True
        )
    print(f"  topic:        {outcome.topic}")
    print(f"  state:        {was} -> {outcome.state}")
    print(f"  trigger:      {outcome.trigger}")
    print(f"  transitioned: {outcome.transitioned}")
    print(f"  superseded:   {[str(i) for i in outcome.superseded]}")


asyncio.run(main())
```

Run it with the same environment as §2 (`DATABASE_URL` on `localhost:15432`, plus
`EOE_SESSION_SECRET` and `EOE_KEK` from `deploy/.env`):

```bash
cd backend && uv run python check-publish.py
```

- [ ] It prints `topic: eoe/redwood-coast/agg/demo-agg-rc-01/desired` — the spec 7.2 desired
      topic, built from the deployment **slug** and the aggregator UUID. Never the deployment
      name, never its UUID.
- [ ] `state: draft -> pending`, `trigger: publish`, `transitioned: True`.
- [ ] It also prints the aggregator's **platform UUID** beside its `aggregator_uuid`. Those
      are two different identifiers for one device (spec 4.2): the revision's `target_id` is
      the platform UUID, the topic segment is the `aggregator_uuid`, and `publish_revision`
      resolves one to the other (D75). Using the wrong one builds a topic no device subscribes
      to and no ACL grants.
- [ ] Refresh the Revisions tab. The revision now reads **pending**, and every older open
      revision for that same aggregator reads **superseded** — whatever state it was in, which
      is the spec 6.2 diagram's edge (D69). The `superseded:` line in the script's output lists
      exactly which ones it closed.

### Nothing was listening, and that is the point

Only now, after the publish, connect a subscriber — as the **device's own** credential
(password from `deploy/dev-certs/accounts.json`, as in §1), so the spec 7.1 ACL is being
tested too:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_sub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -t eoe/redwood-coast/agg/demo-agg-rc-01/desired -C 1
```

- [ ] **A JSON payload appears immediately**, even though the subscriber connected after the
      publish. That is the spec 6.4 reconnect property: the message is retained, so an
      Aggregator that was powered off, out of signal, or being reflashed gets its desired
      config the moment it reconnects, with no polling and nothing to ask the platform for.
- [ ] The payload carries `schema_version`, `revision_id`, `generated_at`, `target`, `config`
      and `checksum`. Compare `revision_id` to what the script printed and `checksum` to the
      Revisions tab — they match.
- [ ] **`config` is the revision's snapshot exactly.** That is why a device's echoed checksum
      can match by construction (D52): the platform publishes the bytes it hashed, and
      anything that rewrote them on the way out would show up later as fleet-wide phantom
      drift rather than as an error here.
- [ ] Look at the secret-typed keys. On the demo fixture none are set, so they read `null`
      (`upload.s3_access_key` and the other service-onboarding secrets stay that way — E5
      owns them, and config overrides refuse to write them at all). Set `network.wifi_password`
      in the editor — it is secret and operator-writable — apply, and publish again.
- [ ] On the wire it comes back as a **marker object**, not your value:

```json
"network.wifi_password": {"$secret": "config:pod:cd00b28d-...:network.wifi_password"}
```

- [ ] Search the whole payload for the passphrase you typed. **It is not there.** Secrets do
      not transit desired config (spec 5.4, 8); the marker names where the value lives and the
      value itself reaches devices by the separate rewrap path.

### The rules that keep it safe

- [ ] **Republishing is idempotent.** Run `check-publish.py` again. It prints
      `transitioned: False` and `trigger: None`, the revision stays `pending`, and the audit
      log gains no second row — but the message goes out again. That re-send is deliberate: it
      is the repair for a broker that lost its retained store, and it is safe because the bytes
      are identical (D72).
- [ ] **Only the newest revision publishes.** Make a second config change in the UI so a newer
      `draft` exists, then edit the script to publish the OLD revision id. It raises
      `StaleRevision` naming the newer one, and **nothing is published**. Without that refusal
      the supersede sweep would quietly close the newer draft you were still working on — the
      two rules are a pair (D73, and D69's note in §4).
- [ ] **The flag is enforced in one place.** Change `publish_enabled=True` to `False` in the
      script. It raises `PublishDisabled` and nothing reaches the broker. The check lives
      inside `publish_revision`, not at its call sites, so no future caller can reach a device
      by forgetting it (D71).
- [ ] **A broker outage leaves nothing half-done.** Stop the broker
      (`docker compose -f deploy/docker-compose.yml -p eoe-qa stop mosquitto`) and make a
      fresh config change in the UI so there is a new `draft` to publish. **Delete the
      `await manager.wait_connected(...)` line from the script first** — otherwise it blocks
      for 30 seconds and raises `TimeoutError` from the manager before `publish_revision` is
      ever reached, which tests E3.2 rather than E3.4.
- [ ] Run it. It fails with **`BrokerUnavailable`** naming the deployment and the topic it
      did not publish, and the revision is **still `draft`** in the Revisions tab, with no new
      `revision.publish` audit row. The publish happens inside the database transaction, so a
      failure rolls back the state change with it (D74) — the alternative would be a `pending`
      revision no device was ever told about, which 300 seconds later would be reported as a
      device timeout that never happened.
- [ ] Start the broker again
      (`docker compose -f deploy/docker-compose.yml -p eoe-qa start mosquitto`) and delete
      `backend/check-publish.py`.

- [ ] Finally, check the audit trail: **Audit** in the UI, filtered to Redwood Coast. Each
      state-moving publish left one `revision.publish` row naming the actor, the topic, the
      checksum, the state it moved from and to, and the revisions it superseded. The repeat
      publishes left none — nothing changed, so there was nothing to record.

## 6. The device answers back (E3.5)

§5 pushed config out and stopped there. This section is the return path: what the platform
does with what a device says. The `worker` container runs this consumer for real now (§7,
E3.7); this section wires its own copy by hand, and the wiring is one line, which is itself
worth seeing.

You need the QA stack from §0, and a **pending** revision for `demo-agg-rc-01` — publish one
through §5 if you have not already. Save this as `check-consumer.py` in `backend/` (a scratch
probe, like §2 and §5 — delete it afterwards):

```python
import asyncio
import logging
from dataclasses import replace

from sqlalchemy import select

from app.controlplane.broker import MqttClientManager, load_broker_coordinates
from app.controlplane.consumer import ReportedConsumer, latest_state
from app.db import create_session_factory
from app.models import Aggregator, ConfigRevision, DeviceEvent
from app.secrets import SecretStore
from app.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

AGG = "demo-agg-rc-01"

settings = Settings()
_, factory = create_session_factory(settings.database_url)
coordinates = [
    replace(c, host="localhost", port=18883)
    for c in load_broker_coordinates(factory, SecretStore(factory, settings.kek))
]

with factory() as db:
    platform_uuid = str(
        db.scalars(select(Aggregator.id).where(Aggregator.aggregator_uuid == AGG)).one()
    )


def newest(db):
    """Re-read every time rather than pinning one revision at startup: you
    publish a second revision partway through this section, and a pinned id
    would keep reporting the OLD one (which by then is `superseded`)."""
    return db.scalars(
        select(ConfigRevision)
        .where(ConfigRevision.target_id == platform_uuid)
        .order_by(ConfigRevision.created_at.desc(), ConfigRevision.id.desc())
        .limit(1)
    ).one()


with factory() as db:
    revision = newest(db)
    print(f"newest revision: {revision.id} ({revision.state})")
    print(f"  checksum:  {revision.checksum}")
    print(f"  snapshot:  {revision.snapshot}")

consumer = ReportedConsumer(factory)


async def show(message):
    outcome = await consumer.handle(message)
    print(f"  <- {message.topic}\n     outcome: {outcome}")
    with factory() as db:
        current = newest(db)
        stored = latest_state(db, "aggregator", platform_uuid)
        events = db.scalars(select(DeviceEvent).order_by(DeviceEvent.at)).all()
        print(f"     revision:  {current.id} is {current.state}")
        print(f"     reported:  {stored.checksum if stored else '(nothing stored)'}")
        print(f"     events:    {[(e.code, e.at.isoformat()) for e in events]}")


async def main():
    manager = MqttClientManager(lambda: coordinates)
    # The whole wiring. The consumer names its own topic filters, so a topic
    # added to spec 7.2 cannot reach the publisher and silently miss here.
    manager.subscribe(consumer.filters, show)
    async with manager:
        await asyncio.sleep(600)


asyncio.run(main())
```

Run it with the same environment as §2 and §5:

```bash
cd backend && uv run python check-consumer.py
```

- [ ] It prints the newest revision, its state (`pending` if you published in §5), its
      checksum and its snapshot. **Copy the checksum and the snapshot** — you are about to
      play the device, and the device's job is to echo them.

### The happy path: pending becomes applied

In a second terminal, publish a reported state as the device's **own** credential (password
from `deploy/dev-certs/accounts.json`, as in §1). Paste your snapshot into `config` and your
checksum into `checksum`, and set `applied_revision_id` to the revision id printed above:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -q 1 -t eoe/redwood-coast/agg/demo-agg-rc-01/reported -m '{"schema_version":1,"reported_at":"2026-08-10T12:00:00Z","applied_revision_id":"YOUR-REVISION-ID","config":YOUR-SNAPSHOT,"checksum":"YOUR-CHECKSUM","health":{"uptime_s":86400,"coarse":"ok"}}'
```

- [ ] The first terminal prints `outcome: applied` and `revision: ... is applied`. That is
      spec 6.2's `pending → applied`, "device reports matching state" — the loop §5 opened,
      closed.
- [ ] `reported:` now shows the same checksum. That is spec 6.1's other half: the device
      carries a desired configuration and a reported one, and the platform's job is to
      converge them.
- [ ] In the UI, **Audit** filtered to Redwood Coast has one new `revision.report` row with
      **no actor**. A device said so, not a user — the same "system-originated" convention the
      timeout sweep and the publish path use.
- [ ] Note what the platform did NOT need: the payload never says which device sent it. The
      identity came from the topic, which the broker's ACL cut to this device's own subtree
      (§1). A payload field would be a self-declaration any device could write anything into.

### Replay it, and reorder it (spec 7.4)

- [ ] **Publish the exact same message again.** `outcome: unchanged`, the revision stays
      `applied`, and the audit log gains **no second row**. QoS 1 is at-least-once, so a
      redelivery is normal traffic, not a fault. Note it was not short-circuited on the
      timestamp: it ran the whole comparison, found the same revision with the same checksum,
      and had nothing left to say.
- [ ] **Now send a LATE one.** Keep the message exactly as it is — same `config`, same
      `checksum` — and change only `reported_at` to `"2026-08-10T11:59:00Z"`, a minute
      EARLIER than the one that just landed.
- [ ] `outcome: stale`. Nothing was stored and nothing moved. This is what spec 7.4 means by
      tolerating out-of-order delivery: the late message describes a world that has already
      moved on, and acting on it would drive a reconciled device to `drifted` on the strength
      of news that was true a minute ago.

### The two ways a report can be wrong, and why they are different

- [ ] **The device contradicts itself.** Send a report with the original `checksum` but one
      value changed inside `config`. `outcome: malformed`, and the revision **does not move**.
      The platform recomputes the checksum from the config rather than trusting the field —
      that is the only thing making "device-echoed checksums match by construction" a property
      rather than a hope, and a firmware that cannot reproduce the recipe is caught here,
      precisely, instead of looking like a config disagreement.
- [ ] **The device coherently applied the wrong thing.** Publish a fresh revision first (make
      another config change and re-run `check-publish.py`) so there is something `pending`
      again — the probe's `revision:` line re-reads the newest one, so it will follow you.
      Then change a value inside `config` AND give it a checksum that genuinely matches that
      config: `uv run python -c "import json,sys; from app.config.canonical import
      config_checksum; print(config_checksum(json.load(sys.stdin)))" < your-config.json`.
- [ ] `outcome: rejected` and the revision goes straight to **`failed`** — it does not wait
      out the 300-second window. The device answered, and answered wrong: that is a definite
      negative, and reporting it as a *timeout* five minutes later would be an inaccurate
      error message for something the platform already knew for certain (D70).
- [ ] Look at the audit row's detail: `differing_keys` names the settings that disagree and
      **never their values**. Snapshots carry secret markers and device-supplied values are of
      unknown provenance, so the operator gets the key names and reads the values from the
      revision and the reported state.

### Identity conflicts quarantine, and inventory does not move (spec 4.3)

This is the acceptance criterion worth doing by hand, because the claim is about a row that
*doesn't* change.

- [ ] In the UI, note `alder-creek-01`'s parent pod and name (its MAC is `02:EE:0E:01:01:01`).
- [ ] Publish a reported Listener state for that MAC on a **different** Aggregator's subtree —
      `demo-agg-rc-02` — using **that** aggregator's own credential:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u dev-demo-agg-rc-02 -P OTHER_DEVICE_PASSWORD -q 1 -t 'eoe/redwood-coast/agg/demo-agg-rc-02/lst/02:EE:0E:01:01:01/reported' -m '{"schema_version":1,"reported_at":"2026-08-10T12:05:00Z","config":{},"checksum":"sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a","liveness":{"state":"streaming"}}'
```

- [ ] `outcome: quarantined`.
- [ ] **Go back to the Listener in the UI. It is exactly as it was** — same parent, same name,
      same everything. Spec 4.3 item 2 says the platform quarantines the conflicting report
      "rather than overwriting inventory", and two devices claiming one MAC is precisely the
      case where believing the newest report would silently corrupt the fleet.
- [ ] Check the database: `quarantined_report` has a row with `reason = 'mac_conflict'`
      carrying the payload verbatim, and `inventory_alert` has an open `duplicate_identity`
      row. Send the same message again: **another quarantine row, still one alert.** Every
      report is evidence; alerts dedupe (D37).
- [ ] There is also **no `device_state` row** for that MAC. A report the platform does not
      believe must not become the device's reported configuration either.
- [ ] **Now report a MAC that is in no inventory row at all** — change the topic's MAC to
      `02:EE:0E:FF:FF:FF`, publishing as `dev-demo-agg-rc-01`. `outcome: quarantined` again,
      but the row's reason is `unknown_mac` and **no alert opens**: nothing disagrees with
      anything, this is simply a Listener nobody has entered yet, and the quarantine row is
      how an operator finds it (D76).
- [ ] **And report as an Aggregator that does not exist.** Publish to
      `eoe/redwood-coast/agg/ghost-device/reported` as the `platform-redwood-coast` account
      (a device credential could not reach that subtree — which is the point). `outcome:
      unprovisioned`, and `inventory_alert` gains a `provisioning_required` row. Spec 4.3
      item 3: unprovisioned detection is a membership check against inventory, never equality
      to a sentinel, so two unconfigured dev boxes cannot collide with each other.

### Events land, and land once

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -q 1 -t eoe/redwood-coast/agg/demo-agg-rc-01/event -m '{"schema_version":1,"at":"2026-08-10T12:10:00Z","level":"warn","code":"listener_stream_gap","detail":"listener 02:EE:0E:01:01:01 gap 240ms","listener_mac":"02:EE:0E:01:01:01"}'
```

- [ ] `outcome: event`, and the `events:` line lists it.
- [ ] **Publish the identical message again.** `outcome: duplicate_event`, and the `events:`
      line is unchanged — one row, not two. An event carries no device-supplied id, so its
      identity is (emitter, instant, code); a duplicated row would be a lie about how often
      something happened, read straight off the timeline E3.11 builds (D77).
- [ ] Change only the `at` to a minute later and publish. **Two rows now.** Dedupe must not
      swallow a recurring fault — a stream gap every minute is a minute-by-minute story.
- [ ] Publish an event with no `listener_mac` at all (drop the field; `"code":"config_applied"`
      will do) twice. Still **one row**. That is the `NULLS NOT DISTINCT` index doing its job:
      without it, exactly the lifecycle events an Aggregator emits about itself would be the
      ones that doubled.

### What is deliberately not here yet

- [ ] Publish to the **status** topic
      (`-t eoe/redwood-coast/agg/demo-agg-rc-01/status -m '{"schema_version":1,"state":"online","at":"2026-08-10T12:15:00Z"}'`).
      `outcome: not_mine`. The consumer subscribes to it — spec 9.3 makes LWT the
      authoritative Aggregator liveness verdict — but **E3.8** owns what happens next, and a
      recognized-and-dropped message is an honest seam rather than a silent one.
- [ ] Stop the probe with Ctrl-C and delete `backend/check-consumer.py`.

## 7. The loop runs itself (E3.7)

§5 published by hand and §6 consumed by hand. This section is the process that does both
without you: the `worker` container. It holds the subscriptions §6 wired manually, and it
adds the two things no message could ever trigger — a revision timing out because nobody
answered, and drift found by re-comparison rather than announced by a device.

Two things make this section honest and are worth knowing before you start. The worker
keeps **no state of its own**: the pending window lives in `config_revision.published_at`
and the desired config lives in a retained message, which is why you can restart it
mid-flight below and nothing is lost. And it **never republishes**: `auto_reconcile` is
stored, defaults off, and is inert pending spec 17 item 3, so every repair below is an
operator action.

### The worker is running

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa ps worker
docker compose -f deploy/docker-compose.yml -p eoe-qa logs worker | tail -20
```

- [ ] The service is `running`, and the log carries
      `reconciliation worker started (timeout sweep 30s, drift sweep 300s)` followed by
      `connected to the redwood-coast broker at mosquitto:8883` — one line per deployment.
- [ ] Nothing in that log mentions a password or a topic you did not expect. Broker
      credentials go through `SecretStore` and the coordinates object never repr's its
      password (rule R2).

For this section, make the sweeps impatient so you are not watching a clock. Add these to
`deploy/.env` and restart the worker:

```
EOE_TIMEOUT_SWEEP_SECONDS=5
EOE_DRIFT_SWEEP_SECONDS=5
EOE_PUBLISH_ENABLED=true
```

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa up -d worker api
```

- [ ] The worker log now says `timeout sweep 5s, drift sweep 5s`.
- [ ] The **api** log says `outbound publish connections started (EOE_PUBLISH_ENABLED is on)`.
      The API dials the broker only when publication is on: with the flag off there is
      nothing it could send, so a connection would be a socket held open to do something the
      platform has forbidden (D86). It registers no subscriptions — consuming is the
      worker's job, and the two processes never overlap.

### Publishing is an operator action, and it is gated

Sign in as the seeded owner and find a draft revision (make one through
`POST /config/apply` as in §5, or reuse one from the E2 walkthrough). With
`EOE_PUBLISH_ENABLED` still off — comment it out and `up -d api` again if you already
turned it on — POST to the new route:

```bash
curl -i -X POST http://localhost:18000/api/v1/revisions/REVISION_ID/publish \
  -H "X-CSRF-Token: CSRF" -b cookies.txt
```

- [ ] **409**, code `conflict`, message naming `EOE_PUBLISH_ENABLED`. The revision is still
      `draft`. Now turn the flag back on, `up -d api`, and repeat.
- [ ] **200**, `"state": "pending"`, `"trigger": "publish"`, `"transitioned": true`, and a
      `topic` of `eoe/redwood-coast/agg/demo-agg-rc-01/desired`.
- [ ] Read the retained message off the broker as the device (the `mosquitto_sub` command
      from §5). The `revision_id` matches what you just published.
- [ ] Repeat the same POST. **200 again**, `"transitioned": false`, and the state is still
      `pending` with no second audit row. The bytes go out again on purpose (D72) — that is
      the repair for a broker that lost its retained store — and the state does not move.

### Silence becomes `failed(timeout)`

The demo deployment's window is 300 seconds. Shorten it, then say nothing:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec postgres \
  psql -U eoe -d eoe -c "UPDATE deployment SET pending_timeout_seconds = 10 WHERE slug = 'redwood-coast'"
```

- [ ] Within a few seconds the worker log says
      `revision ... timed out after 1Xs with no device report`, and
      `GET /api/v1/revisions/REVISION_ID` reads `failed`.
- [ ] The audit log (`GET /api/v1/audit?action=revision.timeout`) has one row with
      **no actor**. Nobody did this and no device did either — that is the entire content of
      the entry, and it is why `failed(timeout)` may never be reused for a device that
      answered wrongly (D70; a wrong answer fails immediately as `report_error`, which you
      saw in §6).
- [ ] Publish the same revision again (`failed -> pending`, `"trigger": "retry"`) and watch
      it time out a second time. The window restarted: measured from `created_at` it would
      have failed instantly, before the device had a moment to answer (D84).

### Drift nobody reported

Set the window back to something patient
(`UPDATE deployment SET pending_timeout_seconds = 300 WHERE slug = 'redwood-coast'`),
publish a fresh revision, and have the device ack it exactly as in §6 so it reaches
`applied`. Then make it diverge **without telling the platform which revision it applied**:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -q 1 -t eoe/redwood-coast/agg/demo-agg-rc-01/reported -m '{"schema_version":1,"reported_at":"2026-08-10T13:00:00Z","applied_revision_id":null,"config":{"logging.verbosity":"debug"},"checksum":"sha256:REPLACE_WITH_THE_CHECKSUM_OF_THAT_CONFIG"}'
```

(The checksum must be that config's own — the platform recomputes it and rejects a device
that contradicts itself, §6.)

- [ ] The message alone moves nothing: it names no revision, so there is no spec 6.2 edge to
      take. `device_state` updates and the revision is still `applied`.
- [ ] Within one drift-sweep interval the worker log says
      `aggregator ... has drifted from revision ... (N differing key(s))` and the revision
      reads `drifted`. **This is the case §6 cannot cover**, and the whole reason spec 6.4
      item 5 exists: divergence with no report that could drive it.
- [ ] `GET /api/v1/audit?action=revision.drift` shows `found_by: drift_sweep` and a
      `differing_keys` list of key NAMES. No values anywhere — the snapshot holds secret
      markers and the device's values are of unknown provenance (rule R2).
- [ ] POST the publish route again: `"trigger": "republish"`, state `pending`. That edge has
      no other driver in this phase.

### The flag that does nothing, on purpose

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec postgres \
  psql -U eoe -d eoe -c "UPDATE deployment SET auto_reconcile = true WHERE slug = 'redwood-coast'"
```

- [ ] Drive the device to `drifted` again (ack, then diverge). The worker log now adds
      `deployment ... has auto_reconcile on; it is INERT pending spec 17 item 3`.
- [ ] The revision stays `drifted`. Nothing was published, and `published_at` did not move.
      The column exists so the spec 17 item 3 decision has somewhere to land; the phase that
      implements the policy is the phase that may act on it (D81).
- [ ] Set it back to false.

### Restarting the worker loses nothing

Publish a revision so something is `pending`, then:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa restart worker
```

- [ ] While it is down, shorten the window again (`pending_timeout_seconds = 10`). The new
      worker fails the revision out on its first sweep — a window it never started, opened
      by a process that no longer exists.
- [ ] The device can still read its desired config off the broker (the §5 `mosquitto_sub`
      command) even though both the publisher and the worker have restarted since. Postgres
      and the retained message ARE the handover; there is nothing else (spec 14.3).

### Who may publish

- [ ] As a **viewer** scoped to redwood-coast, POST the publish route: **403**, naming
      `manage_config`. Not a 404 — they can already read that revision through
      `GET /revisions/{id}`, so pretending it does not exist would be a lie about a row on
      their screen.
- [ ] As an operator scoped to a **different** deployment: **404**. They cannot see it, and
      a 403 would confirm it exists (D35).
- [ ] Stop the broker (`docker compose ... stop mosquitto`) and publish: **503**, code
      `service_unavailable`, and the revision is exactly where it was. The publish rides
      inside the database transaction, so a broker outage never leaves a `pending` revision
      no device was told about — which would resolve as a timeout and blame the device for
      the platform's outage (D74, D83). Start the broker again.
- [ ] Remove **all three** lines you added — `EOE_PUBLISH_ENABLED` and both
      `EOE_*_SWEEP_SECONDS` — from `deploy/.env` when you are done, and `up -d worker api` to
      return to the real cadences. Leaving them behind no longer breaks the gate (the
      container tests pin every compose variable as of D87), but `deploy/.env` is what your
      next `compose up` reads, and a stack that publishes to devices because of a line you
      left in a file three sessions ago is worth not having.
