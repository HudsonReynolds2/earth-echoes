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

The platform's side of the connection. Nothing runs it automatically yet — the reconciliation
worker (E3.7) owns that — so this section drives it by hand, which is also the clearest way to
see the property it exists for: **a broker outage is not something message-handling code
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
