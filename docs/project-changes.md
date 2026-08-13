# Project Changes

Numbered, reverse-chronological, append-only log of changes to scope, sequencing, task
definitions, or acceptance criteria relative to the planning documents (rule R1,
`.claude/rules/project-rules.json`). Every entry names an addendum that exists in the
referenced planning document; an entry with no addendum is incomplete.

## #34 (2026-08-13): A third E3-owned edit is taken, to fix a stranded broker connection

- **What changed:** `app/controlplane/broker.py` gains `_open_client` and `_connection_loop`
  uses it, so a cancellation inside aiomqtt's `__aenter__` can no longer strand a connected
  client off the `AsyncExitStack`. Plus a forced regression test in `test_mqtt_manager.py`.
- **Why:** `stop()` cancelling mid-connect left a CONNECTED client with a live socket and a
  running `_misc_loop` that nothing owned and nothing could close — a per-reconnect leak of
  sockets and tasks in a process meant to run for months. Found by E3's own
  `test_shutdown_leaves_no_running_tasks` under a loaded gate, and reproduced deterministically
  before the fix was written. Details in D138.
- **Whose scope this crosses:** the phase document authorizes **two** discretionary E3-owned
  edits, both in E5.7b, and says a third is a stop-and-ask. It was asked and answered:
  **taken on the owner's explicit authorization**, 2026-08-13, over the declined alternative of
  recording the defect and deferring it to an E3 batch — which would have left a known flake
  able to redden any later gate, E5.12's included.
- **Affects:** project_planning/phase-5-deployment-services.md section 2 ("The E3-owned edits
  this phase is authorized to make"); docs/DECISIONS.md D138, extending D94
- **Addendum:** PHASE5-4-06

## #33 (2026-08-12): The API process gets a root log handler, and an E0-owned docstring that
was wrong is corrected

- **What changed:** `app/middleware.py` gains `install_root_handler`, called by both
  `create_app` and `runner.py::main`. `runner.py`'s docstring, which claimed "under uvicorn the
  server installs the handlers", is corrected.
- **Why:** uvicorn attaches handlers to its own `uvicorn.*` loggers and leaves the ROOT logger
  bare, so Python's last-resort handler passed WARNING and above and silently dropped every
  `app.*` INFO line in the API process — broker connected, coordinates refreshed, publish
  outcomes. Found by C3's manual walkthrough, which tried to prove `refresh()` had connected by
  grepping the log and got nothing. The wrong docstring is why it survived three epics.
- **Whose scope this crosses:** `app/middleware.py` is E0-owned. **Taken on the owner's
  explicit authorization**, 2026-08-12, as an E0-owned fix made by E5; it closes D127, which
  had been carried as a stop-and-ask.
- **Affects:** project_planning/phase-5-deployment-services.md section 5 (definition of done);
  docs/DECISIONS.md D136, closing D127
- **Addendum:** PHASE5-4-05

## #32 (2026-08-12): A thirteenth device-facing config key, so a rotation is visible to devices

- **What changed:** the catalog gains `services.credentials_generation` (int,
  `write_restricted=SERVICE_ONBOARDING`) and `deployment` gains a
  `services_credentials_generation` column (migration `d5f28c60a419`). E5's projection, which
  the phase document describes as "the twelve device-facing keys", now writes thirteen.
- **Why:** a desired snapshot carries secret MARKERS and never plaintext (spec 5.4, 8; D51,
  D126), and a marker is a SecretStore name — identical before and after a rotation. Measured:
  rotating every credential minted ZERO revisions, while rotating to a different hostname minted
  one per Aggregator and none per Listener. So E5.11's acceptance ("one new revision per
  Aggregator") and spec 16.3's "rotation is a config revision, not a manual redistribution"
  were unachievable without one non-secret thing that changes.
- **Whose scope this crosses:** the catalog is E2-owned and this is a migration, so it is out of
  phase 5's scope under rule R2. **Taken only on the owner's explicit decision**, 2026-08-12,
  over the declined alternatives of accepting zero revisions and amending the acceptance, or
  deferring the question to E7.
- **Also changes the spec, which the first draft of this entry missed.** The catalog is spec
  5.3's table and the suite asserts them key for key, so a thirteenth restricted key is a spec
  change and not only a phase-document one: `echoes-of-earth-platform-spec-v1.1.md` gains a
  thirty-eighth row and addendum **SPEC-5-01**. The frozen wire checksum in the merge-engine
  suite moves with it — recorded separately in D137, because re-freezing a golden digest is
  never routine.
- **Affects:** project_planning/phase-5-deployment-services.md section 4 (E5.7a, E5.11);
  project_planning/echoes-of-earth-platform-spec-v1.1.md section 5.3;
  docs/DECISIONS.md D134, D137
- **Addendum:** PHASE5-4-02

## #31 (2026-08-12): Spec 16.5's periodic service re-checks are closed as deliberately not built

- **What changed:** the platform runs no timed re-verification of deployment services. E5.11
  registers no sweep, and spec 16.5's "periodic re-checks" item is closed as *not built* rather
  than carried as outstanding work. `status.py::services_recheck_sweep` survives as an
  on-demand bulk re-test invoked by an operator action, and its docstring now says so.
- **Why:** the owner's decision, asked directly and answered directly on 2026-08-12 — timed
  polling reports a fact that was true minutes ago, and the platform should fail fast and
  loudly off real liveness instead. Degradation comes from observed events only: an
  operator-run test, a rotation's re-verification, and for MQTT the control plane's own
  connection and LWT.
- **Affects:** project_planning/phase-5-deployment-services.md section 4 (E5.11); the E5.5
  notes and the INTERFACES entry that both expected E5.7b to register the sweep
- **Addendum:** PHASE5-4-03

## #30 (2026-08-12): The broker fixtures are pinned to the version the generated stack ships

- **What changed:** `backend/tests/conftest.py` and `deploy/docker-compose.yml` stop using the
  floating `eclipse-mosquitto:2` tag. The fixture reads `app.services.stack.IMAGES` so there is
  one pin; the dev compose stack names the same version.
- **Why:** Docker Hub moved `:2` to 2.1.x, and 2.0 and 2.1 read `dynamic-security.json`
  passwords differently (D132). Every dynsec test in the suite passed against 2.1.2 while the
  2.0.20 the platform pins refused every login — a pinned artifact tested against a floating tag
  proves nothing about what ships. Found by E5.10's keystone.
- **Whose scope this crosses:** `conftest.py` is E0-owned test infrastructure and
  `deploy/docker-compose.yml` is E0/E3-owned. **Taken on the owner's explicit authorization**,
  2026-08-12.
- **Affects:** project_planning/phase-5-deployment-services.md section 5 (gate-time design)
- **Addendum:** PHASE5-4-04

## #29 (2026-08-12): An unnumbered infrastructure batch lands before C4, and phase 5's
"additive-only in `conftest.py`" rule is broken deliberately

- **What changed:** a batch with no task number — **INFRA.1** — lands at the head of
  `e5-batch-1` before C4 begins. It replaces the per-module Postgres container with a
  machine-wide warm server handing out template clones, and moves every test container's
  writable state to tmpfs. DECISIONS D128 and D129 carry the design and its costs.
- **Why it is not part of C4:** it is E0-owned test infrastructure, not E5 scope, and folding it
  into a numbered E5 unit would make that unit's diff unreviewable and its gate measurement
  meaningless.
- **Why it is not additive:** phase-5 section 2's process choices bind this epic to being
  "additive-only in `conftest.py`" so the parallel SIM epic can merge cleanly. INFRA.1 REWRITES
  `ephemeral_postgres` and cannot honour that. The owner accepted the merge cost knowingly; the
  precedent runs the other way too, since commit `167aa6e` imported SIM's concurrency fix into
  this worktree verbatim, and SIM adopts this one the same way.
- **Why it happened now rather than after C4:** phase-5 section 5 caps the warm gate at ~300s
  and the C3 checkpoint measured 299.16s, so E5.8b's compose-config tests and E5.10's keystone
  bring-up had no margin at all. Section 5 pre-authorises cutting container-test scope to hold
  the number — but E5.10's keystone IS its acceptance, and cutting it would gut the unit. The
  owner chose to remove the cost instead of the tests, and additionally directed that the suite
  stop wearing the machine's SSD, which the baseline measurement put at 4.05 GB per gate run.
- **Who approved:** the owner, on 2026-08-12, choosing the infrastructure batch before C4 over
  both "cut scope to hold 300s" and "measure C4 first, then raise the ceiling".
- **Affects:** project_planning/phase-5-deployment-services.md section 2 (process choices) and
  section 5 (the gate-time ceiling), project_planning/e5-progress-ledger.md
- **Addendum:** PHASE5-2-03

## #28 (2026-08-12): `allow_write_restricted` is four signatures, and the object-storage
question is closed without a catalog toggle

- **What changed, part one.** Phase-5 fixed choice 3 says the flag is threaded through
  "three signatures". It is four: `apply_change_plan` calls `put_overrides`, so without the flag
  reaching it the plan a caller was handed could not be executed. Same default, same meaning.
  The flag also carries the "regenerated wholesale, never merged" behaviour the same fixed
  choice requires, which the document implies but does not state. DECISIONS D122.
- **What changed, part two.** The E5 ledger's "OPEN QUESTION for the owner" -- what makes object
  storage `not_required` when spec 16.2 names a raw-audio toggle that does not exist -- is
  **answered and closed**: E5.4e's reading (both credentials absent) stands, the platform
  supports raw audio only for now, and **no `upload.raw_audio_enabled` catalog key is added**.
  That would have been an E2-owned catalog change plus a migration, out of E5's scope under rule
  R2. DECISIONS D123.
- **Why:** the first is a document undercounting a signature, recorded because a document that
  says "exactly three" while the tree has four is worse than no document. The second is a spec
  gap the owner resolved by declining to widen the catalog for a flag with one consumer.
- **Who approved:** the owner, on 2026-08-12, choosing the existing reading over both an
  unconditional requirement and an explicit toggle.
- **Affects:** project_planning/phase-5-deployment-services.md section 2 (fixed choice 3),
  project_planning/e5-progress-ledger.md (the open question)
- **Addendum:** PHASE5-2-02

## #27 (2026-08-12): A broker credential has three states, not two, and revoking one never
blocks a device delete

- **What changed:** `broker_credential.state` is `minted` / `revoke_pending` / `revoked`, where
  the E5.6 task description implies two. Deleting an Aggregator whose broker is unreachable
  still returns 204; the row lands in `revoke_pending` and
  `credentials.drain_pending_revocations` retries on the worker's sweep until the broker
  confirms. A CHECK constraint ties `revoked_at` to the `revoked` state so the third value
  cannot make the timestamp ambiguous.
- **Why:** E5.6's acceptance ("deleting an aggregator revokes its dynsec client against a real
  broker and leaves the row `revoked`") does not say what happens when the broker is down, and
  it will be -- decommissioning is exactly the work that happens while a site is offline. Both
  two-state answers are bad: refusing the delete lets one deployment's outage block inventory
  work, and letting it pass silently strands a live credential on somebody's broker forever with
  no record that it exists.
- **The consequence for the E3-owned surface, stated rather than buried:** the retry needs a
  loop, so E5.7b registers a second sweep (`broker-credential`) beside the authorized
  `service_config_sweep`. Both have E5-owned bodies; the E3-owned diff is registrations.
  DECISIONS D125 states the whole surface taken, including what was NOT taken.
- **Who approved:** the owner, on 2026-08-12, choosing the retry over a 503 that refuses the
  delete.
- **Affects:** project_planning/phase-5-deployment-services.md section 4 (E5.6), section 2
  ("The E3-owned edits this phase is authorized to make")
- **Addendum:** PHASE5-4-01

## #26 (2026-08-11): `BrokerCredentialProvider` is defined by E5 and consumed by E4

- **What changed:** `project_planning/phase-4-provisioning.md` §2 fixed choice 1 has E4.6
  shipping the `BrokerCredentialProvider` seam, with "E5.6's entire job is to add a dynsec
  provider and flip the default". That ordering assumed E4 landed first. **It did not** — E4
  has not been started, and E5 is being built now. So the interface dependency reverses:
  **E5.6 defines the protocol** (`mint`, `revoke`, `state`) in
  `backend/app/services/credentials.py` and ships both `DynsecCredentialProvider` and
  `DevBrokerCredentialProvider`.
- **E4.6's remaining work is unchanged in substance** — choose a provider and flip
  `EOE_BOOTSTRAP_CREDENTIALS`. It imports the protocol instead of declaring it, and writes no
  dev provider of its own, because `DevBrokerCredentialProvider` is exactly the one phase 4
  described.
- **A second consequence for E4.6:** the degraded verified-broker predicate phase 4 specified
  ("a `deployment_service` row with `service_key='mqtt'` exists", carrying a
  `# E5.5 replaces this predicate` marker) is **no longer needed**, because E5.5 ships
  `deployment.services_status`. E4.6 gates on `services_status == 'verified'` directly and that
  marker should never be written.
- **Why not wait for E4.** E5.6 needs to mint credentials regardless: the generated stack
  pre-creates the platform account and the deployment-namespace role, and per-device
  credentials must exist before hardware ships. Building it without a named interface, for E4
  to wrap later, would be the same work with the seam discovered afterwards rather than
  designed.
- **Who approved:** the owner, on 2026-08-11, at plan approval.
- **Affects:** project_planning/phase-4-provisioning.md §2 (fixed choice 1)
- **Addendum:** PHASE4-2-01

## #25 (2026-08-11): dynsec is required for v1, closing spec 17 item 14

- **What changed:** spec 17 item 14 asked whether v1 should require Mosquitto's dynamic
  security plugin for platform-managed brokers instead of supporting spec 16.4's
  manual-install fallback. **The owner chose to require it**, and the item is now closed.
  Spec 16.4's sentence about generating a credential pair for the operator to install by hand,
  with the bundle held until they confirm, is superseded.
- **What it deletes from task E5.6:** a second `BrokerCredentialProvider` implementation, a
  `pending_manual_install` state and its confirm endpoint, a held-bundle predicate E4 would
  have had to consult, a wizard branch, and the class of deployment that is half-provisioned
  because someone meant to paste an ACL into a broker host and did not.
- **What it costs, stated rather than hidden:** an operator running a Mosquitto without
  `dynamic_security.so` must enable it before their deployment can be verified. The MQTT
  tester's failure message names the plugin and what to add to `mosquitto.conf`.
- **The consequence that had to be built:** the dynsec verdict is part of broker verification,
  so `absent` and `denied` both keep `services_status` off `verified` — which by spec 16.5
  blocks provisioning-bundle generation, since the bootstrap block would embed credentials
  that do not exist. The probe reports three verdicts rather than two, because "plugin absent"
  and "your platform account is not an admin" have different remedies.
- **What did NOT change:** item 13 (Chameleon Cloud VM auto-provisioning) stays open and out
  of scope; Path B still ends at a downloadable bundle.
- **Who approved:** the owner, on 2026-08-11, at plan approval.
- **Affects:** project_planning/echoes-of-earth-platform-spec-v1.1.md §17 (item 14)
- **Addendum:** SPEC-17-01

## #24 (2026-08-11): The E5 phase document, and the units and permissions it adds

- **What changed:** epic E5 gains its phase document,
  `project_planning/phase-5-deployment-services.md`, written to the project plan §5 structure
  and now the binding scope for the epic. Two further changes it makes are recorded separately
  as #25 (dynsec required) and #26 (the `BrokerCredentialProvider` reversal); this entry covers
  the rest.
- **A thirteenth unit, E5.0.** The project plan lists twelve E5 tasks and no phase-document
  task. E5.0 is the document, `project_planning/e5-progress-ledger.md`, and these records.
  Same shape as E4.0 and SIM.0. Five of the twelve tasks also split into lettered units for
  gating (E5.4 into a-e, E5.7 into a-b, E5.8 into a-b, E5.12 into a-b), giving eighteen in all.
- **Two new permissions**, `MANAGE_SERVICES` and `VIEW_SERVICES`, extending the test-critical
  RBAC map and its frontend mirror. Neither the project plan nor the spec names them; E0.7
  defined no services verb, and reusing `MANAGE_CONFIG` would hand a Field Tech write access
  to a deployment's Influx admin token, S3 secret key and broker password.
- **Cross-epic edits are authorized in advance**, all recorded in `DECISIONS.md`: two E3-owned
  and discretionary (`MqttClientManager.refresh()` and a `service_config_sweep` on the existing
  sweep runner), both confined to task E5.7b so the whole discretionary surface is one diff;
  one E2-owned (`DevicePlan.changed_keys` computed from stripped snapshots, which stops one
  services save minting a revision per Listener); and one E3-owned but forced rather than
  chosen, in E5.1 (see D109). Any further cross-epic edit is a stop-and-ask.
- **Process, for this epic only:** one branch (`e5-batch-1`) and one PR rather than phase 4's
  per-batch shape, and the full gate at five checkpoints rather than after every numbered
  unit — with the compensating rule that nothing reaches the remote without a full green gate.
  Recorded as a deviation in `DECISIONS.md` (D107).
- **What did NOT change:** the twelve tasks, their order, or the epic's definition of done.
  Spec 16's two paths, five testers, and status lifecycle are implemented as written.
- **Who approved:** the owner, on 2026-08-11, at plan approval, choosing the new permissions,
  the hybrid container/fake test strategy, the single branch, and the checkpoint gate cadence.
- **Affects:** project_planning/echoes-of-earth-project-plan.md §3 (epic E5)
- **Addendum:** PLAN-3-03

## #23 (2026-08-11): The SIM phase document, and the simulation scale it fixes

- **What changed:** epic SIM gains its phase document,
  `project_planning/phase-sim-simulation-harness.md`, written to the project plan §5 structure
  and now the binding scope for the epic. Three things in it are choices the planning documents
  left open or contradictory, and are fixed here rather than per session.
- **The scale target, which was ambiguous.** Spec 14.2 says "around 30 listeners across at least
  a few concurrent aggregators, plus up to around 20 or more mock aggregators"; the project plan
  §3 reads the same target as "20 or more mock aggregators, around 30 listeners each". Those are
  600 Listeners and roughly 30. **The project plan's reading is binding: 20 × 30 = 600.** It is
  the demanding reading, spec 14.2 says the target MUST run comfortably on one host, and a
  harness built for the smaller number could not be stretched to the larger one later without a
  redesign. CI runs 2 × 3; every count is a parameter.
- **Two additions the plan's task list does not name.** SIM.4 ships a `sim` compose service
  behind an optional profile (off by default), which extends the `COMPOSE_SERVICES` pin in
  `test_repo_layout.py` — deliberately and with its INTERFACES entry, exactly as E3.7 did for
  `worker`. SIM.5 adds **two** gate stages rather than one, `sim-quality` and `sim-protocol`,
  mirroring the backend's quality/tests split; `sim-protocol` is the name INTERFACES.md already
  reserved for this epic.
- **What did NOT change:** the five tasks, their order, or the epic's definition of done. SIM
  remains a client of the platform with no privileged access, and E8.6 still owns the full-scale
  run against the complete platform — this epic ships the runner and the written procedure.
- **Who approved:** the owner, on 2026-08-11, at plan approval, choosing the 20 × 30 reading,
  the standalone `/sim` uv project over a workspace, REST-based provisioning over direct
  database seeding, TOML scenario files, both CI stages, and the compose profile.
- **Affects:** project_planning/echoes-of-earth-project-plan.md §3 (epic SIM)
- **Addendum:** PLAN-3-02

## #22 (2026-08-10): E3.7 ships the operator publish route and the worker container

- **What changed:** three additions inside task E3.7 that the phase document's task text
  does not name. (1) `POST /revisions/{revision_id}/publish` — the operator action the task
  requires ("re-publish happens on operator action") but that no E3 task claimed; it calls
  E3.4's `publish_revision` and adds no publish logic of its own (DECISIONS D82). (2) A
  `worker` service in the dev compose stack running `python -m app.controlplane.runner`,
  plus an outbound publish-only broker connection in the API process, because the worker
  and the publisher now live in two processes (D59, D86). (3) The D8 error vocabulary gains
  `service_unavailable` for a broker outage (D83).
- **Why:** without the route, the phase's own acceptance step "operator re-publish" would
  exist only inside a test, and drift — which the worker is forbidden to repair itself,
  `auto_reconcile` being inert — could not be repaired through the platform at all.
- **What did NOT change:** the scope of the worker itself, the sweeps, or the transitions.
  E3.13 still owns wiring E2's bulk apply to publication and flipping
  `EOE_PUBLISH_ENABLED` on; this route is the single-revision action beside it, and both
  take one code path.
- **Who approved:** the owner, on 2026-08-10, choosing the route over deferring it, with
  the instruction that anything provisional be marked as such. Marked accordingly:
  `deployment.auto_reconcile` (stored, inert, pending spec 17 item 3), the drift sweep's
  `desired_changed` signal (observation only — spec 6.2 has no state for it), and
  `EOE_WORKER_IN_API` (off by default).
- **Effect on scope:** none of the thirteen E3 tasks moves; E3.7 is larger by one route and
  one compose service.
- **Affects:** project_planning/phase-3-control-plane.md §4 (task E3.7)
- **Addendum:** PHASE3-4-02

## #21 (2026-08-10): Dev host ports moved into the 1xxxx range

- **What changed:** the published HOST ports of the dev compose stack move from
  8000/5173/5432/6379 to **18000/15173/15432/16379**, and E3.1's new broker publishes
  **18883** rather than 8883. Phase-0 section 2 fixes the old numbers, so this amends it.
- **What did NOT change:** the container side. Every service still listens on its standard
  port inside the network (8000/5173/5432/6379/8883), so no image, no uvicorn or vite
  argument, no `mosquitto.conf` listener and no service-to-service URL moved — only the
  host mapping and the host-facing URLs (`EOE_CORS_ORIGINS`, `VITE_API_BASE_URL`, the
  verifier's default `--api`, the QA script's probes, and the guides).
- **Why:** the standard ports collide with other services commonly running on a developer
  machine, and the collision is not cosmetic — it makes the gate impossible to pass, since
  `test_compose_stack` and `test_verify_tool` bind them for real (rule R0 admits no
  skipping). Owner instruction, 2026-08-10.
- **Enforcement:** `FIXED_PORTS` in `backend/tests/test_repo_layout.py` remains the single
  enforced list and now carries HOST:CONTAINER pairs; `docs/INTERFACES.md` records the new
  host ports beside a pointer to it.
- **Affects:** project_planning/phase-0-foundations.md §2 (ports)
- **Addendum:** PHASE0-2-02

## #20 (2026-08-10): E3 sequencing — contracts module lands at E3.1, state machine before publish

- **What changed:** two ordering changes inside epic E3, both within their own batch.
  (1) `backend/app/contracts/mqtt.py` is created by **E3.1** with the spec 7.2 topic
  builders instead of arriving whole at E3.3; E3.3 adds the spec 7.3 payload models to it.
  (2) Batch 2 runs **E3.6 (state machine) before E3.4 (publish) and E3.5 (consumer)**,
  instead of the phase document's 3.4 → 3.5 → 3.6 order.
- **Why:** (1) E3.1's broker ACL grants ARE topic strings; generating them from literals
  and refactoring two tasks later would put the topic namespace in two places, which the
  "single contracts module" fixed choice exists to prevent (DECISIONS D62). (2) Both the
  publisher and the consumer transition revision state, so the module that owns transitions
  must exist before either calls it rather than open-code a state write.
- **Who approved:** the owner approved the batch-2 reorder with the E3 plan on 2026-08-10;
  the handbook (section 2) allows a session to propose a task order.
- **Effect on scope:** none. All thirteen E3 tasks still ship, each with its own gate.
- **Affects:** project_planning/phase-3-control-plane.md §4 (tasks E3.1, E3.3-E3.6)
- **Addendum:** PHASE3-4-01

## #19 (2026-08-10): E3's E2-coordination contingency is moot; E3.13 is in scope

- **What changed:** the phase-3 document was written expecting E3 to run in parallel with
  E2 and hedges accordingly — agree the `config_revision` shape with the E2 session, fall
  back to consuming revision snapshots if the merge engine has not landed, and treat E3.13
  as conditional on E2 merging. E1 and E2 both completed before this phase started (gates
  20-38, PRs #9-#16), so none of those contingencies apply.
- **Why:** the epics ran in sequence, not in parallel. `config_revision` arrives exactly as
  E2 defined it (D55); `app/config/service.py::effective_resolved` is available to the
  publisher and the drift re-compare from the day E3.7 lands; and E3.13 — wiring apply to
  `publish_revision` and flipping `EOE_PUBLISH_ENABLED` on — is ordinary in-scope work for
  this phase rather than a deferred follow-up.
- **Owner decisions taken at plan approval (2026-08-10):** DECISIONS D59 (worker topology
  and the Postgres LISTEN/NOTIFY websocket bus), D60 (E3 lifts D40 on real reported state),
  D61 (`EOE_PUBLISH_ENABLED` defaults on at E3.13).
- **Effect on scope:** none removed; E3.13 becomes unconditional.
- **Affects:** project_planning/phase-3-control-plane.md §2 (prerequisites and coordination)
- **Addendum:** PHASE3-2-01

## #17 (2026-08-04): E2.2's level rule inverted to match spec 5.3 — at-or-above, never below

- **What changed:** task E2.2 says "writing a key below its allowed level is permitted
  per spec 5.3's inheritance note". The shipped validator enforces the OPPOSITE: a key
  may be overridden at its lowest level or at any ancestor level, never below
  (`network.wifi_ssid`, lowest level Pod, is settable at organization/deployment/pod and
  rejected at aggregator/listener; `lowest_level='any'` is settable everywhere).
- **Why:** the phase-doc sentence misreads the spec. Spec 5.3's actual inheritance note
  says "inheritance permits setting most keys **higher**" than their lowest level, and
  spec 5.1's rationale argues directly against below-level writes (network keys resolve
  at Pod because every Listener in the pod shares one network — a per-listener WiFi
  password contradicts the model). Authority order: spec beats phase doc. Owner decided
  2026-08-04 during E2 planning (the same resolution shape as #13's org-DELETE).
- **Affects:** project_planning/phase-2-configuration-model.md §4 (task E2.2)
- **Addendum:** PHASE2-4-01

## #18 (2026-08-04): E2.6 concretized — write-at-level, per-device draft revisions, revisions routes assigned

- **What changed:** task E2.6's sketch left three things open that the shipped design
  fixes: (a) apply gains an explicit **write-at-level** control — `level="target"`
  (default) writes onto each matched entity, a named level writes once at the single
  common ancestor or 422s on a split; (b) revisions are **per-device only**
  (aggregator/listener, per spec 6.1/7.2's topic addressees) with marker-bearing
  snapshots composed per spec 5.4, and the phase doc's "target entity type and id"
  sketch is narrowed accordingly; (c) the spec-13 **revisions read routes**
  (GET /{device}/{id}/revisions, GET /revisions/{id}), which no phase-2 task claimed,
  land in E2.6 because E2.8's acceptance ("sees draft revisions listed") needs them.
  Apply stops at draft unconditionally; `EOE_PUBLISH_ENABLED` exists, defaults off, and
  is only reported until E3's publisher consumes it.
- **Why:** spec 6.1 defines desired config per Aggregator/Listener; a selection is not
  a topic addressee. The common-ancestor rule keeps a bulk edit one auditable write
  instead of N copies. Owner decisions of 2026-08-04 (the E2 plan).
- **Affects:** project_planning/phase-2-configuration-model.md §4 (task E2.6)
- **Addendum:** PHASE2-4-02

## #16 (2026-08-02): Overview ships a minimal real roll-up ahead of E6/E7

- **What changed:** the Overview page ("/") is rebuilt with the V2·S1 layout carrying
  ONLY E1-owned data — a serif hero of **listeners registered** (honest label, not
  "online"), a real meta line, one card per deployment with real pod/listener counts —
  while every slot whose data belongs to a later epic stays an honest EmptyState naming
  it (attention feed → E3/E7; status/services lines → E3/E5; no telemetry volume). The
  page title becomes "Organization overview" (V2·S1's title; v2 wins on values), which
  retitles one shell.test row and two auth.test assertions (recorded in D42). E1's task
  list does not include Overview; building the E1-ownable half now mirrors how DES.7
  shipped page skeletons ahead of their epics (#9's precedent), with the owner's explicit
  decision (2026-08-02).
- **Guard:** the no-fabricated-status rule (D40) is asserted by test on this page.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-04

## #15 (2026-08-02): E1.5's "raise a condition" implemented as return-based resolutions

- **What changed:** task E1.5 says the service should "raise a `duplicate_identity`
  condition" / "raise `provisioning_required`". The shipped design returns an
  `IdentityResolution` with an outcome enum instead of raising for expected conditions,
  writes the alert rows the task specifies, and offers `require_known_aggregator` as a
  raising variant. Recorded so the wording difference is a documented choice, not drift:
  E3.5's consumer loop handles every outcome uniformly, and exception control flow for
  expected message states would push try/except into every ingest path.
- **Why:** the task's own acceptance tests are outcome-shaped (clean match, name
  conflict, MAC conflict, unknown aggregator — all verified); the "condition" language
  named the states, not the mechanism.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-03

## #14 (2026-08-02): POST /organizations clamped to a single organization

- **What changed:** creating a second organization returns 409 `conflict` while v1 runs
  single-org. Spec 12.1 fixes v1 as one Organization whose scoping flows through joins;
  an unclamped POST would let two orgs exist with no scoping story and would falsify the
  global-uniqueness-equals-within-org reasoning D32 records for `aggregator_uuid`.
  Multi-org later removes the clamp and relaxes that constraint together (D32/D34
  cross-reference).
- **Why it is a plan change:** E1.2's task text implies plain POST semantics for all five
  entities; the clamp narrows one of them deliberately.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-02

## #13 (2026-08-02): Organizations get no DELETE endpoint

- **What changed:** the E1.2 surface ships `GET/POST` collection and `GET/PATCH` item
  routes for `/organizations` — no DELETE. Task E1.2's wording ("`GET/PATCH/DELETE` items
  for all five entities") conflicted with spec 13, which lists no DELETE for
  organizations, consistent with the single-org v1 of spec 12.1. The spec is first in the
  authority order; the owner confirmed the resolution (2026-08-02) before implementation.
- **Affects:** project_planning/phase-1-hierarchy-inventory.md
- **Addendum:** PHASE1-4-01

## #12 (2026-08-01): Records hygiene batch — the DES-batch paper trail brought current

- **What changed:** No product code. A records-and-test-hygiene pass closing the gaps a
  post-DES review of `23eff5d..f93f061` found: (1) `project_planning/phase-0-foundations.md`
  gains PHASE0-4-05 (D25's shell replacement, previously recorded everywhere except the phase
  document) and PHASE0-4-06 (correcting PHASE0-4-04's claim that the E0.4 acceptance criteria
  "continue to hold unchanged" — the one-file token-sheet criterion stopped holding literally
  at the same gate that appended that addendum). (2) Entry #11 below gives Gate 17's backdrop
  change the numbered record it never had, and records that #9 was amended in place. (3)
  DECISIONS D28 records the fifth Gate 16 test change D26's "all four" inventory missed;
  D29 records this batch's two test strengthenings (`test_governance.py` non-empty baseline
  guard; `users-admin.test.tsx` renamed and made to assert D25's actual nav intent). (4) Stale
  statements corrected in `DES.4-handoff.md` (claimed `Screens.dc.html` "isn't in this repo" —
  it landed at Gate 16) and `DES-track-handoff.md` (claimed to be a frozen 2026-07-30 snapshot
  while its table was updated through Gate 18). (5) `docs/frontend-guide.md` is subordinated to
  `docs/INTERFACES.md` "Frontend composition and shared components" with a precedence note and
  gains an owner-supplied "Starting E1" brief; INTERFACES.md cross-links back. (6)
  `frontend/public/images/README.md` gains an explicit provenance/license TODO for
  `forest-background.jpg` so the one compliance gap the DES batch left is tracked, not silent.
- **Why:** Rule R1 exists so a fresh session can trust the documents in the authority order.
  The phase doc contradicted the shipped shell and token reality, the change log had been
  edited in place, and one test's name asserted an invariant D25 deliberately abandoned.
- **Affects:** project_planning/phase-0-foundations.md
- **Addendum:** PHASE0-4-06

## #11 (2026-08-01): Late numbered record — Gate 17's backdrop asset change, and the in-place amendment of #9

- **What changed (at Gate 17, 2026-07-30, commit `7d5400a`):** `forest-background.jpg`
  committed web-sized (1920×1280, ~925 KB, EXIF stripped; the supplied original was a 24 MB
  camera file) and wired to back **every** page via `.shell-content`, scrimmed by the new
  additive token `--eoe-color-backdrop-scrim` (0.93 light / 0.94 dark, on D21's terms); the
  login hero keeps the lighter `--eoe-color-overlay` treatment. Both treatments paint a
  `background-color` first so a missing file degrades to a flat token color.
- **Why this entry is late:** at Gate 17 the change was recorded by amending entry #9 in
  place ("#9, amended" in the Gate 17 update) instead of appending a numbered entry. This log
  is append-only; a new token, a permanently committed binary, and an all-page visual
  treatment constitute a plan change that warrants its own number. #9's amended text stands
  as-is — rewriting it again would repeat the mistake — and this entry plus addendum DES-7-03
  are the durable record.
- **Affects:** project_planning/DES-track-handoff.md
- **Addendum:** DES-7-03

## #10 (2026-07-31): Fonts vendored and the DES track handed off to E1

- **What changed:** The DES track's last open implementation item closes before E1 starts.
  (1) **Fonts are vendored** rather than named-and-hoped: seven latin-subset woff2 files in
  `frontend/public/fonts/` (~160 KB) declared by the new `frontend/src/styles/fonts.css`, with
  `frontend/tests/fonts.test.ts` gate-enforcing that nothing is fetched from a CDN, that every
  family a token names is actually supplied, and that the status glyph subset covers every
  glyph token. (2) The seventh file exists because of a finding: **none of the six status
  glyphs exists in IBM Plex Sans, IBM Plex Mono, or Source Serif 4** (checked against the
  complete families), so the shape channel — one of the three channels the status vocabulary
  requires — was resolving to system fallback and to tofu on a minimal air-gapped host. It now
  ships as a 568-byte Noto Sans Symbols 2 subset behind the additive token
  `--eoe-font-family-glyph` (DECISIONS D27). (3) `docs/INTERFACES.md` gains **"Frontend
  composition and shared components"**: the props and use of `PageHeader`, `ContextBar`,
  `StatusChip`/`StatusLegend`, `EmptyState`, and `Can`, the two page-composition shapes, and
  the conventions later epics must follow (no literals, component CSS in `app.css`, serif is
  display-only, mono for identifiers, density from the row/control-height tokens, `.admin-table`
  as the pattern E1.8's TanStack tables generalise).
- **Why:** E1.8 builds a hierarchy tree, device tables, entity forms, and a bulk-import result
  grid — the first heavy consumer of the design system. An implementation session's inputs are
  the spec, its phase document, `docs/INTERFACES.md`, and `docs/DECISIONS.md`; the component
  library those sessions need was documented only in `Design System.dc.html`, which is **not in
  this repository**. Without this, E1 would re-invent table, form, and status vocabulary and the
  direction would drift on its first real use. Vendoring the fonts now also settles the type
  metrics before dense tables are built against them.
- **Also recorded:** open question 3 (config editor on tablet) stays open and belongs to E2, not
  E1. DES.8 remains blocked on E4–E6.
- **Affects:** project_planning/DES-track-handoff.md ("The three rules" item 3, status table,
  open questions)
- **Addendum:** DES-7-02

## #9 (2026-07-30): DES.7 applied — shell restructured, night theme shipped, page skeletons ahead of their epics

- **What changed:** DES.7 ("apply the design system") is done for the shell and the frame.
  `Shell.tsx` becomes V2·S1's dark top bar (DECISIONS D25); the night theme ships behind a
  manual toggle plus `prefers-color-scheme`, closing D21's recorded gap (D24); `app.css` is
  rewritten against V2 with shared `ContextBar`, `PageHeader`, `StatusChip`/`StatusLegend`,
  and `EmptyState` components. Routes and v2-styled skeletons were added for Map, Inventory,
  Configuration, and Provisioning **ahead of the epics that own their data** (E6, E1, E2, E4)
  — each renders only a header and a panel naming the epic that fills it. No mock devices,
  rows, or wizard steps: the point is that E1/E2/E4/E6 drop real data into a settled frame,
  not that the app looks finished.
- **Why it deviates:** the plan sequences DES.7 "after DES.4/DES.5 and the E2 UI". Only E0
  exists, so this batch does the half that does not need data — tokens, shell, theme,
  components — and stops at the boundary where invented content would start.
- **Deferred, noted here so it is not re-decided:** the **map engine** is not implemented.
  Direction is Google Maps satellite via the official JS API when online, an
  operator-supplied local image for air-gapped hosts (spec §15.1), ESRI later; E6 owns it.
  `Map.tsx` reserves the region and ships the real status legend around it. **Font
  vendoring** is also deferred — `tokens.css` names IBM Plex Sans/Mono and Source Serif 4
  with a fallback stack, and the woff2 files are not yet in `frontend/public/fonts/`, so the
  product currently renders in the fallbacks. **Image assets** land in
  `frontend/public/images/`: `forest-background.jpg` is present and backs **every** page via
  `.shell-content`, scrimmed by the new additive token `--eoe-color-backdrop-scrim` (0.93
  light / 0.94 dark) so it reads as texture and every documented contrast ratio still holds;
  the login hero keeps the far lighter `--eoe-color-overlay` and lets the photograph carry
  the screen. Both treatments paint a `background-color` first, so a missing file degrades to
  a flat token color rather than to unreadable text. The asset is committed web-sized
  (1920×1280, ~925 KB, EXIF stripped) rather than as the 6000×4000 / 24 MB camera original:
  it is fetched on every page, and git keeps binaries forever.
- **Also deferred:** the v1 bulk-edit modal (S4) and services onboarding wizard (S5) are
  flows nested inside Inventory and Provisioning, not top-level destinations in v2. They get
  built with E2.8 and E5.12 rather than being stubbed now.
- **Affects:** project_planning/DES-track-handoff.md ("DES.7 work items" 4-6, open question 2)
- **Addendum:** DES-7-01

## #8 (2026-07-30): DES.4 v2 tokens land; token namespaces gain an additive status/border/density extension

- **What changed:** `frontend/src/styles/tokens.css` and `tokens.alt.css` take the DES.4 v2
  ("field notebook") value set — same 30 property names, same five namespaces, values only.
  A third sheet, `frontend/src/styles/tokens.ext.css`, is accepted (DECISIONS D21, DES-4-01)
  and wired into `main.tsx`: it adds keys to the existing `--eoe-color-*`/`--eoe-space-*`/
  `--eoe-font-*` namespaces and introduces five new ones (`--eoe-border-width-*`,
  `--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`), closing
  the six-value status vocabulary spec §9.3/§6.2 requires. Nothing in the original E0.4 key
  set is renamed, removed, or repointed. `frontend/tests/tokens.test.ts` now treats
  `tokens.ext.css` as a third application-owned sheet. Separately, `app.css`'s four uses of
  `var(--eoe-space-1)` as a border/outline width (rendering a 4px sidebar border, `.card`
  border, and both focus outlines) now use the new `--eoe-border-width-hairline: 1px`.
- **Why:** DES.4-handoff.md flagged this as a change to an E0-owned interface (rule R2) and
  asked for a verdict before applying it; the project owner accepted it in this session to
  finish the DES.4 deliverable. `tokens.alt.css` does not yet mirror the extended keys with
  dark-mode equivalents — recorded as a known, deliberately deferred gap in D21, not silently
  dropped.
- **Affects:** project_planning/phase-0-foundations.md section 2 (E0.4 token namespaces)
- **Addendum:** PHASE0-4-04

## #7 (2026-07-24): Top-level guide/ directory and the deployment verifier (Gate 14)

- **What changed:** The fixed repository layout gains a top-level `guide/` directory: the
  clearly demarcated, GitHub-prominent home for every client-facing artifact (operator
  quickstart, seed-script instructions with implications, deployment-verifier
  instructions). E0.12's deliverable also gains `backend/app/verify.py`
  (`uv run python -m app.verify`): a shippable owner-journey verifier that drives every E0
  subsystem through a temporary owner account over real HTTP and then deletes the
  account (audit rows deliberately survive with a nulled actor — immutability holds).
  No API surface changes; the locked route/table contracts are untouched.
- **Why:** Project-owner directive: client-facing parts must live in one demarcated,
  easy-to-find group, with USER instructions for the seed script, and epic-wide
  verification through a seeded owner account that is removed afterwards.
- **Affects:** project_planning/phase-0-foundations.md section 2 (repository layout)
- **Addendum:** PHASE0-2-01

## #6 (2026-07-24): E0 readiness flight added (Gate 13)

- **What changed:** A cross-cutting readiness suite (`backend/tests/test_e0_readiness.py`,
  Gate 13) verifies E0.1 through E0.11 as a whole: the exact public surface (routes and
  tables) as a locked contract, env-var parity between documentation and Settings, the
  seams later epics consume (E1's un-FK'd scope columns and MAC-wide entity ids, E3/E5's
  SecretStore name shapes, E8.5's session-minting seam for OIDC), a data-seeded migration
  round trip, and production posture (prod frontend image actually serves; API container
  runs non-root). Two defects found and fixed in the process: the compose frontend lacked
  `VITE_API_BASE_URL`, and the API image ran as root.
- **Why:** Project-owner directive: verify the platform is production-poised and that the
  infrastructure later epics build on is genuinely ready, not just unit-tested.
- **Affects:** project_planning/phase-0-foundations.md section 5 (definition of done)
- **Addendum:** PHASE0-5-01

## #5 (2026-07-24): E0.11 lands before E0.10

- **What changed:** The implementation order of the last two build tasks swaps: E0.11
  (platform secrets envelope encryption) precedes E0.10 (optional TOTP). Task content is
  unchanged; gate tags stay bound to task ids (gate-11 completes before gate-10 in
  history).
- **Why:** A TOTP secret is a secret, and the cross-phase convention (rule R2, handbook
  section 3) requires secrets to move only through `SecretStore`. Building TOTP first would
  either violate that convention or store the secret plaintext and re-encrypt later.
- **Affects:** project_planning/phase-0-foundations.md section 4 (E0.10, E0.11)
- **Addendum:** PHASE0-4-03

## #4 (2026-07-23): E0.8 spec citation corrected

- **What changed:** The phase document's E0.8 task cites "spec 12.1" as the source of the
  audit-log requirement; section 12.1 covers tenancy. The requirement actually derives from
  spec section 14.1 (immutable audit log of every mutation) and section 13 (`GET /audit`).
- **Why:** Later sessions follow section references literally; an incorrect citation sends
  them to the wrong requirement.
- **Affects:** project_planning/phase-0-foundations.md section 4 (E0.8)
- **Addendum:** PHASE0-4-02

## #3 (2026-07-23): Session inputs are three documents, not two

- **What changed:** Project plan section 5 says each implementation session receives "exactly
  two inputs: spec v1.1 and the phase document." The implementation handbook (section 2), the
  later and operational document, specifies three inputs plus one conditional: the spec, the
  phase document, the current `docs/INTERFACES.md`, and `docs/DECISIONS.md` once it has
  content. The handbook's list governs.
- **Why:** The two documents disagree; sessions must know which list to follow.
- **Affects:** project_planning/echoes-of-earth-project-plan.md section 5
- **Addendum:** PLAN-5-01

## #2 (2026-07-23): E0.12 Seed script missing from the project plan

- **What changed:** Project plan section 3 lists eleven E0 tasks (E0.1 through E0.11). The
  phase document defines twelve; E0.12 (dev seed creating the initial owner account,
  credentials printed once, fresh environment to logged-in owner in one command) exists only
  in the phase document. The Jira board needs the extra story.
- **Why:** Task inventory drift between the plan and the phase document would surface as a
  missing story on the board.
- **Affects:** project_planning/echoes-of-earth-project-plan.md section 3 (E0)
- **Addendum:** PLAN-3-01

## #1 (2026-07-23): Task E0.0 (governance scaffolding) added before E0.1

- **What changed:** A task E0.0 precedes E0.1: binding machine-readable project rules
  (`.claude/rules/project-rules.json`), the `CLAUDE.md` loader, the three project logs
  (`docs/project-updates.md`, `docs/project-changes.md`, `docs/DECISIONS.md`), the
  `docs/INTERFACES.md` skeleton, the git baseline of `project_planning/`, the commit message
  template, and the gate runner. E0.0 ends in Gate 0, subject to rule R0 like every task.
- **Why:** The project is built as fresh scope-limited sessions (handbook section 2); the
  governing rules and the running record must live in the repository rather than in any one
  conversation, and they must exist before the first build task.
- **Affects:** project_planning/phase-0-foundations.md section 4
- **Addendum:** PHASE0-4-01
