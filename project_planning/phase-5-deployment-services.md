# Phase 5 Document: Deployment Services Onboarding (Epic E5)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 16 in full, 5.3's deployment service rows as a writer, 12.4 as a consumer
**Depends on:** E0, E1, E2 and E3 complete and merged (E3 merged to `main` as PR #17). E4 has
**not** been built; section 2 resolves the E4/E5 ordering the project plan assumed.

---

## 1. Scope

Build deployment services onboarding: the services data model and its encrypted credential
storage, the write-only credentials API, the connection test framework and the five testers
spec 16.2 names, the rolled-up `services_status` and its degradation lifecycle, per-device
broker credential minting over Mosquitto's dynamic security API, the post-connect delivery
that makes service settings arrive as retained desired config, the generated self-hosted
stack with every credential the platform made itself, rotation, and the onboarding wizard
that drives both paths.

When this phase ends, an operator creating a Deployment has two ways to give it services.
Either they enter the endpoints and credentials of services they already run and watch five
connection tests pass, or they download a stack the platform generated, run
`docker compose up -d` on their KVM host, and click Verify — and the same five tests pass
against it. Either way the deployment reaches `services_status: verified`, every credential
is stored encrypted and never echoed, and an Aggregator that comes online days later finds
its full configuration waiting for it at the broker.

**This phase replaces a runbook, not a service.** Spec 1.4 is explicit that the platform does
not host or run the per-deployment services itself. Every design question that starts "could
the platform manage the Influx instance…" is answered no by the spec. The platform connects
to services, verifies them, generates a stack the operator runs, and stores the credentials.

**The two halves of spec 16.4 are asymmetric on purpose.** The bootstrap block is plaintext
and minimal — the broker endpoint and the device's own credentials, and nothing more.
Everything else travels as retained desired config through the reconciliation loop E3 already
built. This phase builds the second half and the credential minting the first half consumes;
E4 builds the file the first half is written into.

## 2. Prerequisites and inherited interfaces

Read `docs/INTERFACES.md` first. What this phase consumes, and does not redefine:

**`deployment_service`** (`app/models.py`, E3.1) — the broker row, and the table this phase
extends. `INTERFACES.md` already binds how: *"E5 owns extending it with the
Influx/Grafana/Prometheus/S3 rows, connection tests, and the spec 16.5 verification status
lifecycle — by widening the `service_key` CHECK and adding columns here, **not** by starting
a second table."* Its `deployment_id` is a real foreign key, deliberately unlike the D33/D55
evidence tables, because a service row describes a live connection and is meaningless once
its deployment is gone. `password_secret_name` names a SecretStore entry and the row never
holds a credential; `ca_cert_pem` is deliberately *not* a secret, because it is the public
trust anchor.

**`load_broker_coordinates` and `MqttClientManager`** (`app/controlplane/broker.py`, E3.2) —
read, and amended in exactly one respect (E5.7b below). `BrokerCoordinates` carries
`password` as `field(repr=False)` behind a `__str__` that names only the deployment and the
socket, so every log line in that module survives a later `%r`. **Keep both.** `tls_context`
makes a pinned `ca_cert_pem` the *only* trust anchor (D65), never "system store plus this
one"; the services testers reuse it rather than growing a second TLS rule.

**`SecretStore`** (`app/secrets.py`, E0.11) — the only at-rest scheme this phase uses. The
`deployment:{id}:{service_key}` namespace is already reserved for it, and the module docstring
already names this phase: *"E5 stores deployment service credentials (broker, Influx,
Prometheus, Grafana, S3)."* This phase adds no custody machinery; `rotate_kek` already exists
and E8.1 owns the secret-manager backend behind the same interface.

**The settings catalog** (`app/config/catalog.py`, E2.1) — and specifically the **twelve keys
already marked `write_restricted="service_onboarding"`**, all at `lowest_level="deployment"`:
`upload.s3_bucket`, `upload.s3_endpoint`, `upload.s3_access_key`*, `upload.s3_secret_key`*,
`telemetry.influx_url`, `telemetry.influx_token`*, `telemetry.influx_database`,
`telemetry.prometheus_url`, `telemetry.prom_remote_write_url`,
`telemetry.prom_remote_write_user`, `telemetry.prom_remote_write_password`*,
`telemetry.grafana_url` (* = `secret=True`). E2 built the lock; **this phase is the key**.
`upload.s3_prefix` is deliberately *not* restricted (D48) and stays operator-writable at
aggregator level. Spec 5.3 states the rule this implements: "the deployment services
onboarding flow (Section 16) writes them rather than the operator editing them key by key."

**`app/config/validation.py`'s `service_restricted` error code** — the refusal E2 wrote for
this phase, whose message already says the key "is written by the deployment services
onboarding flow (E5, spec 16); it cannot be set through config overrides." That refusal stays
true for operators. Fixed choice 3 explains how E5 writes anyway without deleting it.

**`put_overrides`** (`app/config/overrides.py`, E2.2) — the **only** writer of
`entity_override`, and the owner of the D51 secret-marker convention: a secret's plaintext
goes to SecretStore under `config:{entity_type}:{entity_id}:{key}` and `{"$secret": name}`
goes in the JSONB. This phase writes through it and does not grow a second writer.

**`build_change_plan` / `apply_change_plan` / `snapshot_from_raw`** (`app/config/plan.py`,
E2.6) and **`publish_revision`** (`app/controlplane/publisher.py`, E3.4). A services save is
an ordinary config apply followed by an ordinary publish, and it uses these functions.
`snapshot_from_raw` **already anticipates this phase** — it strips write-restricted keys from
listener snapshots and keeps them in aggregator snapshots, and `runner.py`'s drift sweep
shares that rule so the drift detector cannot itself drift. That mechanism only makes sense
if these keys arrive as ordinary override values, which is why fixed choice 3 is the shape it
is.

**The revision state machine** (`app/controlplane/revision_state.py`, E3.6) — test-critical,
and `transition()` is the only writer of `config_revision.state`. This phase creates
revisions and publishes them; it never writes a state.

**`app/devbroker.py`** (E3.1) — the dev broker generator, and the source of the Mosquitto
material this phase's stack generator needs. **The ACL is the isolation guarantee**: an
Aggregator gets exactly seven grants that are spec 7.2's Direction column read literally, so
a device cannot publish its own desired config and cannot manufacture agreement to defeat
drift detection. E5.6 and E5.8a make that list have exactly one source.

**RBAC** (`app/auth/rbac.py`, E0.7) — the four roles and the eight permissions. Its docstring
states the rule this phase follows: "later epics extend the enum and the map deliberately,
never ad hoc." `tests/test_rbac.py` is test-critical (spec 14.5): extend it, never weaken it.
`frontend/src/lib/rbac.ts` mirrors the map and the parity is tested.

**The error envelope** (`app/errors.py`) — `service_unavailable` (503) already exists for
exactly this phase's class of "a dependency is down, retry" outcome, and the field-level
convention `detail={"errors": [{"key","code","message"}]}` is what the testers' structured
results follow.

**Test infrastructure** (`backend/tests/conftest.py`) — `ephemeral_postgres`,
`ephemeral_broker`, `docker_retry`, `free_port`, and the `pytest_collection_modifyitems` hook
that pins tests to xdist groups **by module**. `ephemeral_broker` ships files in with
`docker cp` rather than a bind mount ON PURPOSE, because bind mounts of WSL/Windows paths
translate differently per host and the gate must behave identically on all three. Every new
container fixture in this phase follows that rule.

**Frontend** (E0.4, E1.8, DES.7) — `.data-table`, `.form`/`.form-field`, `.btn-*`, `.modal*`,
`.outcome-*`, `Can`/`useCan` are E1's vocabulary; extend them rather than starting a second
one. Layout for the verify step comes from **S5 in `Screens.dc.html`** ("Services onboarding
wizard — step 3, verify", labelled E5.12 · spec §16.2, §16.3). v1 holds the layout, the
current token sheets hold the values — **v2 wins on every value** (DES handoff).

### Fixed choices for this phase

These were decided by the owner at plan approval, or follow from a constraint the owner set.
**They are not to be relitigated mid-epic**; a task that appears to need one reopened is a
stop-and-ask.

1. **`deployment_service` widens; it does not fork.** The `service_key` CHECK grows to the
   five spec 16.2 keys. The six MQTT-shaped columns stay exactly where they are — moving them
   would rewrite `load_broker_coordinates`, `devbroker.register_services` and the `port_range`
   constraint for no benefit, and `load_broker_coordinates` is the function every deployment's
   control plane depends on. They become **conditionally required** through a
   `service_key <> 'mqtt' OR (host IS NOT NULL AND …)` CHECK. Two typed JSONB columns carry
   the rest: `config` for the heterogeneous per-service fields and `secret_names` mapping
   field name → SecretStore name, never a value. Fifteen nullable columns whose validity is a
   function of `service_key` is a schema that documents nothing and constrains nothing; a
   CHECK constraint cannot validate a URL anyway, so the typing that matters happens at the
   write boundary, in **one Pydantic model per service** (rule R2, "typed end to end").

2. **`services_status` is a column on `deployment`, written only by one pure function.**
   Per-service status (`untested` / `verified` / `failed` + reason + `last_tested_at` +
   `consecutive_failures`) lives on `deployment_service`, because status is a property of a
   service connection and putting it elsewhere means a join to answer "is Influx up". The
   spec 16.5 rollup is denormalized onto `deployment` because **E6.4's map rollup and E7.4's
   Owner fan-out both read it per deployment**, inside fan-outs that are already
   cross-deployment. The correctness risk of denormalizing is answered by making
   `app/services/status.py::roll_up` the only writer and asserting the invariant across the
   suite, not by arguing about it.

3. **E5 writes the twelve restricted keys as a regenerated projection into the deployment's
   `entity_override` row, through `put_overrides`, behind one keyword-only
   `allow_write_restricted` flag** threaded through `validate_override_map` → `put_overrides`
   → `build_change_plan`. Three signatures, one default, one meaning.
   The rejected alternative — a second resolution source layered into the merge chain —
   fails on four counts that compound: it edits `app/config/merge.py`, which is one of the
   four spec 14.5 test-critical components; six existing consumers (`effective_for`,
   `effective_raw`, `effective_resolved`, `override_chain`, `build_change_plan`,
   `runner.desired_snapshot`) compose the chain from `entity_override` and would each need to
   learn about it; the D51 secret-marker handling already exists and works and would need a
   second answer; and `snapshot_from_raw` was written on the assumption these keys arrive as
   override values.
   **The projection is regenerated wholesale, never merged.**
   `service_settings(rows, secret_store) -> dict` is a pure function from the five service
   rows to the twelve keys, and every save replaces them. Two sources of truth are reconciled
   by making one a deterministic function of the other and recomputing it every time.
   **On the duplicated secret at rest, which looks like an oversight and is not:** the
   projection reads each secret from its service-owned name (`deployment:{id}:influx_token`)
   and hands the plaintext to `put_overrides`, which stores a second copy under
   `config:deployment:{id}:telemetry.influx_token`. Writing a marker that pointed at the
   service-owned name instead would mean an operator unsetting a config key deletes the
   service row's credential out from under the connection tester. Two ciphertexts under one
   KEK, both covered by `rotate_kek`, neither ever in a response, is the right price for
   keeping the two lifecycles independent. **Do not "fix" this.**

   > **Addendum PHASE5-2-02 (2026-08-12, ref project-changes #28):** the flag is **four**
   > signatures, not three: `apply_change_plan` calls `put_overrides`, so the flag has to reach
   > it or the plan a caller was handed cannot be executed. Same default, same meaning
   > (DECISIONS D122). The same entry records the second thing the flag carries, which this
   > choice requires but does not say in the signature list: with it on, every write-restricted
   > key stored at the write target is **dropped before the change map is applied**, which is
   > what makes "regenerated wholesale, never merged" true of a cleared optional field rather
   > than only of a changed one. Separately, the epic's open question about what makes object
   > storage `not_required` is **closed**: E5.4e's "both credentials absent" reading stands, the
   > platform supports raw audio only for now, and no `upload.raw_audio_enabled` catalog key is
   > added (DECISIONS D123).

4. **dynsec is required for v1.** This resolves **spec 17 item 14** in the direction the item
   offers as the alternative: the platform mints per-device broker credentials through the
   Mosquitto dynamic security API, and a broker without it cannot be verified. There is no
   manual-install path and no held-bundle state, which deletes a UI flow, a state machine and
   a class of half-provisioned deployment. The cost is stated rather than hidden: an operator
   running a Mosquitto without the plugin must enable it, and the tester's failure message
   says exactly that. E5.0 files the spec addendum.
   The consequence that must be built, not just written: the dynsec probe's verdict is part
   of broker verification, so `absent` and `denied` both keep `services_status` off
   `verified`, which by spec 16.5 blocks bundle generation.

5. **The five testers run against real ephemeral containers on the happy path and in-process
   fake HTTP servers on the error paths, inside the existing `backend-tests` stage.**
   No new gate stage: splitting the backend suite by marker needs `-m` selection, and R0 makes
   any deselected test a hard failure (`EOE_GATE=1`, `tests/gate_runner.py::enforce`). A
   "fast gate" someone runs and calls green is exactly the loophole R0 closes.
   Real containers are non-negotiable on the happy path because these testers exist to detect
   precisely what a fake cannot have: Prometheus's remote-write receiver being off by default,
   Influx 3's actual auth semantics, Grafana's datasource provisioning shapes, MinIO's SigV4.
   A tester validated only against a fake is a tester validated against its author's beliefs.
   Fakes earn the volume — timeouts, 5xx, malformed bodies, TLS failures, remedy text, the
   whole status cross-product — because inducing those in a container means restarting it with
   a different config and in an in-process handler it is three lines.
   **The gate-time design is part of this choice and is stated in section 5.**

6. **E5 defines `BrokerCredentialProvider`; E4 consumes it.** `phase-4-provisioning.md` §2
   fixed choice 1 says E4.6 ships the seam and E5.6 adds a provider. That ordering assumed E4
   landed first and it did not — E4 is entirely unbuilt. So the dependency reverses: this
   phase defines the protocol and ships `DynsecCredentialProvider` and
   `DevBrokerCredentialProvider`, leaving E4.6 exactly what phase 4 promised it — pick a
   provider and flip `EOE_BOOTSTRAP_CREDENTIALS`. E5.0 files the project-changes entry and
   the addendum on the phase 4 document.

7. **The stack bundle is never stored.** `POST .../services/stack` generates every credential,
   puts each in SecretStore, and writes the five rows in **one transaction, before a byte is
   rendered**; `GET .../services/stack/download` re-renders deterministically from those rows
   and streams. No blob column, no temp directory, no cleanup job, and no window in which a
   bundle exists whose credentials the platform cannot verify. The property that makes this
   legitimate is that **two downloads are byte-identical**, which is why the broker's TLS
   material is generated once at POST and stored rather than regenerated per download.

8. **`app/services/clients/` is the only place a deployment service is dialled from.** Testers
   are written in terms of the clients, which makes spec 16.5's "reusing the Section 10 read
   clients" true **by construction** rather than by discipline, and gives E7 something to
   extend. Spec 18: "the service clients built here are reused by Phase 7." When E7.1 needs
   `aggregator_uuid`-scoped SQL and E7.2 needs PromQL, they add query methods to these classes
   and inherit credential resolution, timeouts, TLS handling and error mapping.
   *Naming caveat, stated once:* `app/services/` means **deployment services**, not "the
   service layer". `app/config/service.py` is the merge accessor and is unrelated.

9. **Two new permissions: `MANAGE_SERVICES` and `VIEW_SERVICES`.** Manage goes to Owner and
   Deployment Operator only — service credentials are the deployment's keys to everything, and
   a Field Tech's role is provisioning hardware, not holding Influx admin tokens. View goes to
   all four roles so status renders everywhere. This extends the enum, `ROLE_PERMISSIONS`,
   `tests/test_rbac.py` and `frontend/src/lib/rbac.ts`. **Extending the test-critical RBAC
   suite is permitted and weakening it is not** (spec 14.5, rule R0); the new rows are
   additions to its table, and every existing assertion stays.

### Process choices the owner set for this epic

- **One branch, `e5-batch-1`, one PR.** Not the five-batch shape phase 4 pencilled.
- **Targeted tests per unit; the full `make gate` at five checkpoints** (section 4's C1-C5),
  not after every numbered task. This is a deliberate, owner-authorized deviation from rule
  R0's per-task gate, taken for wall-clock reasons, and E5.0 records it in `DECISIONS.md`.
  **The compensating discipline is absolute and is what keeps R0's guarantee intact: nothing
  reaches the remote without a full green gate.** Commits between checkpoints are local and
  unpushed; the push and the tag are the gated events, and a red gate is still never
  committed, never pushed, and never summarized as a pass.
- **Concurrency.** The SIM epic is being built in parallel in the same repository. This phase
  works in its own git worktree, appends rather than renumbers in `DECISIONS.md`,
  `project-changes.md` and `project-updates.md`, is additive-only in `conftest.py`, `gate.sh`
  and `ci.yml`, and **does not touch `/sim`, `phase-sim-simulation-harness.md`, or
  `sim-progress-ledger.md`**. Gate tags take `max(existing gate-* tags) + 1` at tag time
  rather than a pre-assigned integer, because SIM's numbering moves.

  > **Addendum PHASE5-2-03 (2026-08-12, ref project-changes #29):** the "additive-only in
  > `conftest.py`" half of this choice is **deliberately broken once**, by an unnumbered
  > infrastructure batch (**INFRA.1**) landing at the head of this branch before checkpoint C4.
  > It rewrites `ephemeral_postgres` — one machine-wide warm Postgres handing out template
  > clones instead of a container per module — and moves every test container's writable state
  > to tmpfs. The reason is section 5's ceiling: C3 measured 299.16s against ~300s, leaving
  > E5.8b and E5.10 no margin, and section 5's pre-authorised trade (cut container-test scope)
  > would have cut E5.10's keystone, which is that unit's entire acceptance. Measured effect
  > and the two defects it cost are in DECISIONS **D128** and **D129**. **Section 5's ~300s
  > ceiling is superseded** — the number it was protecting against no longer describes the
  > suite, and the ledger records the new measured baseline that replaces it. SIM adopts this
  > change the way this worktree adopted SIM's concurrency fix at `167aa6e`: verbatim, later,
  > as an import rather than a re-authoring.

### The E3-owned edits this phase is authorized to make

Two are **discretionary** and both land in **E5.7b**, so the whole chosen cross-epic surface is
one task's diff. Each needs an `INTERFACES.md` amendment and a `DECISIONS.md` entry. **A third
discretionary E3 edit is a stop-and-ask (rule R2).** No edit to `app/contracts/mqtt.py`,
`app/controlplane/consumer.py`, or `app/controlplane/revision_state.py`.

A third edit was **forced rather than chosen** and landed in E5.1: making the four MQTT
coordinate columns nullable turns `Mapped[str]` into `Mapped[str | None]` and breaks
`load_broker_coordinates` under `mypy --strict`, so that function now skips an under-specified
row with a warning naming the deployment and the missing columns, beside its existing
`SecretStoreError` skip (D64's rule, D109's entry). It is unreachable while the new conditional
CHECK holds. It is counted here rather than folded into the two, because a document that says
"exactly two" while the tree contains three is worse than no document.

1. **`MqttClientManager.refresh()`.** `INTERFACES.md` states as a contract that "coordinates
   load once, at `start()`. Adding a deployment's broker row takes a manager restart." That was
   an honest limitation until this phase; E5 changes broker rows as a matter of course — Path B
   writes a new one, rotation changes its password, a new deployment gets its first — so it
   becomes a bug the moment E5 ships. `refresh()` re-runs the loader off the event loop, diffs
   by `deployment_id`, and starts / cancels / restarts tasks. The diff is three lines because
   `BrokerCoordinates` is a frozen dataclass, so a rotated password **is** a difference.
   `start()`'s semantics are unchanged and `_registrations` is manager-level, so a newly
   started task inherits the full subscription set.
   It is needed in **both** hosts: `runner.py`'s worker process and `main.py`'s lifespan
   manager, which are different processes. A poll is correct here rather than LISTEN/NOTIFY —
   a new deployment's broker cannot be dialled before the operator has finished configuring it
   anyway, so a channel, a payload contract and a second delivery path buy nothing.
2. **`service_config_sweep` on the existing sweep runner.** Spec 16.4 requires the worker to
   publish service settings "as soon as the device exists in inventory", so a device created
   *after* the services save must still get its retained config. `ReconciliationWorker`'s
   sweep runner is already a generic `(name, interval, callable)`; this is a third entry and
   one settings field, not new machinery.

> **Addendum PHASE5-4-06 (2026-08-13, ref project-changes #34):** **the third discretionary
> E3 edit was asked for and taken** — `app/controlplane/broker.py` gains `_open_client`, and
> `_connection_loop` establishes its client through it. A cancellation inside aiomqtt's
> `__aenter__` (paho's blocking connect in an executor thread, then the CONNACK) abandoned
> `enter_async_context` before it registered the client, while the executor thread finished
> connecting regardless, leaving a CONNECTED client with a live socket and a running
> `_misc_loop` that no stack owned and `stop()` could not close. It is D94's leak from the
> entry side, and the fix is deliberately D94's shape — own task, shielded, awaited to
> completion on cancellation. Found by E3's own `test_shutdown_leaves_no_running_tasks` under a
> loaded gate and reproduced deterministically before the fix was written (D138). The count in
> this section is now **three discretionary edits and one forced**; a fourth is still a
> stop-and-ask.

### The E2-owned defect this phase must fix on the way through

`DevicePlan.changed_keys` in `app/config/plan.py` compares raw before/after maps **including**
write-restricted keys, while its sibling `snapshot_from_raw` correctly strips them from
listener snapshots. So one services save marks every Listener in the deployment as changed,
`no_op` is False, and `apply_change_plan` mints a revision per listener whose snapshot — after
stripping — is byte-identical to the previous one. On a SIM fleet that is ~600 pointless
revisions and ~600 pointless retained publishes per save.

Fix it at the source: compute `changed_keys` from `snapshot_from_raw(target_type, before)`
versus `snapshot_from_raw(target_type, after)`. That also makes preview honest — "this
listener is unaffected" becomes true — and it uses the same composition rule the drift sweep
already shares. Flag it, record it, and pin it with E5.7a's "exactly one revision per
Aggregator, zero for Listeners" acceptance so it cannot be dropped as an optimization.

## 3. Out of scope

Bundle generation, the archive, the manifest, the Aggregator `settings.yaml` and its bootstrap
block, provisioning records and the tracking board (**E4**, spec 8). This phase ships
`BrokerCredentialProvider` and two implementations and **builds and holds no provisioning
bundle**; the spec 16.5 generation gate is *enforced* by E4.6's predicate, which E5.5 supplies
the data for and does not call. The firmware envelope, per-Pod DEKs and `EOE_FIRMWARE_KEK`
(**E4.5**, spec 8.4) — SecretStore is this phase's only at-rest scheme. Telemetry reads of
every kind: PromQL and Influx SQL that answer a research or dashboard question, Grafana
embeds, the Owner fan-out and its cache (**E7**, spec 10) — this phase builds the clients and
the *connectivity* checks on them, and nothing that returns data. The inbound Grafana alert
webhook receiver and alert surfacing (**E7.6**, spec 11.2) — E5.4d **registers the contact
point** that receiver will consume, and the route it points at does not exist yet, which is
deliberate and is stated at the call site. Alert rule authoring (**spec 11.1** says the
platform does not author rules in v1). The map rollup and any rendering of `services_status`
outside the S5 wizard (**E6.4**, spec 9.3) — this phase owns the column and its definition.
Chameleon Cloud VM auto-provisioning (**spec 17 item 13**); Path B ends at a downloadable
bundle. Secret-manager backends and org-wide KEK rotation (**E8.1**, spec 12.4, spec 17 items
7 and 12). mTLS for the control plane (**E8**; spec 7.1 scopes it to "where the deployment
supports it", and `deploy/mosquitto/mosquitto.conf` currently attributes it to E5's onboarding
— E5.8a amends that comment so the file stops promising something no epic is building).
Any edit to `app/contracts/mqtt.py`, `app/controlplane/consumer.py`,
`app/controlplane/revision_state.py`, or the merge engine — and any weakening of the four
test-critical suites (RBAC, merge engine, revision state machine, provisioning manifest).

## 4. Task list

Eighteen units. Five checkpoints (**C1**-**C5**) mark the full-gate boundaries; every unit
ends with its own targeted tests green, `ruff` clean and `mypy app` clean.

**E5.0 Phase document and records.** This document,
`project_planning/e5-progress-ledger.md`, and the `docs/project-changes.md` entries covering:
E5.0 itself (absent from the plan's twelve-task list); the `BrokerCredentialProvider`
reversal (fixed choice 6); dynsec becoming required (fixed choice 4); the two authorized E3
edits and the E2 `changed_keys` fix; the two new permissions; and the one-branch / five-
checkpoint process. Plus the matching addenda on `echoes-of-earth-project-plan.md` §3, on
`phase-4-provisioning.md` §2 fixed choice 1, and on the spec recording item 14 as resolved for
v1. Docs only; the gate is a regression check.
*Acceptance:* every fixed choice above appears in `DECISIONS.md` or is scheduled onto the task
that implements it; the phase 4 document carries an addendum saying its fixed choice 1 now
consumes an E5-owned interface rather than defining one; the ledger's rows exist and their
count equals the number of numbered units here.

**E5.1 Services data model.** The migration and models of fixed choices 1 and 2, plus the
deployment-delete fix. Widen `service_key_vocab` to the five keys; make `host`, `port`,
`username`, `password_secret_name` nullable behind the conditional `mqtt` CHECK; add
`config` and `secret_names` JSONB, `status` + `status_reason` + `last_tested_at` +
`consecutive_failures` + `last_test_detail`; add `deployment.services_status`. Then
`app/services/store.py` (`load_service`, `load_services`, `upsert_service`,
`delete_services_for`).
**`DELETE /deployments/{id}` is broken today and this task fixes it:** `delete_deployment`
refuses only on `pods` and `role_assignments`, and `deployment_service.deployment_id` is a
hard FK, so deleting any deployment `devbroker` has touched raises an `IntegrityError` the
catch-all turns into a 500. The fix is deletion beside the existing `delete_overrides_for`
call, **not** a new refusal — `devbroker` writes an `mqtt` row for every deployment, so
refusing would make deletion permanently impossible. Phase 1's "no cascade deletes" choice is
about entities in the hierarchy; a service row is infrastructure attached to the deployment,
and the `entity_override` cleanup already in that function is the right analogy.
*Acceptance:* the five service keys are pinned in a test against a **hardcoded transcription
of the spec 16.2 table**, the way `test_settings_catalog.py` pins the catalog against spec 5.3,
so a sixth key without a spec change is a red gate; inserting a non-`mqtt` row with null
`host`/`port` succeeds and an `mqtt` row with null `host` is rejected **by the database**, not
by Python; `load_broker_coordinates` returns identical `BrokerCoordinates` before and after the
migration, asserted by seeding through `devbroker.register_services`; deleting a deployment
carrying all five rows and their secrets returns 204, leaves zero `deployment_service` rows and
zero `deployment:{id}:*` secrets, still refuses (409) for the E1 child blockers, and deletes
the secrets only after the commit per D51.

**E5.2 Write-only secrets API.** `GET/PUT /deployments/{id}/services` in
`app/api/services.py`, with one Pydantic model per service in `app/services/schemas.py` so the
JSONB is typed at the boundary. Secret fields accept a plaintext string, the D51 keep sentinel
`{"$secret_set": true}`, or omission; the GET renders a set secret as the keep sentinel and
never as a value — reusing `app/config/validation.py::KEEP_SENTINEL` rather than inventing a
second wire shape, so the services form round-trips exactly like the config editor already
does. Plus the fixed-choice-9 permissions and their frontend mirror.
*Acceptance:* a PUT setting every secret on all five services followed by a GET produces a body
in which **none of the submitted plaintexts appears as a substring anywhere**, asserted by
scanning the serialized response for the literal values rather than by inspecting field names;
the round trip holds — `PUT(body) → GET → PUT(that body) → GET` yields identical stored secret
names and identical `SecretStore.get` results, so a UI can save a form it never held the
secrets for; PUT is `MANAGE_SERVICES`-gated and audited with a detail naming changed **keys**
and never values; a field tech and a viewer both get 403 on PUT and 200 on GET; `test_rbac.py`
and the frontend parity test both cover the two new permissions.

> **C1 — full gate.** Baseline and post-C1 warm gate durations recorded in the ledger.

**E5.3 Connection test framework.** `app/services/testers/base.py`: the `ServiceTester`
protocol, `TestResult` (`service_key`, `outcome`, `checks`) and `CheckResult` (`name`,
`passed`, `detail`, `remedy`, `elapsed_ms`), the concurrent runner with a per-tester and a
whole-call timeout budget, and `POST /deployments/{id}/services/test`. The runner takes
**candidate** credentials from the request body or **stored** ones when the body is absent,
because spec 16.2 says the platform "validates each entry with a live connection test before
accepting it" — a tester that can only run against stored rows cannot implement that sentence.
*Acceptance:* a tester dialling a black-holed address (a socket bound and never accepted,
in-process) fails within its declared budget ±1s and the whole call returns within the
whole-call budget, asserted with a clock, because an API request that hangs on a dead
deployment is the failure mode this endpoint invites; one tester raising an unexpected
exception yields `fail` with a redacted reason for that service and real verdicts for the other
four without a 500 (S5's own caption: one failure never blocks reading the other four); every
`CheckResult.remedy` is non-empty on every failure path the suite exercises, asserted
table-driven; no credential appears in any `TestResult`, in any record captured by `caplog`
during the suite, or in the audit row.

**E5.4a-e Service testers — five units, one per service.** Each ships one tester in
`app/services/testers/`, the client it is written against in `app/services/clients/`, and its
share of the container rig (fixed choice 5; the rig itself lands with E5.4b, the first unit
that needs a non-Mosquitto container).

- **E5.4a MQTT tester and dynsec probe.** Connect, then publish and subscribe on a reserved
  leaf under the deployment root, built through `contracts.mqtt.deployment_root` and never a
  literal. Plus the three-valued dynsec probe: a well-formed `getDefaultACLAccess` response is
  `available`; an error response is `denied` (the plugin is installed but this account is not an
  admin — a real, common state with a completely different remedy); no response before the
  timeout is `absent`. A boolean would collapse two remedies into one verdict, and S5's whole
  premise is that failures show the remedy.
  *Acceptance:* the round trip succeeds against `ephemeral_broker` and fails with a
  distinguishable reason for each of wrong password, untrusted CA and unreachable host; the
  probe returns `absent` against the current dev broker and `available` against a
  dynsec-enabled variant of the same fixture, with `denied` driven by a dynsec broker whose
  account lacks the admin role; the probe never publishes to `$CONTROL` on a broker it has not
  first confirmed accepts the connection; per fixed choice 4, a non-`available` verdict fails
  the tester rather than warning.
- **E5.4b InfluxDB 3 tester.** Authenticated query, then write and delete of one point in a
  reserved measurement. `httpx` over the HTTP query API — **not** FlightSQL, so no `pyarrow`
  enters the dependency tree; E7 may add a FlightSQL transport behind the same client later.
  *Acceptance:* the write-then-delete leaves the reserved measurement with zero rows, asserted
  by querying it afterwards, because a tester that writes and cannot clean up pollutes a
  research database; a wrong token fails `auth` and a wrong database fails `not_found`, and the
  two are distinguishable in `CheckResult.detail`.
- **E5.4c Prometheus tester.** Authenticated `up` instant query against the read URL, plus an
  authenticated probe of the remote-write endpoint confirming the receiver is enabled and the
  credentials accepted.
  *Acceptance:* the probe distinguishes receiver-disabled (404 without
  `--web.enable-remote-write-receiver`) from credentials-rejected (401) from accepted, asserted
  against a real Prometheus started once with the flag and once without; whether the probe
  leaves a sample visible to the read query is asserted either way, so a later change to it is
  deliberate.
- **E5.4d Grafana tester.** `/api/health`, datasource enumeration for Influx and Prometheus
  with an **offer** to provision the missing ones — provisioning is a separate explicit call,
  never a side effect of a test — and registration of the platform's alert webhook contact
  point.
  *Acceptance:* running it twice against the same Grafana leaves exactly one contact point with
  the platform's name and exactly one datasource per store, asserted by diffing Grafana's own
  listing, because this is the one tester that mutates a system the operator also edits by
  hand; the contact point's URL is the platform's `POST /webhooks/grafana-alerts` path, **which
  E7.6 implements and this phase does not**, stated in a comment at the call site.
- **E5.4e Object storage tester.** Head bucket, then put and delete a zero-byte object under a
  reserved prefix. `boto3` through `asyncio.to_thread`, following `broker.py`'s precedent for
  synchronous work.
  *Acceptance:* the reserved prefix is empty after a pass, asserted by listing it; a bucket that
  exists but denies the key fails `forbidden` rather than `not_found`; S3 reports
  `not_required` rather than `fail` for a deployment with raw-audio upload disabled, because
  spec 16.2 makes it conditionally required and reporting it red would train operators to
  ignore red.

**E5.5 Services status lifecycle.** `app/services/status.py`: the pure `roll_up(rows) -> str`,
`DEGRADE_AFTER_FAILURES = 2`, `apply_test_results`. Plus
`GET /deployments/{id}/services/status` and the periodic re-check sweep. `consecutive_failures`
is state rather than a heuristic because spec 16.5 says re-checks demote "on repeated failure",
and a per-service counter incremented on fail and zeroed on pass is the smallest thing that
makes "repeated" true; a time window would need a history table.
*Acceptance:* `roll_up` is table-driven over the full cross product of per-service statuses
including the S3-not-required case, transcribed from spec 16.5's four values with the reason
for each mapping in the test's own docstring; `deployment.services_status` equals `roll_up`
over that deployment's rows after **every** mutation path in the suite, asserted by a helper
that walks every deployment, so the denormalized column cannot silently diverge from its own
definition; one failed re-check does not demote a `verified` deployment, two consecutive ones
do, and a single success resets the counter — the map turning red on a transient blip is the
failure this threshold exists to prevent; the endpoint is `VIEW_SERVICES`-gated and its reason
never names a credential.

> **C2 — full gate.** The rig's measured cost against the section 5 ceiling recorded in the
> ledger.

**E5.6 Per-device broker credential minting.** `app/services/dynsec.py` — a **dedicated
short-lived client** speaking `$CONTROL/dynamic-security/v1`, never `MqttClientManager`, for
three independent reasons any one of which is sufficient: the manager's subscription set is
fixed before `start()` (D64) and `$CONTROL` is not in `deployment_subscriptions`; the tester
runs against candidate coordinates that are not yet stored, while the manager only knows
deployments that already have a row; and request/response correlation wants a fresh session so
a stale reply cannot be mistaken for this call's answer. It reuses `broker.py::tls_context`
rather than growing a second TLS rule, so D65's pinned-CA property holds identically.
Plus `app/services/credentials.py` (the `BrokerCredentialProvider` protocol and the two
implementations of fixed choice 6), the `broker_credential` table (`deployment_id`,
`aggregator_uuid`, `username`, `password_secret_name`, `state` ∈ `minted`/`revoked`,
timestamps), the mint and revoke endpoints, and `aggregator_acl_grants(slug, aggregator_uuid)`
extracted from `devbroker.acl_file_text`.
*Acceptance:* the dynsec role's ACL grants and the dev broker's ACL file lines are **rendered
from one list**, asserted by a test that reads `aggregator_acl_grants` and checks both
renderers against it, because two literal readings of spec 7.2's Direction column will
eventually disagree and the disagreement is a device that can publish its own desired config;
a credential minted through dynsec is immediately usable — the suite connects with it,
publishes to the aggregator's `reported` topic and is **refused** on its `desired` topic, with
the denial assertion paired with an authorized publish to the same topic because denial looks
like silence; deleting an aggregator revokes its dynsec client against a real broker and leaves
the row `revoked`, so a decommissioned Pi cannot reach the control plane.

> **Addendum PHASE5-4-01 (2026-08-12, ref project-changes #27):** `broker_credential.state` has
> **three** values, not the two this task lists: `minted`, `revoke_pending`, `revoked`. This
> task does not say what happens when the broker is unreachable during a delete, and it will be
> — decommissioning is exactly the work that happens while a site is offline. The owner's
> decision on 2026-08-12: the delete **proceeds** (204), the row lands in `revoke_pending`, and
> `credentials.drain_pending_revocations` retries on the worker's sweep until the broker
> confirms. A CHECK ties `revoked_at` to the `revoked` state so the extra value cannot make the
> timestamp ambiguous, and an unreachable broker (retried) is distinguished from a plugin that
> answered and REFUSED (raised and logged, because retrying a configuration fault hides it).
> The consequence for section 2's E3-owned budget is that **E5.7b registers a second sweep**
> for the retry beside the authorized `service_config_sweep`; both bodies are E5-owned and the
> E3-owned diff is registrations. DECISIONS D121 and D125 — the latter states the whole surface
> taken and what was deliberately not taken.

**E5.7a Projection and privileged write.** `app/services/projection.py::service_settings`, the
`allow_write_restricted` flag of fixed choice 3, the `changed_keys` fix above, and the services
save calling `build_change_plan` / `apply_change_plan` / publish. Extract `api/config.py`'s
`_publish_applied` into `publisher.py::publish_all` and have both callers use it rather than
copying it — two divergent publish loops with two error-swallowing policies is a bug that
surfaces months later as "some applies publish and some do not".
*Acceptance:* `set(service_settings(...)) ⊆ {e.key for e in CATALOG if e.write_restricted}`
asserted against `CATALOG` and not a copied list, and the twelve keys are covered exactly when
all five services are configured; `PUT /deployments/{id}/config/overrides` **still** 422s with
code `service_restricted` for every one of the twelve keys, so the flag's default is proven and
not assumed; a services save on a deployment with one Aggregator and thirty Listeners produces
**exactly one revision and thirty `no_op` device plans**, with the listeners' snapshots
byte-identical before and after; `GET .../config/effective` shows all twelve keys with
`source: deployment` and every secret redacted, while `effective_resolved` for the aggregator
resolves them; saving twice with identical input creates zero new revisions.

**E5.7b The two authorized E3 edits.** `MqttClientManager.refresh()` and the refresh loops in
both hosts; `service_config_sweep` on the existing sweep runner. Both are described in section
2; nothing else in `app/controlplane/` changes.
*Acceptance:* a deployment whose `mqtt` row is created **after** the worker started acquires a
live connection within one refresh interval, asserted end to end against a real broker with no
process restart; rotating a broker password reconnects that deployment's task and leaves other
deployments' connections unbroken, asserted by holding a subscription on a second deployment
across the rotation and seeing no gap; a cold-start Aggregator created after the services were
configured finds a retained desired message carrying all twelve keys waiting at the broker,
asserted by subscribing **after** the publish; `refresh()` is idempotent — called with
unchanged coordinates it cancels no task, asserted by identity on the task objects.

> **C3 — review pass, then full gate.** A review subagent reads the cross-epic diff before the
> gate runs.

**E5.8a Broker material, extracted not copied.** `app/brokerconfig.py` receives
`generate_tls_material` (parameterized with SAN hostnames and IPs, current values as defaults),
`password_hash`, `password_file_text` and `aggregator_acl_grants`; `devbroker.py` keeps its CLI
and calls them. Then the generated Mosquitto artifacts: TLS listener, `dynamic-security.json`
with a pre-created platform admin and the deployment-namespace role, `mosquitto.conf`. Also
amends the mTLS comment in `deploy/mosquitto/mosquitto.conf` per section 3.
*Acceptance:* `devbroker.py` contains no cryptographic or password-format code after the
extraction **and `backend/tests/test_dev_broker.py` passes unchanged** — the existing suite is
the regression proof that this was a move and not a rewrite, because "extract a helper" and
"rewrite a helper" look identical in a diff; the generated `mosquitto.conf` starts a real
broker through the `ephemeral_broker` machinery and its dynsec probe answers `available`.

**E5.8b The other four services and the compose assembly.** Influx init, Prometheus
`prometheus.yml` + `web_config.yml` + the remote-write receiver flag + retention + self-scrape
and node-exporter scrape configs, Grafana datasource and contact-point provisioning files,
optional MinIO with a created bucket. `docker-compose.yml` and the YAML configs are **built as
Python dicts and serialized with `yaml.safe_dump`**, never string-templated — a compose file
built from a dict is valid by construction, and `string.Template`'s `${name}` collides with
compose's own `${VAR}` interpolation. Static prose lives in `deploy/stack-templates/`.

> **Addendum PHASE5-4-07 (2026-08-13, ref project-changes #35):** the static prose lives in
> **`backend/app/services/stack_templates/`**, not `deploy/stack-templates/`. The location this
> document named is unshippable: the API image is built with `context: ../backend`, so `deploy/`
> is outside the build context and no `COPY` can reach it — `readme()` raised `FileNotFoundError`
> on `/srv/deploy/stack-templates/README.md` and the download endpoint 500'd in every
> containerized deployment, while the whole suite stayed green because tests run from the repo
> tree. Inside the package, `COPY app ./app` ships it by construction.
> `test_repo_layout.py::test_runtime_data_files_are_inside_the_image` now fails instead of an
> operator's download. Found by the first hand-run of `guide/e5-verification.md`, after the C5
> gate was green (DECISIONS D146).
*Acceptance:* `docker compose -f <generated> config` exits 0 for both the with-MinIO and
without-MinIO shapes; every port the README lists is a port the compose file publishes **and
vice versa**, asserted in both directions; no generated file contains a literal copied from
`deploy/mosquitto/mosquitto.conf` rather than rendered, asserted through E5.8a's shared-renderer
test.

**E5.9 Stack credential generation and registration.** Generate every credential, `SecretStore.put`
it, write the five rows at `status='untested'`, and store the broker's TLS material (CA PEM on
the row, private keys in SecretStore) — **all in one transaction, before any byte of the bundle
is rendered** (fixed choice 7).
*Acceptance:* a fault injected after credential generation and before commit leaves zero rows
and zero secrets; every credential comes from `secrets.token_urlsafe`-grade entropy and none is
derived from the deployment slug, id or name, asserted by generating two stacks for one
deployment and diffing every credential; regenerating rotates every credential and the old ones
are gone from SecretStore.

**E5.10 Stack bundle endpoints, and the rig keystone.** `POST /deployments/{id}/services/stack`,
`GET /deployments/{id}/services/stack/download`, and the README listing the required open ports,
`docker compose up -d`, and an explicit "treat this archive as a credential" warning.
This unit also **re-points the container rig at the generated bundle** (section 5): the rig
stops being a hand-written compose file at the same image set and becomes the bundle the
platform rendered.
*Acceptance:* two consecutive downloads are **byte-identical**, which is the determinism that
lets the platform keep no blob; the keystone — the generated bundle is written to a temp
directory, brought up with `docker compose up -d` on ephemeral ports, **all five E5.4 testers
run against it and pass, and `services_status` reaches `verified`** — which is spec 16.3's own
sentence executed rather than described; download is `MANAGE_SERVICES`-gated and audited with no
credential in the detail, the archive is streamed and never persisted server-side, and every
test that renders a bundle writes to `tmp_path` so `SECRET_PATTERNS` can never match the tree.

**E5.11 Rotation and regeneration flow.** `POST /deployments/{id}/services/stack/rotate`:
regenerate through E5.9, re-render, re-run the E5.3 tests, republish through E5.7a's path — so
rotation is a config revision, not a manual redistribution (spec 16.3).
*Acceptance:* rotation produces one new revision per Aggregator and zero for Listeners; the old
credentials are absent from SecretStore afterwards; the deployment passes through
`pending_verification` and returns to `verified` only after a real test pass, never
optimistically; **a rotation whose re-verification fails leaves `services_status` at `degraded`
and still publishes**, because the devices need the new credentials precisely because the old
ones stopped working — the intuitive order is wrong and the code carries a comment saying so.

> **Addendum PHASE5-4-02 (2026-08-12, ref project-changes #32):** "one new revision per
> Aggregator" needed a **thirteenth** projected key to be achievable, and this unit adds it.
> A desired snapshot carries secret MARKERS and never plaintext (spec 5.4, 8; D51, D126), and a
> marker is a SecretStore NAME — the identical string before and after a rotation. Measured:
> rotating every credential minted ZERO revisions, while rotating to a different hostname minted
> one per Aggregator and none per Listener, so the projection path was working and there was
> simply nothing to say. `services.credentials_generation` (catalog key,
> `write_restricted=SERVICE_ONBOARDING`; column `deployment.services_credentials_generation`,
> migration `d5f28c60a419`) is the non-secret counter that changes. It is E2-owned surface plus
> a migration, taken only on the owner's explicit decision (D134); `write_restricted` is what
> keeps it out of Listener snapshots and therefore keeps the "zero for Listeners" half true.

> **Addendum PHASE5-4-03 (2026-08-12, ref project-changes #31):** **this unit registers no
> sweep.** Spec 16.5's "periodic re-checks" are closed as *deliberately not built* on the
> owner's decision — timed polling reports a fact that was true minutes ago, and degradation
> should come from observed events only (an operator-run test, this re-verification, and for
> MQTT the control plane's connection and LWT). `status.py::services_recheck_sweep` survives as
> an on-demand bulk re-test an operator action invokes, never a scheduled job (D133).

> **C4 — full gate.**

**E5.12a Onboarding wizard UI: Path A.** Route
`inventory/deployments/:deploymentId/services`, `frontend/src/lib/services.ts` following
`src/lib/inventory.ts`'s shape (typed `ApiError`, one function per call, flat query keys), the
five per-service forms rendered from the schema rather than hardcoded field lists, and the
per-service result row with its own status, last-tested time, remedy and retry.

**E5.12b Path B, status, gating, and the E5 verification walkthrough.** The generated-stack
path, the S5 verify step, the rolled-up status display, and the spec 16.5 gate on bundle
generation. Plus `guide/e5-verification.md` (rule R1) and the amendments this epic forces on
`guide/e3-verification.md`, whose broker-row assertions this epic changes — in the same batch.
*Acceptance (both):* secrets are write-only in the UI as well as the API — a saved secret
renders as the keep sentinel and the field is never populated from a response, asserted in the
component test by checking the input's value is empty after a load; `services_status` is **not**
device status, uses its own vocabulary and does not render through `StatusChip` (D40 and the DES
three-channel rule); a viewer and a field tech see status and no Save, Test or Generate; every
new class lands in an `app.css` section and every new token is an additive `tokens.ext.css`
extension with its dark value (D21), with `tokens.test.ts` green; the walkthrough runs start to
finish by hand against a clean stack, both paths.

> **C5 — full gate, then the PR.**

## 5. Definition of done

An operator either enters existing service credentials and sees every test pass, or downloads
a generated stack, runs it, and verifies it; a provisioned Aggregator receives all service
configuration over MQTT on first connect; `services_status` reflects reality and degrades on
failure. Plus:

- **No service credential exists in any API response, log line, fixture, or committed file** —
  asserted, not asserted-to. Three assertions per credential-bearing path: absent from
  responses (scanning for literal values, reusing `test_repo_layout.SECRET_PATTERNS`), absent
  from `caplog` across the E5 suite, absent from the working tree.
- The stack bundle legitimately contains a private key and five passwords. That makes every
  "is this a leak" judgement harder here than anywhere else in the codebase, which is why the
  three assertions above are a definition-of-done item and not a nice-to-have.
- The twelve restricted keys are writable by this phase and by nothing else, with the
  operator-facing 422 proven independently.
- Exactly two E3-owned modules changed, both named in section 2, both recorded.
- **Gate time.** Warm gate before this epic is the baseline recorded by E5.0. The container
  rig is designed to add little: one session-scoped rig on **one shared xdist group** so it
  starts once per gate rather than once per module per worker; the rig **is** the generated
  stack from E5.10 onward, so the keystone costs no second bring-up; containers start in
  parallel, so the ready-wait is max (Grafana, ~10-15s) and not the sum; the container tests
  are a small contract suite per service with fakes carrying the volume; and CI pre-pulls the
  images in a step parallel to dependency install so the ~1.4 GB cold cost is off the test
  job's clock. **The measured warm gate is recorded in the ledger at every checkpoint. If it
  exceeds ~300s, container-test scope is cut rather than the number accepted** — that trade is
  pre-authorized so an unattended session does not have to stop and ask.
- The universal definition of done (handbook section 4): CI green, every mutation audited,
  every endpoint RBAC-gated, `INTERFACES.md` and `DECISIONS.md` updated, the demo fixture still
  seeds, the compose stack still starts clean.

> **Addendum PHASE5-4-04 (2026-08-12, ref project-changes #30):** the gate-time design above
> assumed the fixtures exercise what the platform ships, and they did not. `conftest.py` and
> `deploy/docker-compose.yml` used the floating `eclipse-mosquitto:2` tag while
> `stack.IMAGES` pins 2.0.20, and Docker Hub has since moved `:2` to 2.1.x — two versions that
> read `dynamic-security.json` passwords differently (D132). Every dynsec test in the suite
> passed against a broker no operator would ever run. Both now read the pin from
> `stack.IMAGES` so there is one version fact; **a pinned artifact tested against a floating
> tag proves nothing about what ships.** E0/E3-owned files, taken on the owner's explicit
> authorization.

> **Addendum PHASE5-4-05 (2026-08-12, ref project-changes #33):** "CI green" hid a process
> that logged almost nothing. Uvicorn attaches handlers to its own `uvicorn.*` loggers and
> leaves the ROOT logger bare, so every `app.*` INFO line in the API process was dropped
> (D127, now closed by D136) — while `runner.py::main`'s docstring asserted the opposite,
> which is why it survived three epics. `app/middleware.py::install_root_handler` is now
> called by both hosts, so "which processes log" is a single fact. E0-owned surface, taken by
> this epic on the owner's explicit authorization.

## 6. Handoff artifacts

- `docs/INTERFACES.md` gains an **Owned by E5** section: the widened `deployment_service` with
  its conditional CHECK and the meaning of `config` / `secret_names`; the per-service and
  rolled-up status vocabularies as **two distinct vocabularies**, with `roll_up` named as the
  only writer of `deployment.services_status`; the services, test, status, credential and stack
  endpoints; `BrokerCredentialProvider` marked **E4.6 CONSUMES THIS**; the dynsec probe's three
  verdicts; and the ownership sentence for `app/services/clients/` — *the only place a
  deployment service is dialled from; E7 extends these modules and does not create parallel
  ones*. It also **amends** two E3 sentences: the `MqttClientManager` "coordinates load once"
  contract, and the `deployment_service` section's "CHECK-constrained to `('mqtt')` for now".
- `docs/DECISIONS.md`: the nine fixed choices with their rationale and their open remainders,
  the two E3 edits, the E2 `changed_keys` fix, the gate-cadence deviation, and every deviation
  found while building. Spec 17 item 14 is **closed** by fixed choice 4 and must say so; item
  13 stays open.
- `guide/e5-verification.md`, and the amendments this epic forces on `guide/e3-verification.md`.
- `project_planning/e5-progress-ledger.md`, kept current per unit — the file a session joining
  mid-epic reads first, carrying the measured gate durations.
- `deploy/stack-templates/`, and `deploy/.env.example` plus the environment-variable table in
  `INTERFACES.md`'s **Owned by E0** section gaining `EOE_COORDINATES_REFRESH_SECONDS` and the
  services re-check interval — by name, never by value. That table is E0's and these are
  additive rows.

> **Addendum PHASE5-6-01 (2026-08-13, ref project-changes #35):** the first artifact in the line
> above ships as **`backend/app/services/stack_templates/`**. `deploy/` is outside the API
> image's build context, so a template there is not in the image at all and the download endpoint
> 500s in any containerized deployment — see addendum PHASE5-4-07 and DECISIONS D146. The rest of
> the line is unchanged.
