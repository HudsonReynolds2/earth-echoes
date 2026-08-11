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

then `docker compose -f deploy/docker-compose.yml -p eoe-qa up -d --build`, then
`uv run python -m app.seed --demo`, then:

```bash
cd backend && uv run python -m app.devbroker --host mosquitto --keep-tls
```

and finally `docker compose -f deploy/docker-compose.yml -p eoe-qa restart mosquitto`.

> **`-p eoe-qa` is not optional.** Without it Compose names the project after the directory
> — `deploy` — and every `-p eoe-qa` command in the rest of this walkthrough then talks to a
> stack you do not have, while the one you do have keeps holding the host ports. That is a
> real gate failure that has happened (D90).

> **Before you ever run the test gate** (`.\gate.ps1` / `make gate`): take the QA stack down
> — `.\qa-stack.ps1 down`, or on POSIX
> `docker compose -f deploy/docker-compose.yml -p eoe-qa down`. The gate's container tests
> bind the same fixed host ports and will go red against ANY running stack (D44). If the
> gate reports `port is already allocated`, `docker ps` will name the container holding it.

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

## 8. Is it alive? (E3.8)

Every section so far asked what a device is *doing*. This one asks whether it is *there* —
and spec 9.3 is emphatic about who gets to answer. Prometheus does not: its agent buffers to
a write-ahead log and backfills on reconnect (spec 10.4), so a device that died five minutes
ago can still be filling in metrics for the time it was dead. MQTT answers, because the
broker notices a dropped socket the instant it drops.

The mechanism is the Last Will and Testament. When an Aggregator connects it hands the broker
a message and says "publish this if I disappear without saying goodbye". You are going to be
the broker's witness to exactly that.

Start from the §0 stack, with the worker running (§7).

### The device announces itself

Read a device password out of `deploy/dev-certs/accounts.json` (gitignored, regenerated by
the dev-broker script) and publish an `online` status as the device itself:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub \
  -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt \
  -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -q 1 -r \
  -t eoe/redwood-coast/agg/demo-agg-rc-01/status \
  -m '{"schema_version":1,"state":"online","at":"2026-08-10T12:00:00Z"}'
```

- [ ] The worker log says `aggregator demo-agg-rc-01 is online (first status)`.
- [ ] In the database, one `aggregator_status` row:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec postgres \
  psql -U eoe -d eoe -c "SELECT online, declared_at, changed_at, received_at FROM aggregator_status"
```

- [ ] `online` is `t`. Note `declared_at` (the device's clock, `12:00:00Z`) and `changed_at`
      (the platform's, just now). **They are different columns because they answer different
      questions**, and the next step is why that matters.
- [ ] `SELECT * FROM device_state` is unchanged. Being reachable says nothing about what
      config a device is running, and E3.8 writes nothing to E3.5's table.

### The retained flag is doing real work

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa restart worker
```

- [ ] The worker reconnects and the log shows it learning the status again, unprompted —
      nobody republished anything. That is the `-r` above: the broker held the message and
      replayed it to the new subscriber. A platform that restarts learns the whole fleet's
      liveness without asking a single device.
- [ ] `changed_at` did **not** move. A replay is not a new outage — if it rewrote that
      column, every platform restart would tell you the fleet went down when your own
      service did.

### The device dies badly

Now the acceptance itself. Open a connection that holds a will, in the background:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec -d mosquitto mosquitto_sub \
  -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt \
  -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -i qa-lwt-probe \
  -t eoe/redwood-coast/agg/demo-agg-rc-01/desired \
  --will-topic eoe/redwood-coast/agg/demo-agg-rc-01/status --will-qos 1 --will-retain \
  --will-payload '{"schema_version":1,"state":"offline","at":"2026-08-10T12:00:00Z"}'
```

Note the will's timestamp: **`12:00:00Z`, the same as the `online` you already sent**. That is
not sloppiness in the example, it is the truth about how wills work — a device composes its
will when it connects, and the broker holds those exact bytes for however long the session
lasts. Hours of newer heartbeats can follow it.

Kill it the way a power cut does, with no chance to say goodbye:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto pkill -9 -f qa-lwt-probe
```

- [ ] Within a second the worker log says `aggregator demo-agg-rc-01 is now offline`. Nobody
      published that message. The broker composed it from the will and sent it the moment the
      socket died without a DISCONNECT packet — which is the entire difference between this
      and a device shutting down politely.
- [ ] `online` is now `f`, and `changed_at` moved.
- [ ] **`declared_at` went BACKWARDS**, to `12:00:00Z`. This is the trap the design exists to
      avoid: if the platform ordered status messages by the device's own clock — the way it
      correctly orders reported state under spec 7.4 — it would have discarded this will as
      stale news and left a dead device reading online forever. Status is ordered by when the
      platform received it, and `declared_at` is stored without ever being obeyed (D88).

### What is deliberately not here yet

- [ ] There is no online/offline dot in the UI. E3.11 puts these transitions on the device
      timeline, E3.12 pushes them live over the websocket, and E6 paints the map. E3.8 ships
      the verdict and the honest absence of a place to show it.
- [ ] Listeners have no status topic and never will: they hold no MQTT session (spec 6.4), so
      their half of the spec 9.3 verdict comes from the Aggregator-reported liveness block —
      E3.9, the next section.

## 9. The Listener that sleeps on purpose (E3.9)

Listeners are the one device class the platform cannot ask a question. They hold no MQTT
session (spec 6.4) — no LWT, nothing to ping — and under `capture.mode=duty_cycle` they are
deliberately silent for most of their lives. **A platform that treated silence as failure
would report a perfectly healthy deployment as a fleet-wide outage every night**, which is
exactly what spec 6.5 exists to prevent.

The answer is a promise. Before sleeping, a Listener tells its Aggregator over the local
HaLow link when it will be back. The Aggregator trusts that time rather than recomputing the
schedule — the Listener's own clock governs when it actually wakes — and the platform is one
step further removed still: it records what the Aggregator says and never evaluates a
deadline itself. This section is mostly about proving that last part.

You need the §0 stack and the device password from `deploy/dev-certs/accounts.json`.

### A sleeping Listener is healthy

Publish a Listener report with a `sleeping` liveness block, as the Aggregator (Listeners
never publish — the Aggregator publishes on their behalf, spec 6.5):

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub \
  -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt \
  -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -q 1 \
  -t eoe/redwood-coast/agg/demo-agg-rc-01/lst/02:EE:0E:01:01:01/reported \
  -m '{"schema_version":1,"reported_at":"2026-08-10T12:00:00Z","applied_revision_id":null,"config":{},"checksum":"sha256:REPLACE","liveness":{"state":"sleeping","expected_wake_at":"2026-08-10T12:05:00Z"}}'
```

(The checksum must be that config's own — the platform recomputes it, §6.)

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec postgres psql -U eoe -d eoe \
  -c "SELECT entity_id, liveness_state, expected_wake_at, liveness_changed_at FROM device_state WHERE entity_type='listener'"
```

- [ ] `liveness_state` is `sleeping` and `expected_wake_at` holds the time the Listener
      promised. **This reads as HEALTHY** (spec 9.3), the same as `streaming`. If you take one
      thing from this section, take that.
- [ ] Publish the identical report again. `liveness_changed_at` does **not** move — it means
      "how long it has been asleep", not "when we last heard about it".

### The platform will not do the Aggregator's job

Now the property worth testing on purpose. Send a `sleeping` report whose `expected_wake_at`
is an hour in the **past**, and raise no event:

- [ ] Wait. Watch the drift and timeout sweeps run. **Nothing happens** — the Listener still
      reads `sleeping`, still healthy.
- [ ] That is correct, and it is not laziness. The platform holds a clock and a promise and
      still has no standing to conclude anything: it does not know the grace period (that is
      `listener.wake_grace_seconds`, a **device** setting that rides the config down), and it
      does not know whether the Listener already came back with the report still in flight.
      Only the Aggregator, on the same local link the Listener declared over, can decide. A
      future phase that adds a sweep to "fix" this would be contradicting spec 6.5.

### The Aggregator decides, and says so

Raise the event the Aggregator would raise:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_pub \
  -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt \
  -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -q 1 \
  -t eoe/redwood-coast/agg/demo-agg-rc-01/event \
  -m '{"schema_version":1,"at":"2026-08-10T12:05:31Z","level":"warn","code":"listener_missed_wake_window","detail":"expected 12:05:00Z, grace 30s elapsed","listener_mac":"02:EE:0E:01:01:01"}'
```

- [ ] The worker log says `listener 02:EE:0E:01:01:01 missed its wake window and is offline`.
- [ ] `liveness_state` is now `offline`, and `expected_wake_at` is **NULL** — the promise is
      spent once it has been missed.
- [ ] One `device_event` row holds the Aggregator's own `detail` text verbatim. That is the
      timeline entry E3.11 will render; the platform never paraphrases the device's account
      of what happened.
- [ ] Publish the same event bytes again: `duplicate_event`, one row, and
      `liveness_changed_at` unmoved. One outage, not two (QoS 1 is at-least-once).

### Coming back

- [ ] Send a `streaming` report. The Listener reads healthy again and `expected_wake_at`
      stays NULL. `offline` is not a terminal state — a Listener that wakes late still wakes.

### What is deliberately not here yet

- [ ] No liveness appears in the UI. E3.11 puts these transitions on the device timeline,
      E3.12 pushes them live, and E6 colours the map from
      `controlplane/liveness.listener_verdict` — the single function all three share, so that
      none of them can independently decide a sleeping Listener looks broken.

## 10. Telling a device what to do (E3.10)

Everything so far has been declarative: the platform states desired config and the device
converges on it. Commands are the exception — `restart`, `resync`, `flush_buffer` are
one-shot imperatives, and they behave differently on purpose.

You need the §0 stack with `EOE_PUBLISH_ENABLED=true` (§7), and a signed-in operator.

### The device has to be listening

The cmd topic is **not retained**. Subscribe as the device first, in one terminal:

```bash
docker compose -f deploy/docker-compose.yml -p eoe-qa exec mosquitto mosquitto_sub \
  -h localhost -p 8883 --cafile /mosquitto/dev/ca.crt \
  -u dev-demo-agg-rc-01 -P DEVICE_PASSWORD -v \
  -t eoe/redwood-coast/agg/demo-agg-rc-01/cmd
```

Then send a command (the aggregator's **platform UUID** goes in the URL — copy it from the
address bar on its page):

```bash
curl -i -X POST http://localhost:18000/api/v1/aggregators/AGGREGATOR_ID/commands \
  -H "Content-Type: application/json" -H "X-CSRF-Token: CSRF" -b cookies.txt \
  -d '{"command":"restart"}'
```

- [ ] **202 Accepted**, not 200. The platform published to a topic; it did not watch the
      device restart. Claiming 200 would report an outcome nobody observed.
- [ ] The subscriber prints the command, and the topic names `demo-agg-rc-01` — the
      `aggregator_uuid`, never the platform UUID you just put in the URL. Two different
      identifiers for one device (spec 4.2); only one of them means anything on the wire.
- [ ] Stop the subscriber and send another command, then start it again. **Nothing arrives.**
      That is the unretained flag doing its job: a command is a one-shot, and a retained one
      would fire at a device coming back online a fortnight later.

### Two presses are two decisions

- [ ] Send `restart` twice with the subscriber running. Both responses are 202 and their
      `command_id` values **differ**, as do the two messages on the wire.
- [ ] That is the spec 7.4 contract, and it cuts both ways. The device deduplicates its own
      QoS 1 retries by `command_id`, so the ids must differ or the device would drop your
      second restart as a redelivery and the API would report success for something that
      never happened. Deduplicating retries is the device's job; deduplicating operators is
      nobody's.

### Who may, and what happens when the broker is down

- [ ] As a **viewer** scoped to redwood-coast: **403**, naming `manage_devices`. Not 404 —
      they can read that aggregator, so pretending it does not exist would be a lie about a
      device on their screen.
- [ ] As an operator scoped to a **different** deployment: **404** (D35).
- [ ] `{"command":"shutdown"}`: **422**. The vocabulary is closed at the boundary.
- [ ] Stop the broker and send a command: **503**, code `service_unavailable`. Then check
      `GET /api/v1/audit?action=aggregator.command` — there is **no row** for it. Nothing
      went out, so nothing is recorded; an audit row here would put a restart on the timeline
      that never happened.
- [ ] With the broker back, a successful command leaves exactly one audit row carrying the
      `command_id` and your user. That id is how a later conversation names one attempt.

## 11. What happened to this device (E3.11)

Every section so far produced state changes. This one is where they become a story an
operator can read months later, which is what spec 6.3 asks for: every transition, with a
timestamp, an actor, the config diff, and whatever the device said.

The load-bearing property is **completeness**. The row is written inside the state machine
itself — the single function allowed to write `config_revision.state` — so a transition that
left no history is not possible rather than merely unlikely.

### Drive a journey, then read it back

Re-run §7's sequence on the aggregator: publish, ack as the device, inject drift, re-publish,
let it time out. Then:

```bash
curl -s "http://localhost:18000/api/v1/aggregators/AGGREGATOR_ID/timeline" -b cookies.txt | jq '.items[] | {from_state, to_state, trigger, actor_email}'
```

- [ ] The entries are **newest first**, and read as the journey backwards: `failed(timeout)`,
      `pending(republish)`, `drifted(report_diverged)`, `applied(report_match)`,
      `pending(publish)`.
- [ ] The two you did carry your email. The three the platform did have `actor_email: null`.
      **That null is a fact, not a gap** — nobody answered, which is the entire content of a
      timeout entry, and the UI says "by the platform" rather than "unknown user".
- [ ] The `pending(publish)` entry carries a `diff` naming the setting you changed with its
      before and after. The other four carry `diff: null`: they moved state without changing
      what was asked for, and repeating the diff on each would read as four config changes.
- [ ] The `drifted` entry's `detail` names the differing **keys** and no values.

### In the UI

- [ ] Open the pod page. Under the Aggregator card there is a **Timeline** panel showing the
      same entries, with the diff as a small before/after table.
- [ ] Open a listener detail page. Same panel, its own history, keyed by MAC.
- [ ] A device you have never published to says "No configuration has been published to this
      device yet" rather than showing an empty box. Never-transitioned is a real answer.
- [ ] Inspect the entries in devtools: they carry `data-revision-state`, **not**
      `data-status`. A revision state and a device status are different vocabularies, and the
      status dots are still E3.12's to earn (D40 is not lifted yet).

### The org-wide view is the audit log, on purpose

- [ ] `GET /api/v1/audit?action=revision.timeout` and its siblings (`revision.publish`,
      `revision.report`, `revision.drift`) render the same story across every device, and
      `?scope=DEPLOYMENT_ID` narrows it to one deployment. That is spec 6.3's second surface,
      and it is E0.8's audit log rather than a second timeline API: two org-wide logs would be
      two answers to one question, and the one nobody looked at would rot (D93).

### History outlives what it describes

- [ ] Delete a revision row directly in Postgres and reload the timeline. **The entry is
      still there.** Every reference out of `reconciliation_event` is deliberately un-FK'd
      (D33): a timeline exists to answer questions about things that are gone, and a cascade
      would erase exactly the history somebody is looking for.

## 12. Live, and finally honest about status (E3.12)

Two things land together here, and they belong together: the platform can now push changes to
a browser as they happen, and the status dots that E1 deliberately refused to draw finally
have something real behind them.

**D40 is lifted here.** E1 forbade every status indicator in the UI because it had nothing
true to put in one, and an invented status is worse than none — an operator who learns the
dots are decorative stops reading them, including on the day one is telling the truth. E3
supplies the real signals: LWT (§8), Listener liveness (§9), revision state (§4). The guard
was rewritten rather than deleted, and this section is where you check that it was earned.

### Status is real, and `unknown` is an answer

- [ ] Open the pod page for a deployment whose devices have never reported. The Status column
      shows a muted **—**, not a chip. That is a device the platform has never heard from, and
      saying so is the whole point.
- [ ] Now publish an `online` status for `demo-agg-rc-01` as in §8. Reload: the aggregator
      card reads **Streaming**.
- [ ] Publish `offline`: it reads **Offline**.
- [ ] Report a Listener as `sleeping` (§9). It reads **Sleeping** — its own status, its own
      glyph, and healthy. A duty-cycled fleet at night must not look like an outage.
- [ ] Drive a revision to `drifted` (§7) on a device that is ONLINE. It reads **Drifted**. Now
      take the same device offline: it reads **Offline**, not Drifted. Reachability outranks
      reconciliation — the drift cannot be repaired until the device is back, and showing
      Drifted would send you to fix a config on an unplugged box.

### Live, without reloading

Keep the pod page open in one window and drive a change from a terminal.

- [ ] Publish an `offline` status for that aggregator. **The card changes without a reload**,
      within a second.
- [ ] Open the browser devtools network tab, filter to WS. There is exactly ONE socket for the
      tab, to `/api/v1/ws`. Each event is followed by ordinary API requests — the socket says
      *what changed*, and the app refetches. It never trusts the event body as data, because a
      browser that reconnects has missed whatever happened while it was away.
- [ ] Stop the API (`docker compose ... stop api`) and watch the socket close and retry with
      backoff. Start it again: the socket reconnects and the page refetches everything at
      once, so nothing missed during the outage stays stale on screen.

### Two scopes, one change (the acceptance)

- [ ] Sign in as an operator scoped to **Redwood Coast** in one browser, and one scoped to
      **High Desert** in a private window. Put both on an inventory page.
- [ ] Drive a status change on a Redwood Coast device. **Only the Redwood Coast window
      reacts.** The other receives nothing — not "receives and hides", receives nothing: the
      filter is applied on the server, per event, per connection, because a socket is a
      long-lived read of everything happening in the platform.
- [ ] Sign out in one window. Its socket closes with code **1008** and does not retry in a
      loop; the other window is unaffected.

### Where status still does not appear

- [ ] The **Configuration** pages show no status chips. That part of D40 still applies: those
      screens describe configuration, not devices, and a dot there would be decoration.
- [ ] The Map is still E6's and alerts are still E7's. The `alerting` status exists in the
      vocabulary and nothing can produce it yet.

## 13. The whole loop, from the UI (E3.13)

Everything until now was a piece. This is the sentence the epic exists to make true:

> You change a setting in the browser, and the device is running it a moment later — with a
> record of who asked, what changed, and what the device said back.

`EOE_PUBLISH_ENABLED` now **defaults to on**, so you no longer set it by hand. Remove it from
`deploy/.env` (§7's cleanup) and restart the API to confirm the default is doing the work.

### The sentence, end to end

Start from a clean §0 stack, with a mock device subscribed as in §5.

- [ ] In the UI: **Configuration → Redwood Coast → Pod 01 → `demo-agg-rc-01`**, change
      `logging.verbosity`, and **Preview**. The preview names the key and its before/after.
- [ ] **Apply.** The response now says `"state": "pending"` and `"published": N` — not
      `draft`. That is the change E3.13 makes: apply used to stop at a draft and wait for
      someone to publish it.
- [ ] Note that N is larger than one. An aggregator-level change moves its Listeners'
      effective config too, so E2 cuts a revision per affected device and **all of them go
      out**.
- [ ] Your subscribed mock device receives the retained desired message within a second,
      carrying the new value and the revision id.
- [ ] Ack it as the device (§6). The revision reads **applied**.
- [ ] The device page's **Timeline** shows exactly two entries for that revision: your
      publish, with your email, and the device's ack, with no actor. That is the whole story
      of a config change, and it was assembled without anyone writing a log line.
- [ ] With the page open the whole time, none of that required a reload (§12).

### When the broker is down

The failure worth trusting the platform about.

- [ ] `docker compose ... stop mosquitto`, then change a setting and **Apply**.
- [ ] The apply **succeeds** — HTTP 200 — with `"published": 0` and `"state": "draft"`.
- [ ] Check the effective config: **your edit is saved.** Publication happens after the write
      commits, so an unreachable broker costs you a publish and never your work.
- [ ] The timeline shows **nothing** for those revisions. The platform did not pretend a
      device had been told.
- [ ] Start the broker and use **`POST /revisions/{id}/publish`** (§7) on one of them. It goes
      out, and the timeline picks up from there. Same route drift repair uses — one publish
      path, one set of refusals.

### Epic E3 is complete

- [ ] Re-read §§1–12 as a set. A config change published to a mock Aggregator lands as a
      retained message; the ack drives pending to applied; injected divergence drives applied
      to drifted; silence past the window drives pending to failed; a newer revision
      supersedes. LWT flips Aggregator status in real time and Listener liveness follows spec
      6.5. Identity conflicts quarantine. All of it is visible on the timeline and pushed over
      the websocket.
- [ ] What is still deliberately absent: the Map (E6), alerts and the `alerting` status (E7),
      provisioning bundles (E4), service onboarding and connection tests (E5), and the
      fleet-scale simulator (SIM).
