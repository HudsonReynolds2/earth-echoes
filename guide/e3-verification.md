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
