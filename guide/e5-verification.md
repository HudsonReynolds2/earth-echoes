# E5 Verification Walkthrough

A hands-on test platform for everything Epic E5 ships — deployment services onboarding: the
five service connections a deployment needs, their write-only credentials, the live
connection tests, the rolled-up status, per-device broker credentials, and the generated
stack. Work top to bottom, ticking each box. Every step says what to do and what you should
see; if you see anything else, that is a finding worth writing down.

This is a living acceptance document (rule R1): E5 ships it, and any later epic that
invalidates an assertion here amends it in the same batch. Siblings:
[E1](e1-verification.md) covers hierarchy and inventory, [E2](e2-verification.md) the
configuration model, [E3](e3-verification.md) the device control plane. **E5 amends E3's
section 1** — the `deployment_service` table it inspects is no longer broker-only — and that
amendment shipped in this same batch.

**The single most important property in this epic is that no service credential is ever
readable back.** Several steps below exist only to try to get one out and fail. Take them
seriously: the stack bundle legitimately contains a private key and five passwords, which
makes every "is this a leak" judgement harder here than anywhere else in the codebase.

## 0. Start the platform

- [ ] From the repo root: `.\qa-stack.ps1` (PowerShell), or the POSIX sequence in
      [the E3 walkthrough](e3-verification.md) section 0. The script ends with
      `QA STACK READY`.
- [ ] `http://localhost:15173` signs in as the seeded owner. The API is at
      `http://localhost:18000`.
- [ ] **Take the QA stack down before you ever run the test gate** (`.\qa-stack.ps1 down`).
      The gate's container tests bind the same fixed host ports and go red against any
      running stack (D44) — and C4's own gate was invalidated twice by exactly this, once by
      a QA stack still holding 15432 and 16379.

Throughout, `$DEP` is the Redwood Coast deployment's id:

```bash
DEP=$(curl -s -b cookies.txt http://localhost:18000/api/v1/deployments \
  | python3 -c 'import json,sys; print(next(d["id"] for d in json.load(sys.stdin)["items"] if d["slug"]=="redwood-coast"))')
```

If you would rather click than curl, every read below is visible at
**Inventory → Redwood Coast → Services**.

## 1. The services row is five services now, not one (E5.1)

E3 created one `deployment_service` row per deployment, for the broker. E5 widened the same
table to the five spec 16.2 services rather than forking a second one.

- [ ] `select service_key, host, port, username, password_secret_name, required, status
      from deployment_service order by deployment_id, service_key;` — after seeding you have
      one `mqtt` row per deployment and nothing else. That is the pre-onboarding state.
- [ ] The mqtt-shaped columns are **conditionally** required. Try to break it:
      `insert into deployment_service (deployment_id, service_key) values ('<dep>', 'influx');`
      succeeds, while
      `insert into deployment_service (deployment_id, service_key) values ('<dep>', 'mqtt');`
      is refused by `mqtt_coordinates_required`. Fifteen nullable columns would have
      constrained nothing; this constrains exactly the row that needs it.
- [ ] A sixth service is refused outright:
      `insert into deployment_service (deployment_id, service_key) values ('<dep>', 'kafka');`
      violates the `service_key` CHECK.
- [ ] **Deleting a deployment still works.** `DELETE /api/v1/deployments/{id}` on a scratch
      deployment returns 204 rather than the 500 it returned before E5.1. Do not run this on
      Redwood Coast.

## 2. Credentials go in and nothing comes back out (E5.2)

- [ ] `GET /api/v1/deployments/$DEP/services` returns **all five keys**, configured or not,
      in spec 16.2's order. An unconfigured service is `"configured": false` with empty
      settings, not an absent key — the wizard renders a fixed set of cards rather than
      discovering which exist.
- [ ] Save an Influx credential:

```bash
curl -s -b cookies.txt -X PUT http://localhost:18000/api/v1/deployments/$DEP/services \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" \
  -d '{"services":{"influx":{"url":"http://influxdb:8181","database":"recordings","token":"walkthrough-token-not-a-real-one"}}}'
```

- [ ] **The response does not contain the token.** `token` comes back as
      `{"$secret_set": true}` — the D51 keep sentinel — and so does every later GET. Grep
      the response for the literal you sent: nothing.
- [ ] **Neither does the database.** `select config, secret_names from deployment_service
      where service_key='influx';` — `secret_names` maps `token` to a *name*,
      `deployment:<id>:influx_token`, and `config` holds the URL and database. No value.
- [ ] **Neither do the logs.** `docker compose -f deploy/docker-compose.yml -p eoe-qa logs
      api | grep walkthrough-token` returns nothing. (If it returns nothing because the API
      logs nothing at all, that was D127 and it is fixed — you should see `app.*` INFO lines
      in there.)
- [ ] The sentinel round-trips. PUT the exact body a GET returned, sentinel and all: it
      succeeds and the stored token is unchanged. This is what lets an operator edit a URL
      on a form they never held the credentials for.
- [ ] A sentinel for a credential that is **not** set is refused with a located 422, not
      silently ignored: send `{"services":{"s3":{"bucket":"b","access_key":{"$secret_set":true}}}}`
      and read the field in the error. Silently writing nothing would leave you believing a
      credential was saved.
- [ ] A misspelled field is a 422 and not a key that quietly never reaches a device:
      `{"services":{"influx":{"url":"...","database":"d","tokn":"x"}}}`.
- [ ] **The PUT is wholesale per service.** Save influx with `database` omitted and it is
      cleared — not merged. This is why the UI form always submits all of its own fields.

## 3. Path A in the UI (E5.12a)

Open **Inventory → Redwood Coast → Services**.

- [ ] Five cards, in spec 16.2's order: Mosquitto, InfluxDB 3, Prometheus, Grafana, Object
      storage. Because Redwood Coast already has services, the page opens on **Path A**.
- [ ] Each card's fields are that service's own — Prometheus asks for two URLs because the
      read endpoint and the remote-write endpoint are genuinely two endpoints with two roles.
- [ ] **The saved Influx token renders as set-ness, not a value.** The field shows `••••••••`
      and the word `set`, with Replace and Clear buttons and **no input at all** until you
      click Replace. Click it: the input that appears is **empty**. Confirm in devtools that
      its `value` is `""` — the field is never populated from a response.
- [ ] An unset credential (Grafana's) shows `not set` and offers its empty input directly.
- [ ] Type a password into a Replace input and search the rendered page for it: it exists in
      that one input and nowhere else — not in a summary, not in a title attribute.
- [ ] Change the Influx URL and Save without touching the token. The token survives: the
      form sent the sentinel. Confirm in the database that `secret_names.token` is unchanged.
- [ ] Fill in a field wrongly (say an empty required Host on Mosquitto) and Save: the error
      renders **on that card** and the other four are untouched.

## 4. The five connection tests (E5.3, E5.4a–e)

You need real services to test against, and the QA stack has only the broker. **Do section 7
first if you want Influx, Prometheus, Grafana and MinIO** — the generated stack is the
easiest way to get all four. The broker steps here work immediately.

- [ ] Press **Test connection** on the Mosquitto card. Three checks come back: connect, a
      round trip on the deployment's reserved `_selftest` topic, and the dynsec probe.
- [ ] **The dynsec probe reports three verdicts, not two.** Against the QA stack's broker it
      answers `available`. Against a Mosquitto with the plugin absent it answers `absent`,
      and against one where the platform account is not a dynsec admin, `denied` — different
      remedies, so different words. The QA broker gives you `available`; take the other two
      on the suite's word unless you want to edit `mosquitto.conf` and restart.
- [ ] **The probe's discriminator is the SUBACK, not the publish.** This is worth knowing
      when reading the result: an `acl_file` broker grants the subscribe and then silently
      refuses the publish, so the intuitive test would report `denied` where `absent` is
      correct (D114).
- [ ] Break it on purpose: Replace the Mosquitto password with garbage, Save, Test. The
      verdict is `failed`, the failing check names what happened, and — the property that
      matters — **the failing check carries a remedy telling you what to do**. Every failing
      check does; the suite asserts it. Put the real password back afterwards.
- [ ] **A test of unsaved credentials writes no verdict.** Type a wrong Influx token, press
      Test *without saving*, and watch the result come back `failed` while the card's own
      status stays what it was. Spec 16.2's "validates each entry before accepting it" is
      precisely a test that has not been accepted yet.
- [ ] **Object storage with both credentials blank reports `not_required`, not a failure.**
      Two of the four outcomes are not failures and the UI says so in words.

## 5. Status, and the rollup (E5.5)

- [ ] `GET /api/v1/deployments/$DEP/services/status` returns two vocabularies in one body:
      the deployment's `services_status` (`unconfigured` / `pending_verification` /
      `verified` / `degraded`) and each service's own (`untested` / `verified` / `failed`).
      **They are not aliases.** The right-hand panel in the UI renders the first; the cards
      render the second.
- [ ] The rollup is not device status either. Nothing on this page uses the six spec 9.3
      words (`healthy`, `sleeping`, `offline`, …) or the device status chip.
- [ ] Fail a required service twice in a row and the deployment goes `degraded`; the panel
      names the threshold it used rather than hardcoding it.
- [ ] An **unconfigured object storage cannot hold the deployment back.** With the other four
      verified, `services_status` reaches `verified` while s3 is still `untested` and labelled
      `optional`. The `required` flag comes from the API, not from a rule the frontend
      invented.
- [ ] **Nothing re-checks on a timer, and the UI says so.** Spec 16.5's periodic re-checks
      are closed as *deliberately not built* (D133): a timer reports a fact that was true
      minutes ago. Degradation comes from observed events — a test you run, a rotation's
      re-verification, and for the broker the control plane's own connection and last-will.
      The panel's wording says exactly this. If you ever see "re-checks run every N minutes"
      on that panel, that is a regression toward the old S5 mock.

## 6. Per-device broker credentials, and config that reaches a device (E5.6, E5.7)

- [ ] `POST /api/v1/aggregators/{id}/broker-credential` mints a credential through the
      Mosquitto dynamic security API. `select * from broker_credential;` shows the row; the
      password is in `secret`, by name, as everywhere else.
- [ ] The minted account can connect and is confined to its own subtree. The E3 walkthrough's
      section 1 mosquitto_sub/pub commands work verbatim against it.
- [ ] **A device gets messages, not a silent subscription.** The grant list renders to two
      dynsec acltypes for `read`, because `subscribePattern` alone grants the subscribe and
      delivers nothing (D120).
- [ ] Delete an aggregator while the broker is stopped. The delete **succeeds** and leaves
      the credential `revoke_pending`; the sweep finishes it when the broker is back (D121).
      An unreachable broker never blocks a device delete.
- [ ] **Service settings reach devices as configuration.** After saving services, look at the
      deployment's `entity_override` row: the twelve service keys are there as a regenerated
      projection, secrets as markers. Then watch a desired-config message: it carries secret
      MARKERS and never plaintext (D126).
- [ ] Clear an optional service field and save. The projection is regenerated **wholesale**,
      so the cleared key leaves the projection rather than surviving forever.

## 7. Path B: generate a stack, run it, verify it (E5.8–E5.10, E5.12b)

This is the epic's centrepiece: the platform renders a complete, runnable service stack with
every credential minted and registered before a byte is written.

Use **High Desert** for this — it has no services configured, so its Services page opens on
Path B.

- [ ] The page opens on **Path B · generate a stack for me**, and you can switch to Path A
      and back. The five verify cards are present on both paths.
- [ ] **The credential warning is on the page before anything is downloaded**: the archive
      contains a private key and every service password in usable form.
- [ ] Set the hostname to something reachable from your machine (`localhost` is fine) and
      press **Generate stack**. Optionally tick object storage.
- [ ] **Every service comes back `untested`, and the rollup is `pending_verification`.** A
      generated stack does not get to vouch for itself. If you ever see `verified` here
      without having run a test, that is a serious regression.
- [ ] `select service_key, status from deployment_service where deployment_id='<high desert>';`
      — five rows (four without object storage), all `untested`. The credentials are in
      `secret` already: **generation commits rows and secrets before rendering anything**
      (fixed choice 7).
- [ ] Press **Download bundle**, then press it again and diff the two files:
      **byte-identical**. That determinism is what lets the platform keep no copy.
- [ ] Unpack it. You get `docker-compose.yml`, `mosquitto/`, `prometheus/`, `grafana/`, an
      `.env` and a `README.md`. The README lists every port the compose file publishes, and
      the compose file publishes every port the README lists.
- [ ] `docker compose -f <unpacked>/docker-compose.yml config` exits 0.
- [ ] **File permissions are right for containers that drop privileges.** `ls -l` the
      unpacked tree: only `.env` is 0600. The broker's `server.key` is world-readable on
      purpose — at 0600 Mosquitto refuses to start with `Unable to load server key file`,
      which is a real defect this walkthrough's ancestor found by running the bundle rather
      than reading it.
- [ ] `docker compose up -d` in the unpacked directory. It comes up in around fifteen
      seconds. `docker compose ps` shows the broker, Influx, Prometheus, Grafana, and — if
      you ticked it — MinIO plus two short-lived init containers that exit 0.
- [ ] Now go back to the Services page and press **Re-test** on each card, or Test on all
      five. **All of them pass**, and `services_status` reaches `verified`.
      This is spec 16.3's own sentence executed rather than described.
- [ ] Grafana deserves a look: the platform generated an admin account, used it **once** to
      have Grafana mint the `echoes-platform` service account token, and stored that. Sign in
      to Grafana at the port the README names and confirm the service account exists. Every
      test after the first uses the scoped token, never the admin password.
- [ ] **Nothing was written server-side.** There is no bundle on the API container's disk and
      no blob column; the download re-rendered from the stored rows.

## 8. Rotation is a config revision, not a redistribution (E5.11)

- [ ] With the stack running and verified, press **Rotate credentials** and confirm. Rotation
      regenerates every credential, re-renders, re-verifies and republishes.
- [ ] The result names **how many Aggregators were told**, and it is not zero for a
      deployment with Aggregators. Listeners get none — they have no service credentials.
      (If it reports zero for everything, that was the bug D134 exists for: a desired snapshot
      carries secret NAMES, which do not change when a value rotates, so rotation was
      invisible until a non-secret counter was added.)
- [ ] The old credentials are **gone** from SecretStore. Try the pre-rotation Influx token
      against the running stack: rejected.
- [ ] **Re-verification will probably fail, and the new credentials will have been published
      anyway.** This is the deliberate inversion: you have not restarted the stack with the
      new bundle yet, and that is exactly when the devices most need the new credentials.
      The UI says so in as many words and leaves the deployment `degraded`, honestly.
- [ ] Download the new bundle, `docker compose up -d` again, re-test: back to `verified`.
      `verified` is never set optimistically — only a real test pass moves it.
- [ ] **Rotating a deployment that never generated a stack is a 404, not a silent generate.**
      Try it on a deployment you have not generated for. A mistyped id must not mint a fresh
      stack for the wrong deployment.
- [ ] Wait a minute — literally — and re-read `services.credentials_generation` in the
      deployment's config. It must still be the post-rotation value. It used to be reset to 0
      by the once-a-minute sweep, which threw away a rotation's entire signal to its devices
      inside sixty seconds (D139).

## 9. The spec 16.5 gate, and who can do any of this (E5.12b)

- [ ] With the broker verified, the Provisioning panel reads **Unblocked**. With another
      required service still failing, it also carries the spec's warning: devices provisioned
      now come online with nowhere to ship analysis, metrics or audio.
- [ ] Break the broker (wrong password, save, test). The panel flips to **Blocked** and says
      why: a provisioning bundle embeds the device's broker credentials, so spec 16.5 needs a
      verified broker first. **E5 reports this gate; E4's bundle generator is what will
      enforce it, and does not exist yet.**
- [ ] Sign in as a **viewer** and as a **field tech** (`Users` admin, or seed one). Both see
      every service, its status, and the rollup. Neither sees Save, Test, Re-test, Replace,
      Generate, Download or Rotate, and the Path A/Path B switch is not there at all.
      `MANAGE_SERVICES` is Owner and Deployment Operator only: service credentials are the
      deployment's keys to everything, and a field tech's job is hardware.
- [ ] The API agrees, not just the UI. As a field tech,
      `PUT /api/v1/deployments/$DEP/services` returns 403 and
      `GET /api/v1/deployments/$DEP/services/status` returns 200.
- [ ] `select action, detail from audit_log where action like 'services%' order by at desc;`
      — every save, test, generation, download and rotation is there. **The details name
      fields and counts and never a value**: a download records the byte count and nothing
      else, a save records which field names were written, a test records outcomes by service.

## 10. Try to get a credential out

Every step here should fail. If any of them succeeds, stop and write it down.

- [ ] Any `GET` under `/deployments/{id}/services` — no plaintext, sentinels only.
- [ ] The audit log — names, counts and outcomes only.
- [ ] `docker compose -p eoe-qa logs api worker | grep -i -E 'password|token|secret_key'` —
      no values. Names and markers are fine and expected.
- [ ] `select * from deployment_service;` — names, never values.
- [ ] `select * from entity_override where entity_type='deployment';` — the twelve projected
      keys carry markers.
- [ ] The retained desired-config message for an Aggregator — markers, never plaintext.
- [ ] `git grep` for any credential you typed during this walkthrough, across the working
      tree. The repo-layout scanner runs this idea as a test; run it by hand once anyway.

The one place a credential legitimately exists in the clear is **inside the downloaded
bundle**, which is why the archive is treated as a credential everywhere it is mentioned:
the UI warning, the README's own section, and the audit record that says who took it and
when without saying what was in it.
