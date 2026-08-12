# Project Updates

## 2026-08-12: INFRA.1 — the test suite stops starting a container per module (Gate 60 GREEN)

- **Task closed:** INFRA.1, an unnumbered E0-owned infrastructure batch landed at the head of
  `e5-batch-1` before checkpoint C4 on the owner's decision. DECISIONS **D128** and **D129**,
  project-changes **#29**, addendum **PHASE5-2-03**. No E5 unit changed.
- **Gate:** 60, GREEN. `make gate`, the entire accumulated suite, no filters.
- **Tests:** **1006 backend / 115 vitest / 4 Playwright**, 0 failed / 0 skipped / 0 xfailed /
  0 deselected. The seven added are `test_container_pool.py`, the pool's own contract suite.
  ruff, `ruff format` and the frontend typecheck all clean.
- **What it does:** `ephemeral_postgres` keeps its signature and its guarantee — a migrated,
  empty, private database — and stops being a container. One machine-wide warm Postgres runs
  with its data directory on tmpfs, `alembic upgrade head` runs ONCE into a template database
  keyed by a fingerprint of the migration directory, and each caller gets
  `CREATE DATABASE ... TEMPLATE`. Mosquitto persistence, Prometheus's TSDB, Grafana's SQLite
  and MinIO's data directory moved to tmpfs too. `make testpool-down` closes the pool by hand;
  otherwise it closes itself once four hours pass without an acquisition.
- **Measured, before and after, on the same machine:**

  | | Before (C3 tree) | After | |
  |---|---|---|---|
  | Backend stage | 290.21s | **224.15s** | -66s, on 7 more tests |
  | Containers created per gate | 193 | **65** | -66% |
  | Disk written per gate | 4.05 GB | **0.69 GB** | -83% |
  | One `ephemeral_postgres` | 4.02s | **0.017s** | 236x |

  The pre-change baseline was itself a green instrumented gate run rather than the ledger's
  recorded C3 figure, so the two numbers are comparable.
- **What it cost, and none of it was predicted:** three defects, all found by running the gate
  rather than by reasoning about it. Docker mounts a `--tmpfs` root-owned at mode 0755 while
  these images drop to unprivileged users, which panicked Prometheus and took the whole rig
  down with 37 errors in four modules. Grafana's startup on tmpfs then got fast enough to
  expose a latent race the rig had always had — nothing waited for Prometheus to scrape itself,
  and `/-/ready` answers strictly earlier than having data. And removing 55 Postgres startups
  removed the pacing that had kept D99's forwarder fault rare, which surfaced as seven
  `test_dev_broker` setup errors in a run whose 999 tests all passed (D129).
- **What it did not fix, measured so the next attempt aims correctly:** the remaining writes are
  not container churn. `docker build` accounts for ~638 MB of them (400 MB in
  `test_e0_readiness`, 238 MB in the `containers-build` stage) and the shipped compose stack's
  named volumes for 184 MB. The whole service rig now writes 30 MB, and 186 seconds of
  broker-heavy tests write 19 MB.
- **Consequence for phase 5:** section 5's ~300s ceiling is superseded and **224.15s is the new
  baseline C4 measures against**. C3 had reached 299.16s against a ~300s cap, which left E5.8b's
  compose-config tests and E5.10's keystone bring-up no margin at all.
- **Manual verification:** `make testpool-down` run by hand against a live pool — it removed
  the container, named it, and cleared the registry — and the next suite run started a fresh
  one and passed, which is the reap-and-recreate path end to end. The isolation and
  schema-equivalence properties are asserted by `test_container_pool.py` and were not checked
  by hand; they are automated precisely because they are the ones a person cannot eyeball.
- **Command:** `make gate`.

## 2026-08-12: E5.6, E5.7a and E5.7b — credentials a device can use, and settings that reach it (Gate 59 GREEN)

- **Tasks closed:** E5.6 (per-device broker credential minting), E5.7a (the service-settings
  projection and the privileged write), E5.7b (the two authorized E3-owned edits). Checkpoint
  **C3**. DECISIONS D120-D127, project-changes #27 and #28, addenda PHASE5-2-02 and PHASE5-4-01.
- **Gate:** 59, GREEN. `make gate`, the entire accumulated suite, no filters.
- **Tests:** **999 backend / 115 vitest / 4 Playwright**, 0 failed / 0 skipped / 0 xfailed /
  0 deselected. Backend stage **299.16s** (+20.1s over C2 for 57 new backend tests). ruff,
  `ruff format`, `mypy app` and the frontend typecheck all clean.
- **Command:** `make gate`.
- **The gate was green on the first full run of this batch**, which is worth saying plainly
  after C2's first run was red: the discipline that produced it was running the AFFECTED suites
  after each unit rather than only the new ones — the D118 lesson from C2. Three of those
  intermediate runs found real problems (below) before a gate ever saw them.

### What E5.6 built

- `app/services/credentials.py`: the `BrokerCredentialProvider` protocol **E4.6 imports rather
  than declares** (D105's reversal, now real), `DynsecCredentialProvider`,
  `DevBrokerCredentialProvider`, and `drain_pending_revocations`. Plus `broker_credential`
  (migration `c4e9b21f83da`), three routes on `/aggregators/{id}/broker-credential`, and
  `aggregator_acl_grants` extracted from `devbroker.acl_file_text`.
- **The ACL grants now have exactly one source, and the second renderer needed a grant the
  first does not have** (D120). `read` in an `acl_file` becomes TWO dynsec acltypes:
  `subscribePattern` decides whether the SUBSCRIBE is accepted, `publishClientReceive` whether
  a matching message is actually delivered. Granting only the first produces a device that
  subscribes successfully and then receives nothing — indistinguishable, from the device's side,
  from a platform that never published. Both renderers now read one list and a test checks each
  against it; `test_dev_broker.py` passes **unchanged**, which is the proof the ACL file's bytes
  did not move.
- **Three credential states, not two, on the owner's decision** (D121, project-changes #27).
  Deleting an Aggregator whose broker is unreachable returns 204, leaves `revoke_pending`, and
  the worker's sweep finishes the revocation. Refusing the delete would let one deployment's
  outage block inventory work; passing silently would strand a live credential forever. A CHECK
  ties `revoked_at` to the `revoked` state so the third value cannot make the timestamp
  ambiguous, and an unreachable broker (retried) is distinguished from a plugin that answered
  and REFUSED (raised and logged — retrying a configuration fault hides it).

### What E5.7a built, and the defect it closed

- `app/services/projection.py` (twelve keys, `PROJECTION` asserted against `CATALOG` at import),
  the `allow_write_restricted` flag, and `publisher.publish_all` with both callers on it.
- **`changed_keys` was comparing raw effective maps including the write-restricted keys** (D124,
  the E2-owned defect phase-5 §2 names). One services save therefore marked every Listener in
  the deployment as changed and minted a revision — and a retained publish — per Listener whose
  bytes were byte-identical to the previous one. On the SIM fleet this repository's own harness
  runs, that is **~600 pointless revisions and ~600 pointless retained publishes per save.** It
  now composes through `snapshot_from_raw`, the same function the payload and the drift sweep
  use, so the three cannot disagree. Measured by hand: one save on a deployment with one
  Aggregator and thirty Listeners produces **exactly one revision**.
- **The flag is four signatures, not the three fixed choice 3 counted** (D122), because
  `apply_change_plan` calls `put_overrides`. It also carries the "regenerated wholesale, never
  merged" behaviour the same fixed choice requires — so an S3 endpoint an operator clears leaves
  the projection instead of surviving forever.

### What E5.7b built

- `MqttClientManager.refresh()` and refresh loops in **both** hosts, `_async_sweep_loop`, and two
  sweep registrations: `service-config` (spec 16.4's late-device delivery) and
  `broker-credential` (D121's retry). **D125 states the whole E3-owned surface taken and, just
  as importantly, what was not.**
- **`services_recheck_sweep` is still NOT registered**, though E5.5's notes and the INTERFACES
  entry both said E5.7b would do it. Both have been corrected rather than left to imply
  otherwise. Spec 16.5's periodic re-checks need a production `ServiceTestRunner` dialling every
  deployment's real services on a timer — a behaviour no unit in this phase scoped — and it is
  now recorded in the ledger as **outstanding and a stop-and-ask**.

### Three things the intermediate runs found, recorded rather than smoothed over

1. **A test asserted plaintext where the contract says markers** (D126). E5.7b's acceptance
   words the retained message as "carrying all twelve keys", and the first version of the test
   read that as twelve VALUES. It is twelve keys with the four secrets as D51 markers — the E3.4
   contract, correct, and now pinned by an assertion that ALSO requires no plaintext anywhere in
   the payload. If a later change ever puts a credential in a retained broker message, that test
   fails.
2. **A "rotation" test was passing for the wrong reason.** `ephemeral_broker` ships files in with
   `docker cp` rather than a bind mount (a deliberate cross-platform choice), so rewriting the
   host password file and SIGHUPing changed nothing the broker could see. The test now copies
   the file into the container; the suite also dropped from 29s to 9s once the rotation actually
   worked.
3. **The gate-locked readiness suite caught the two new env vars missing from `.env.example`**
   — exactly what it is for.

### Two efficiency defects found by this session's own review of the cross-epic diff

- `restricted_keys()` walks the whole catalog and was being called **once per key** inside the
  plan's write-target loop. Hoisted.
- `service_config_sweep` built a full change plan for every deployment with any service row —
  loading every hierarchy table and recomputing effective config for every device under it, once
  a minute. The case that hits it is a deployment with only its `mqtt` row, which is **every**
  deployment with a working control plane and an unfinished wizard. It now returns before
  planning when there is nothing to deliver and nothing to withdraw, pinned by a test that makes
  the planner raise if it is reached.

- **Manual verification** (outside pytest, against a real Postgres container, a real
  `alembic upgrade head`, a real dynsec Mosquitto, a real uvicorn process and real HTTP —
  **28 checks, all passing**):
  - **E5.7a:** saved four services; all twelve keys landed in `entity_override` with every
    secret as a `$secret` MARKER and **no plaintext anywhere in the row or in any response**;
    **exactly one revision, for the Aggregator, with thirty Listeners present**; saving twice
    with identical input minted **zero** new revisions; `GET config/effective` showed all twelve
    at `source: deployment` with credentials redacted; and `PUT .../config/overrides` was walked
    through **all twelve keys one at a time** and 422'd `service_restricted` on every one.
  - **E5.6:** minted over HTTP — the response carries no password field at all, and the
    plaintext was retrievable only from SecretStore. The device then **logged in to the real
    broker with that password**, was **refused on its own `desired` topic**, and the paired
    authorized publish to the same topic **did** arrive (the control, because denial looks like
    silence). After `DELETE`, the broker **refused that login**. On a broker with no dynsec
    plugin the mint was refused 422 with a message naming the remedy (fixed choice 4).
  - **E5.7b, proven behaviourally rather than from a log:** a config apply to a deployment with
    no broker row left its revision `draft`; the row was then added **while uvicorn kept
    running**, and one refresh interval later the same apply reached **`pending`** — which is
    only possible if the live process holds a real connection. No restart.
  - **A pre-existing defect this exposed** (D127): the first attempt proved the above by
    grepping the uvicorn log, and found nothing — **including for the deployment connected since
    startup**. Every `app.*` INFO line in the API process is dropped, because uvicorn leaves the
    root logger bare, and `runner.py::main`'s docstring asserts the opposite. Not fixed here:
    logging configuration for the whole API is E0-owned and a stop-and-ask.
  - No plaintext credential appeared in the uvicorn log.

## 2026-08-12: E5.4a-e and E5.5 — five testers, one container rig, and a rollup that reproduces itself (Gate 57 GREEN)

- **Tasks closed:** E5.4a (verified, not authored, this session), E5.4b, E5.4c, E5.4d, E5.4e,
  E5.5. Checkpoint C2.
- **Gate:** 57, GREEN
- **Tests:** 942 backend / 115 vitest / 4 Playwright — 0 failed / 0 skipped / 0 xfailed /
  0 deselected. Backend stage 279.06s against C1's 262.7s, so the whole five-container rig
  costs **+16.4s** and the phase-5 §5 ~300s ceiling holds with ~21s to spare.
- **Command:** `make gate`
- **The first C2 run was RED, and it is recorded here because it found three real defects**
  that no per-unit run could have. `test_no_committed_secret_patterns` tripped on three
  `*_PASSWORD = "..."` constants (the scanner matches 20+ characters after the keyword);
  `E0_ROUTES` had never been extended for E5.5's `GET .../services/status`; and E5.4a's
  registry test still asserted `set(REGISTRY) == {"mqtt"}`. **None was contention** — every
  one reproduced on a targeted re-run. Two were latent in commit `09b5271`, which had been
  committed after running only its own test file. Fixed, then the gate was re-run in full.
- **Artifacts:** `app/services/clients/{httpbase,influx,prometheus,grafana,s3}.py` — the only
  place a deployment service is dialled from (fixed choice 8), with one shared failure
  taxonomy so five services speak one operator-facing vocabulary;
  `app/services/testers/{influx,prometheus,grafana,s3}.py` completing `REGISTRY`;
  `app/services/status.py` (`roll_up` as the sole writer of `deployment.services_status`,
  `DEGRADE_AFTER_FAILURES = 2`, `apply_test_results`, the re-check sweep as a callable E5.7b
  will register); `GET /deployments/{id}/services/status`; migration `b7d41f0c2e93` adding
  `deployment_service.required`; and `conftest.service_rig` — five containers in parallel,
  8.3s to ready, one session fixture on one xdist group.
- **Decisions:** D117 (the required flag is a stored column, not an argument to `roll_up` —
  the save path and the invariant sweep recompute with no test results in hand, so a parameter
  would make the denormalized column irreproducible from its own rows), D118 (a test that pins
  "nothing else has been built yet" is not a test of a behaviour; three instances found),
  D119 (importing a conftest FIXTURE defeats session scope and built the rig three times —
  measured at 5/10/15 containers, and worth 27s on four suites alone).
- **Manual verification:** the whole loop driven over HTTP against a real uvicorn, a real
  Postgres, and real Influx / Prometheus / Grafana / MinIO / Mosquitto containers. Logged in,
  saved all five services, and: the GET echoed **no** submitted secret and rendered every set
  one as `{"$secret_set": true}`; `POST .../services/test` returned `pass` for all five with
  every check green (mqtt connect/round_trip/dynsec, influx query/write/cleanup, prometheus
  read_query/remote_write, grafana health/datasources/contact_point, s3
  head_bucket/write/cleanup) and the rollup reached `verified`; **no credential appeared
  anywhere in the test response**; replacing the Influx token with a wrong one moved the row
  to `untested` on save (a save unverifies) and then to `failed` with the rollup at
  `degraded`; removing the S3 credentials produced `not_required` and `required=False` rather
  than a failure; and a POST without the CSRF header was refused with 403. **One thing the
  walkthrough did NOT demonstrate:** the `DEGRADE_AFTER_FAILURES` tolerance path
  (verified → one failure → still verified), because the preceding save had reset the row to
  `untested` — which is the correct onboarding behaviour, and the tolerance path is covered by
  `test_services_status.py` rather than by hand.
- **Open question for the owner, recorded in the ledger:** E5.4e must answer `not_required`
  "for a deployment with raw-audio upload disabled" and **the settings catalog has no such
  toggle**. The tester keys on both S3 credentials being absent; a half-entered form is tested
  for real and fails. An explicit `upload.raw_audio_enabled` key would be an E2-owned catalog
  change and a stop-and-ask.

## 2026-08-11: E5.1-E5.3 — services onboarding gets a data model, a write-only API, and a test framework (Gate 54 GREEN)

- **Tasks closed:** E5.0, E5.1, E5.2, E5.3 on branch `e5-batch-1`, worked in a separate git
  worktree so the parallel SIM sessions keep the primary tree. This is checkpoint **C1** of the
  epic's five, and it carries E5.3 (a C2 unit) as well, because the unit was finished before the
  gate ran and gating it separately would have bought nothing.
- **Gate 54 GREEN:** `make gate`, **839 backend / 115 vitest / 4 Playwright, 0 failed / 0
  skipped / 0 xfailed / 0 deselected**. Backend stage 262.7s; `ruff check`, `ruff format
  --check`, `mypy app` (strict) and `tsc --noEmit` all clean.
- **E5.1 (built in the prior session, verified in this one).** `deployment_service` widens
  rather than forks (phase-5 fixed choice 1): five spec 16.2 `service_key`s, the four
  MQTT-shaped connection columns nullable behind a conditional CHECK that still makes them
  mandatory for an `mqtt` row, `config`/`secret_names` JSONB, the per-service status block, and
  `deployment.services_status`. Migration `a31287354e23`. It also fixed a live 500:
  `DELETE /deployments/{id}` hit `deployment_service`'s hard FK for any deployment `devbroker`
  had touched. D109.
- **E5.2 — the write-only secrets API.** `GET/PUT /deployments/{id}/services`, with one
  `extra="forbid"` Pydantic model per service in `app/services/schemas.py` — which is where the
  typing E5.1 promised when it chose JSONB actually happens. Secret fields take a plaintext, the
  D51 keep sentinel, or nothing; the GET renders a set secret as the sentinel and is redacted by
  construction, because it reads the row and never SecretStore. **D110:** the PUT is a partial
  collection of wholesale members — within a service a merge would silently keep a field the
  operator cleared, and across services wholesale replacement would force the wizard to
  resubmit four credentials it does not hold. No delete: removing the `mqtt` row would strand
  the control plane.
- **E5.3 — the connection test framework.** `ServiceTester`, `TestResult`/`CheckResult` with a
  required remedy, the concurrent runner with per-tester **and** whole-call budgets,
  `resolve_credentials`, and `POST .../services/test`. **D111:** a tester says four things and
  two of them are not failures (`not_required`, `not_configured`) — red an operator is meant to
  ignore destroys the meaning of the red that matters. `REGISTRY` ships **empty**; E5.4a-e fill
  it, and until then the endpoint honestly reports no results rather than inventing verdicts.
- **Two new permissions** (fixed choice 9): `MANAGE_SERVICES` (Owner and Deployment Operator
  only) and `VIEW_SERVICES` (all four roles). The RBAC suite is test-critical: these are rows
  **added** to its matrix and every existing assertion is untouched.
- **Manual verification** (outside the test harness, against a real Postgres container, real
  `alembic upgrade head`, a real uvicorn process and real HTTP): seeded the demo hierarchy, PUT
  Influx and S3 credentials as plaintext, and confirmed by `grep` over the raw response bodies
  that **none of the three submitted plaintexts appears in the PUT or GET response**; the GET
  rendered every set secret as `{"$secret_set": true}` and the five services in spec 16.2 order.
  `SecretStore.get` round-tripped both values, and a `LIKE` over `secret.ciphertext` matched
  zero rows — the values are stored encrypted, not merely hidden. `POST .../services/test`
  returned 200 with `results: []` (no tester registered yet). The audit rows read
  `{"services": {...field names...}}` and `{"outcomes": {}}`, and a `LIKE` over `audit_log.detail`
  for the plaintexts matched zero rows. A field tech got 200 on GET and **403** on both PUT and
  test. No plaintext in the uvicorn log. A deployment with pods still refuses with **409**; a
  childless deployment carrying two service rows and three secrets deleted with **204**, leaving
  zero rows and zero secrets — the E5.1 500, gone.
- **One honest note on the road to this gate.** Two earlier broad sweeps taken while the SIM
  session was live returned 829/7 errors and 831/5 errors. Every error was a container fixture
  failing to start, none was a test-logic failure, and the errored modules moved between runs.
  The fault is D99's `/forwards/expose ... 500`, which `docker_retry` already retries and which
  still exhausts under two sessions. **Neither run was recorded as a gate result**; the gate
  above was run with the other session idle. **D112** records the defect, the interim rule
  (check before gating) and the harness fix that is owed — and says explicitly not to widen
  `docker_retry`, which is narrow on purpose.
- **Artifacts:** `backend/app/services/schemas.py`, `backend/app/services/testers/{__init__,base}.py`,
  `backend/app/api/services.py`; `app/auth/rbac.py` and `frontend/src/lib/rbac.ts` (two
  permissions); suites `backend/tests/test_services_api.py` (24) and
  `backend/tests/test_service_testers.py` (25), plus additions to `test_rbac.py` and the two new
  routes in `test_e0_readiness.py::E0_ROUTES`; `docs/INTERFACES.md` gains its **Owned by E5**
  section; DECISIONS D110, D111, D112; `project_planning/e5-progress-ledger.md` current per unit.

## 2026-08-11: The gate runs in parallel — 541s to 260s (Gate 53 GREEN)

- **Not a SIM task.** Taken between SIM.0 and SIM.1, on the owner's instruction, because five
  more task gates had to be paid for and the waiting had become the dominant cost of the work.
  DECISIONS D99.
- **Gate 53 GREEN:** 766 backend / 114 vitest / 4 Playwright, 0 failed / 0 skipped / 0 xfailed
  / 0 deselected. Backend stage **260s, down from 541s**; the whole `make gate` now finishes in
  4m38 rather than a little over ten minutes.
- **What changed.** `-n 6 --dist loadgroup`, with a `tryfirst` collection hook marking every
  test with an `xdist_group` named after its module. Module granularity is forced by the suite
  as written: module-scoped container fixtures, and files whose tests deliberately build on
  each other. `test_compose_stack` and `test_verify_tool` share one group, because there is one
  host port 15173 and they both want it.
- **Two defects surfaced by turning it on, both real.** A parametrization keyed on `id(p)` — a
  memory address — collected different test ids in every process and made xdist refuse to run;
  the ids are now named after the cases they describe. And Docker Desktop's port forwarder
  returns `/forwards/expose ... 500` under concurrent publishes, which had already cost two
  serial gates today; `conftest.docker_retry` now retries exactly that fault and nothing else.
- **R0 is untouched:** one unfiltered invocation of the whole suite, same guard, same counts.

## 2026-08-11: SIM.0 — the SIM phase document, and two gate repairs (Gate 52 GREEN)

- **Task closed:** SIM.0 (phase document and records) on branch `sim-batch-1`. DECISIONS
  D97-D98, project-changes #23, addendum PLAN-3-02.
- **Gate 52 GREEN:** `make gate`, 766 backend / 114 vitest / 4 Playwright, 0 failed / 0 skipped
  / 0 xfailed / 0 deselected.
- **What shipped.** `project_planning/phase-sim-simulation-harness.md` — epic SIM's binding
  scope, written to the project plan §5 structure — plus `project_planning/sim-progress-ledger.md`
  as the per-task record for the epic. Fixed choices settled at plan approval: `/sim` as its own
  uv project reaching the backend by path, the contracts module as the exclusive wire surface,
  aiomqtt, TOML scenario files over a typed behaviour registry, REST provisioning, a `sim`
  compose profile, and two gate stages (`sim-quality`, `sim-protocol`).
- **The scale ambiguity is resolved, not inherited.** Spec 14.2 and project plan §3 read the
  simulation target differently (roughly 30 Listeners, versus 30 each across 20 Aggregators).
  The plan's reading is binding: **20 × 30 = 600**, parameterized so CI runs 2 × 3. It is the
  demanding reading, and spec 14.2 says the target MUST run comfortably on one host.
- **Two gate repairs this task did not plan on, both honest.** D97: 
  `test_shutdown_leaves_no_running_tasks` was a flake — aiomqtt cancels its own `_misc_loop`
  through `call_soon_threadsafe`, so a task cancelled but not yet reaped reads as alive the
  instant `stop()` returns. Under full-gate load that window widens, which is why it passed
  standalone and failed in the gate. The assertion now polls for up to 5 seconds; a genuinely
  leaked connection task still fails it. Fixed under R0's wrong-test clause rather than re-run
  until green. D98: the suite gains `--timeout=300` and `--durations=10`, on the owner's
  instruction, so a wedged Docker socket dies named instead of holding the gate hostage; the
  five image-building tests carry `@pytest.mark.timeout(1200)`.
- **Environment note.** Two earlier gate attempts went red on the host rather than the code: a
  running `eoe-qa-*` stack held the `FIXED_PORTS` pins, and Docker Desktop's port forwarder
  returned `/forwards/expose ... 500` for one run. Both were resolved before the green gate;
  neither touched the tree.
- **Next:** SIM.1 (mock Aggregator), gate 53.

## 2026-08-11: E3.8-E3.13 — epic E3 complete (Gates 46-51 GREEN)

- **Tasks closed:** E3.8, E3.9, E3.10, E3.11, E3.12, E3.13 — **and with them every E3 task**
  (E3.1-E3.13), on branch `e3-batch-1`. DECISIONS D88-D96.
- **One entry for six tasks, on the owner's instruction (D89).** Each task still ended in its
  own FULL green gate, commit, push and `gate-{N}` tag; only the per-task entry was batched
  here. Recorded as a deviation from R1 rather than quietly done.
- **Gates:** 46 (E3.8), 47 (E3.9), 48 (E3.10), 49 (E3.11), 50 (E3.12), 51 (E3.13) — all GREEN,
  each `make gate`, 0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Tests at close:** backend 766, vitest 114, Playwright 4 (from 661 / 96 / 4 at gate 45).
- **What shipped.** E3.8 LWT status in `aggregator_status`; E3.9 spec 6.5 Listener liveness on
  `device_state` plus the one shared `listener_verdict`; E3.10 `POST /aggregators/{id}/commands`;
  E3.11 `reconciliation_event` and the per-device timeline with a UI panel; E3.12 `WS /ws`,
  the Postgres LISTEN/NOTIFY bus, and real device status (lifting D40); E3.13 apply wired to
  publication, `EOE_PUBLISH_ENABLED` defaulted on, and the end-to-end loop in CI.
- **THE definition of done, as one test.** `test_a_config_edit_reaches_a_device_and_comes_back_applied`
  runs preview → apply → retained desired message read by a mock device on its OWN broker
  credential → ack → `applied` → timeline → websocket, against a real Mosquitto and a real
  worker. Deliberately the only test spanning every task: a red there with green elsewhere
  means the pieces do not fit rather than that a piece is broken.
- **Two design calls worth re-reading before E4.** (1) LWT status went to its own table rather
  than the `device_state` columns E3.5 anticipated, because a will is published by the BROKER
  and `device_state` means "what the device said" (D88); the rule now is that reported facts
  live on the report row and broker-authored ones do not. (2) Status is ordered by RECEIPT,
  not by the payload clock — a device composes its will at connect time, so ordering it the
  way spec 7.4 orders reports would reject every LWT as stale and leave dead devices reading
  online forever.
- **D40 lifted and rewritten, not deleted (D60).** Status renders only where the API reported
  one; `unknown` is a first-class value that renders as a muted dash, never a chip; config
  routes still show none. The guard test now asserts all three.
- **Four defects found and fixed during the batch, three of them invisible to CI.**
  D87: with publishing on, the API lifespan died (`exit 3`) when it beat the migrations to
  the `deployment_service` table — it would have broken every `compose up` once E3.13 flipped
  the flag. D90: the guide's POSIX path omitted `-p eoe-qa`, so a walkthrough stack held the
  gate's ports under a name no documented teardown named. D92: the worker's disabled
  healthcheck was accepted by the local Compose and rejected by CI's. D94: the connection loop
  re-raised `CancelledError` inside `async with aiomqtt.Client(...)`, stranding `_misc_loop`
  with a live socket on **every reconnect** — surfaced as a gate flake, fixed as the leak it
  was rather than re-run until green.
- **The pattern behind three of those:** the local gate and CI do not run in identical
  environments, and each difference that bit us now has a guard rather than a memory
  (`compose_env` pins every interpolated variable; `disable: true` is banned).
- **Manual verification:** the owner drove `guide/e3-verification.md` §7 against the live QA
  stack (worker startup, gated publish, timeout, drift, restart), corroborated by the worker
  log. §§8-13 ship as written walkthroughs and are covered by named tests; they have not been
  driven by hand yet, which is stated plainly here rather than implied. The walkthrough is the
  next thing to run against the stack.
- **Still deliberately absent:** the Map (E6), alerts and the `alerting` status (E7),
  provisioning bundles (E4), service onboarding (E5), the fleet-scale simulator (SIM).

## 2026-08-10: E3.7 reconciliation worker — the loop runs itself (Gate 45 GREEN)

- **Tasks closed:** E3.7 (branch `e3-batch-1`; DECISIONS D80-D87). Plan change project-changes
  #22 / addendum PHASE3-4-02: the task additionally ships `POST /revisions/{id}/publish`, a
  `worker` compose service, and an outbound publish connection in the API. No task moves;
  E3.13 still owns wiring E2's bulk apply and flipping the flag on.
- **Gate:** 45, GREEN — on the third attempt. The first two were red and both are recorded
  below verbatim, because one of them was a real defect in this task.
- **Tests:** backend 661 (+5 over the 656 this task first collected: 4 new here, 1 from
  splitting the compose-env guard out), vitest 96, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Command:** `make gate`
- **Artifacts:** `backend/app/controlplane/runner.py` (`ReconciliationWorker`,
  `pending_timeout_sweep`, `drift_sweep`), migration `c41e9b7d3a58`
  (`deployment.pending_timeout_seconds`, `deployment.auto_reconcile`,
  `config_revision.published_at`), `POST /revisions/{revision_id}/publish`,
  `MqttClientManager.start_or_retry`, the `worker` compose service, the API lifespan,
  `service_unavailable` in the D8 vocabulary, `backend/tests/test_reconciliation_worker.py`,
  `guide/e3-verification.md` §7, and the INTERFACES worker / policy / publish-route sections.
- **Both halves of the phase acceptance are in the suite**, against a real broker:
  `test_the_full_journey_publish_ack_drift_republish_and_timeout` walks publish → ack → drift
  injection → operator re-publish → timeout in order, and
  `test_a_restarted_worker_resumes_the_windows_it_did_not_start` has one worker publish a
  revision and a DIFFERENT one time it out, with the retained desired message still readable
  by a device that connects after both. Nothing is handed between them: the window is
  `config_revision.published_at` and the config is a retained message (spec 14.3).
- **RED GATE 1, verbatim: `3 failed, 653 passed`.** `test_compose_stack` and `test_verify_tool`
  failed on `Bind for 0.0.0.0:18883 failed: port is already allocated` — the QA stack from the
  manual walkthrough was still up, which the guide's own D44 warning predicts.
  `test_mqtt_manager::test_shutdown_leaves_no_running_tasks` failed on
  `tasks outlived stop(): ['Task-1072']`, a leaked aiomqtt `_misc_loop`; it passed 21/21 in
  isolation immediately after and in both later full runs, so it was contention from the QA
  stack plus parallel image builds. **Recorded rather than dismissed:** a test that fails only
  under load is a latent gate flake, and the next session that sees it should read this entry
  before assuming its own change caused it.
- **RED GATE 2, verbatim: `2 failed, 654 passed`, and this one was a defect (D87).**
  `container eoe-gate-test-api-1 exited (3)` — uvicorn's code for a lifespan that raised. With
  `EOE_PUBLISH_ENABLED` on, the D86 lifespan awaited a bare `MqttClientManager.start()`, which
  reads the `deployment_service` rows once; the API comes up beside Postgres in compose, beat
  the migrations to that table, and died of `UndefinedTable` — taking every route unrelated to
  publishing with it. **This would have broken every `compose up` at E3.13**, where the flag
  defaults on. Fixed by moving the retry INTO the manager as `start_or_retry()`, which also
  deletes the worker's private `_connect_with_retry`: the worker having that guard while the
  API did not is exactly what shipped the bug. Verified by mutation — restoring the bare
  `start()` turns `test_the_api_starts_even_when_the_broker_rows_cannot_be_read` red.
- **Why CI could not have caught it, and the second fix.** Compose interpolates `${VAR}` from
  the process environment first and `deploy/.env` second, and `compose_env()` left five
  variables unpinned — so the container tests read them from a developer's scratch `.env`,
  which §7 instructs you to fill with `EOE_PUBLISH_ENABLED=true`. The gate tested a different
  stack on a walkthrough machine than in CI, where no `.env` exists. Every interpolated
  variable is now pinned and guarded by
  `test_compose_env_pins_every_variable_the_compose_file_interpolates`, which found three more
  on its first run — including `EOE_CORS_ORIGINS`, whose walkthrough value still names the
  pre-PHASE0-2-02 port.
- **RED GATE 3 was self-inflicted and is noted for honesty:** `5 failed` on the worker suite
  because `StubManager` did not model the new `start_or_retry` contract. Test-double gap, not
  production code; fixed by giving the double the contract.
- **Manual verification:** the owner drove `guide/e3-verification.md` §7 against the live QA
  stack through the worker-startup, gated-publish, timeout, drift and restart blocks, and the
  worker log corroborates all of them — `drifted from revision ... (1 differing key(s))`,
  `timed out after 12s with no device report`, and a restart at 00:17:50 whose new process
  failed out a window it never opened. §7's last two blocks (the inert `auto_reconcile` flag
  and the 403/404/503 refusals) were NOT driven by hand: each is covered by a named,
  substantive test, and re-deriving them through hand-made accounts would have added no
  information the suite does not already carry. Recorded plainly rather than implied.

## 2026-08-10: E3.5 reported consumer — the loop closes on real device reports (Gate 44 GREEN)

- **Tasks closed:** E3.5 (branch `e3-batch-1`; DECISIONS D76-D79). No plan change: batch 2 runs
  E3.6, E3.4, E3.5 exactly as project-changes #20 said it would, and this task consumed both
  the state machine and the publish path that ordering existed to provide.
- **Gate:** 44, GREEN. `make gate`, run again after the walkthrough corrections below.
- **Tests:** backend 611 (+53: 33 in `test_reported_consumer.py`, 20 added to
  `test_mqtt_contracts.py` for the topic parser), vitest 96, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Command:** `make gate`
- **Artifacts:** `backend/app/controlplane/consumer.py` (`ReportedConsumer`, the second half of
  the spec 6.4 loop), `contracts/mqtt.parse_topic` + `TopicKind`/`ParsedTopic`, migration
  `a2cf00fc037f` (`device_state`, `device_event`), `quarantine_report` made public in
  `inventory/identity.py`, `delete_device_state_for` wired into the E1 aggregator and listener
  DELETE endpoints, `backend/tests/test_reported_consumer.py`, `guide/e3-verification.md` §6,
  the INTERFACES "reported consumer" and "device_state / device_event" sections.
- **The three acceptance criteria, all three named in the suite.** The conflicting-MAC test
  captures the Listener row before the report and compares it to itself afterwards, field for
  field — spec 4.3 item 2 is a claim about a row that does NOT change, and asserting no write
  was attempted would not be the same claim. The replay test sends byte-identical messages
  twice; the reordering test lands a report from `t` after one from `t+10`.
- **Verified by mutation, not by assertion alone.** Flipping the staleness test from `<` to
  `<=` turns the replay test red, which is the point: idempotency has to come from
  `applied_revision_id` plus checksum as spec 7.4 words it, not from a timestamp shortcut that
  would hide a broken comparison behind an early return. Removing the checksum-recompute makes
  the self-contradiction test report `rejected` instead of `malformed` — i.e. the platform
  would fail the revision, blaming the config, for what is a firmware defect (D70).
- **A report the platform does not believe is not stored either (D79).** Spec 4.3 item 2 stops
  the platform overwriting inventory; a quarantined or misrouted report also writes no
  `device_state` row and moves no revision, because storing it would launder a rejected claim
  into the record E3.7's drift sweep reads. Identity comes from the TOPIC in every path — the
  spec 7.1 ACL authenticated those segments, and no inbound spec 7.3 payload carries an
  identity field.
- **Manual verification:** full QA stack (compose, seed, dev broker), driving the walkthrough's
  own `check-consumer.py` against a real Mosquitto with real revisions from E2's `POST
  /config/apply`. Observed, in order: `pending -> applied` on a matching report published as
  `dev-demo-agg-rc-01`; `unchanged` on a byte-identical replay with no second audit row;
  `stale` on the same message dated a minute earlier, with the stored report untouched;
  `malformed` on a device whose checksum was not its own config's, revision unmoved; and
  `rejected` -> `failed` immediately on a coherently wrong report, whose audit detail listed
  `differing_keys: [analysis.confidence_threshold, logging.verbosity]` and no values. Then the
  identity walk: two `mac_conflict` quarantine rows against ONE open `duplicate_identity`
  alert (append vs. dedupe, D37), the contested Listener row unchanged including `updated_at`,
  a third quarantine row `reason=unknown_mac` with no alert at all (D76), a
  `provisioning_required` alert for a ghost `aggregator_uuid`, and exactly one `device_state`
  row in the database — the Aggregator's, none for either quarantined MAC. Events: one row,
  then `duplicate_event` on redelivery, then two rows for the same code a minute apart, then
  the `NULLS NOT DISTINCT` case (an Aggregator-level event with no `listener_mac`, published
  twice, one row). Finally a status message returned `not_mine`, which is E3.8's seam.
- **Two walkthrough defects found by running it, and corrected against observed behaviour.**
  §5's publish script waited on `coordinates[0].deployment_id`; coordinates are ordered by
  slug, so that is high-desert, and the redwood-coast publish raced its own connection and
  raised `BrokerUnavailable`. It now waits on the deployment by name. §6's first draft pinned
  one revision id at startup, so the step that tells you to publish a fresh revision would
  report the OLD one as `superseded` rather than the new one as `failed`; the probe now
  re-reads the newest revision on every message. Both were caught the same way gate 43's three
  corrections were — by doing what the guide says and reading what actually happened.
- **Scope notes.** `device_state` ships now with owner approval (D78) because spec 7.4's
  ordering rule needs a per-device memory that revision timestamps alone cannot supply, and it
  is explicitly E3.8's and E3.9's to extend with LWT online state and spec 6.5 liveness — E3.5
  stores neither. `E0_TABLES` in `test_e0_readiness.py` gained both table names through that
  guard's documented extension mechanism.

## 2026-08-10: E3.4 desired publish path — config reaches a device (Gate 43 GREEN)

- **Tasks closed:** E3.4 (branch `e3-batch-1`; DECISIONS D71-D75). No plan change: batch 2 runs
  E3.6 before E3.4 exactly as project-changes #20 said it would, and this task consumed the
  state machine that ordering existed to provide.
- **Gate:** 43, GREEN. `make gate`, then the backend stage again to read the counts cleanly.
- **Tests:** backend 558 (+21, all in `test_publish_revision.py`), vitest 96, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Command:** `make gate`
- **Artifacts:** `backend/app/controlplane/publisher.py` (`publish_revision`, the only way a
  revision reaches a device), `backend/tests/test_publish_revision.py`,
  `guide/e3-verification.md` §5, the INTERFACES "desired publish path" section (and the E2
  `config_revision` bullet that claimed `publish_revision` did not exist yet).
- **The acceptance criteria, both of them.**
  `test_a_device_connecting_afterwards_still_receives_its_desired_config` publishes through the
  real client manager to a real broker and only THEN connects a subscriber — as the
  Aggregator's own dev credential, so the spec 7.1 ACL is on trial with it. That is the spec
  6.4 reconnect property, and an unretained publish passes every other test in the file and
  fails this one. Proven by deliberately flipping `retain=True` to `False`: the subscriber
  timed out against a live Mosquitto container. `test_republishing_the_same_revision_is_idempotent`
  is the second.
- **A silent-failure bug the tests could not have caught, found by the manual pass (D75).** The
  first implementation resolved the desired topic by `Aggregator.aggregator_uuid ==
  revision.target_id`. E2 actually writes the PLATFORM UUID (`aggregator.id`) there — spec 4.2
  keeps the three identifiers distinct — and the suite's own fixtures had invented the same
  wrong shape, so the tests agreed with the bug and passed. The first real revision from E2's
  apply endpoint showed `target_id: "8ebd3c87-…"` and the mistake was immediate. The failure
  mode is the dangerous kind: a well-formed topic no device subscribes to and no ACL grants,
  the revision going `pending`, and the device timing out to `failed` five minutes later
  looking like a hardware fault. Fixtures now derive the id from live inventory
  (`platform_uuid_of`), and a new test pins the refusal.
- **The publish happens inside the database transaction (D74).** State change staged, bytes
  sent, commit only on success. Verified by hand: with Mosquitto stopped, the publish raised
  `BrokerUnavailable` and the revision was still `draft` with no audit row. The alternative —
  commit `pending`, then fail to publish — would report a spec 6.2 timeout, which under D70
  means "the device never answered", so the platform would be blaming a device for its own
  broker outage.
- **The pair rule from D69 is now closed on both sides (D73).** `supersede_open_revisions`
  supersedes unconditionally; this task supplies the guard that makes it safe. Verified live:
  publishing an older revision raised `StaleRevision` naming the newer one and left both rows
  untouched, and a later publish superseded two open revisions at once.
- **Idempotent republish re-sends rather than no-ops (D72).** Two publishes of one revision
  produced two identical retained messages, one `pending` state and exactly one
  `revision.publish` audit row. The re-send is the operator's repair for a broker that lost its
  retained store.
- **Manual verification:** full QA stack (compose, seed, dev broker). Created draft revisions
  through E2's real `POST /config/apply` (response `state: draft`, `publish_enabled: false` —
  unchanged), published with the walkthrough's own script, and read the retained payload off
  the broker with `mosquitto_sub` as `dev-demo-agg-rc-01` **after** the publish: `revision_id`
  and `checksum` matched the apply response byte for byte, `target.id` was `demo-agg-rc-01`.
  Set `network.wifi_password` and confirmed the wire carries
  `{"$secret": "config:pod:…:network.wifi_password"}` and never the passphrase. Three
  walkthrough assertions were wrong on first writing and were corrected against observed
  behaviour rather than the other way round: the broker-outage step needs the
  `wait_connected` line removed (otherwise E3.2's `TimeoutError` fires first and E3.4 is never
  reached), unset secrets read `null` rather than showing a marker, and the guide's script had
  inherited the same identifier bug as the implementation.

## 2026-08-10: E3.6 revision state machine — TEST-CRITICAL suite locked (Gate 42 GREEN)

- **Tasks closed:** E3.6 (branch `e3-batch-1`; DECISIONS D69-D70). No plan change: batch 2 runs
  E3.6 before E3.4 and E3.5 exactly as project-changes #20 said it would, because both the
  publisher and the reported consumer transition revision state and the authoritative module
  has to exist before either calls it.
- **Gate:** 42, GREEN. `make gate`, run twice (the second to read the counts cleanly).
- **Tests:** backend 537 (+52, all in `test_revision_state.py`), vitest 96, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Command:** `make gate`
- **Artifacts:** `backend/app/controlplane/revision_state.py` (the machine),
  `backend/tests/test_revision_state.py` (**the third of the four test-critical suites**,
  alongside RBAC and the merge engine), `guide/e3-verification.md` §4.
- **The acceptance criterion, stated as one assertion.**
  `test_the_transition_table_matches_spec_6_2_line_for_line` compares the module's table
  against `SPEC_6_2_TABLE`, a verbatim transcription of the spec's table with its Trigger
  column text included, so the two can be held side by side and diffed by eye. The legal set in
  the test is rebuilt from that transcription and nothing else — adding a transition means
  editing the transcription, which means reading spec 6.2 again.
- **Illegal transitions are proven by enumeration, not by example.** All 288
  `(source, target, trigger)` triples are generated and the 276 outside the legal set must
  raise; a hand-picked list of illegal cases cannot prove absence. One test rather than 276
  parametrized ones, reporting every offender at once, because a table widened wrongly is
  usually widened by more than a single triple.
- **A transition is a TRIPLE, not a pair.** `pending -> failed` is legal as an apply error and
  as a timeout, and illegal as "operator retries" — which is `failed -> pending` read
  backwards. Validating the pair alone would have accepted it.
- **The spec contradicts itself once, and the owner ruled (D69).** Spec 6.2's table lists only
  `pending` and `applied` as sources for `superseded`; its diagram four lines below reads
  `(any non-terminal) --new revision--> superseded`. The diagram wins: under the table alone a
  `failed` revision can never be closed out, so an operator who fixes their config and
  publishes a new revision leaves the old row at `failed` forever beside an `applied` one, with
  nothing saying which is live. The suite keeps both statements attributable —
  `SPEC_6_2_TABLE` and `SPEC_6_2_DIAGRAM_EXTRA` are separate constants — and the three rows
  should be fed back into the next spec revision.
- **One design changed by the owner's push-back, and it is the better one (D70).** The first
  proposal for "device acks revision R but reports config that is not R" was to leave the
  revision pending and let the 300-second window resolve it. That reports a *timeout* for a
  device that answered in two seconds. Instead the ambiguity is removed: a report whose own
  `checksum` disagrees with its own `config` is internally inconsistent and is rejected at the
  boundary as malformed with no state change, while a coherent report that disagrees with the
  revision is a definite negative answer and fails immediately under `report_error`. That
  leaves `Trigger.TIMEOUT` attached to exactly one transition and meaning exactly one thing —
  no valid report arrived — which a suite test pins. E3.5 implements the consumer half.
- **Two properties that only real rows can prove.** `load_for_transition` takes
  `SELECT ... FOR UPDATE`, demonstrated by holding the lock in one transaction and watching a
  second `FOR UPDATE NOWAIT` fail rather than read through it; and when an ack and a timeout
  race for one pending revision the lock serializes them and the guard refuses the loser, so a
  true `applied` is never overwritten by a `failed` that did not happen.
- **The sweep and E3.4's guard are a pair, on purpose.** `supersede_open_revisions` closes every
  other open revision for a device unconditionally, with no timestamp comparison, which is only
  safe because E3.4 will refuse to publish a revision that is not the newest for its device.
  Both docstrings say so; removing either alone is a data-loss bug that discards drafts.
- **Manual verification:** the walkthrough's new §4 run line by line in a REPL — the six states,
  `superseded` as the only dead end, the same pair legal under `timeout` and illegal under
  `retry`, all four `check` messages distinguishing their three different mistakes, the single
  `timeout` pair, `parse_state` refusing `"apllied"` while naming the six legal values, and
  `failed -> superseded` legal under D69. The twelve `spec_trigger` strings printed and read
  back against spec 6.2's Trigger column. No stack needed; this task is a library.

## 2026-08-10: E3.3 spec 7.3 payload models — the wire contract is now complete (Gate 41 GREEN)

- **Tasks closed:** E3.3 (branch `e3-batch-1`; DECISIONS D67-D68). No plan change; the topic
  half had already landed at E3.1 under D62, so this task added the payloads to the same
  module as project-changes #20 said it would.
- **Gate:** 41, GREEN.
- **Tests:** backend 485 (+68, all in `test_mqtt_contracts.py`), vitest 96, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Command:** `make gate`
- **Artifacts:** `backend/app/contracts/mqtt.py` gained the spec 7.3 half — `MqttPayload` and
  its six bodies (`DesiredConfig`, `ReportedAggregatorState`, `ReportedListenerState`,
  `StatusMessage`, `DeviceEvent`, `Command`) with the nested `DesiredTarget`, `HealthBlock`
  and `ListenerLiveness`; the `UtcTimestamp` / `Checksum` / `MacAddress` / `EventCode` field
  types; `encode` / `decode` / `describe`; and `ContractError` as the shared base
  `TopicError` and the new `PayloadError` hang off. `guide/e3-verification.md` §3.
- **The acceptance criteria, in order.** Round-trip tests per model: fourteen parametrized
  payloads encode, decode and compare equal. Topic builders reject bad slugs and MACs: that
  half shipped at E3.1 and is unchanged. The module docstring names SIM as a consumer: it did
  already, and now says which half of the module each consumer reads.
- **The spec's own examples are a test.** `test_the_spec_7_3_examples_parse_as_written` feeds
  every JSON body printed in spec 7.3 through its model, so a later rename is caught as a
  contradiction with a named document rather than as a mystery.
- **Direction decides strictness (D67).** Payloads the platform publishes forbid unknown
  fields; payloads it receives ignore them. Firmware adding a field must never be able to
  stop the platform reading its reports, and an unexpected key on an outbound payload is a
  bug about to reach every device in a deployment. The same split explains the smaller
  rulings: `health.coarse` stays free text (inventing a vocabulary firmware has not agreed to
  would reject real reports for a field the platform does not even chart), while
  `expected_wake_at` is enforced present exactly while sleeping, both ways, because spec 6.5
  has the platform storing a declared wake time and never recomputing one.
- **One finding, found by writing the test rather than the code (D68).** `PayloadError` was
  documented as never carrying payload content, and `str(ValidationError)` does exactly that:
  for a missing-field error the "input" Pydantic echoes is the WHOLE body, config markers
  included. `decode` now builds its message from
  `errors(include_url=False, include_input=False, include_context=False)` — field names, no
  values — and a test feeds it a body whose `config` holds a `secret:` marker to prove it.
  E3.5 will log these, so the property had to be real rather than asserted.
- **The checksum bridge is tested, not assumed.** A snapshot with a bool, an int, a float, a
  null, a list and a nested object round-trips through `encode`/`decode` and produces
  byte-identical `canonical_config_bytes` — the property that makes a device-echoed checksum
  match by construction (D52, D55). `encode`'s null-omission deliberately stops at the model
  boundary: a null inside `config` is data, and stripping it would change those bytes.
- **Manual verification:** the walkthrough's new §3 run line by line in a REPL — the topic
  builder, `TopicError` on `redwood+coast`, a `DesiredConfig` encoding with
  `"schema_version":1` and a `...Z` timestamp, an equal round trip, both halves of the
  sleeping-Listener rule raising with the spec section in the message, a naive timestamp
  refused, an inbound extra field ignored while the same trick on `DesiredConfig` raised, a
  decode failure naming `checksum` while containing no trace of the `secret:` marker in the
  body, and three commands with three distinct `command_id`s. No stack needed; this task is a
  library.

## 2026-08-10: E3.2 MQTT client manager — reconnect, resubscribe, TLS from the row (Gate 40 GREEN)

- **Tasks closed:** E3.2 (branch `e3-batch-1`; DECISIONS D64-D66). No plan change: the task
  shipped as the phase document specifies it.
- **Gate:** 40, GREEN. `make gate`, run twice (the second run to read the counts cleanly).
- **Tests:** backend 417 (+21, `test_mqtt_manager.py`), vitest 96, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected.
- **Command:** `make gate`
- **Artifacts:** `backend/app/controlplane/` (new package — everything that talks to a broker;
  the wire contract stays in `app/contracts/mqtt.py`, which is published outside this
  codebase and this is not) with `broker.py`: `BrokerCoordinates`,
  `load_broker_coordinates`, `tls_context`, `Backoff`, `InboundMessage` and
  `MqttClientManager`. `conftest.ephemeral_broker` gained an optional fixed `host_port` plus
  `Broker.stop()`/`start()` and a `free_port()` helper; `aiomqtt` added as the one new runtime
  dependency (the phase-3 fixed client choice), async tests on anyio's pytest plugin (D66);
  `guide/e3-verification.md` §2.
- **The acceptance criterion is one test.**
  `test_a_broker_restart_is_invisible_to_message_handling` kills a real broker under a
  connected manager, restarts it, and asserts a message published afterwards reaches the
  handler that was registered once, before any of it, and was never told the connection
  dropped. It can only pass if the manager reconnected AND replayed its subscriptions, since
  a clean session leaves Mosquitto remembering nothing.
- **Docker re-assigns port 0 on every container start**, which the restart test found the
  moment it was written: a broker created with `-p 127.0.0.1:0:8883` comes back on a
  different host port, so the manager would have been dialling a dead socket and "failed to
  reconnect" would have been the test's own fault. Hence `free_port()` and the explicit
  `host_port` — the default stays Docker-assigned everywhere else, where nothing restarts.
- **Two rulings worth their own record.** A stored broker CA now REPLACES the public trust
  store rather than being added to it (D65): `create_default_context()` plus
  `load_verify_locations` reads like hardening and is the opposite, since every public root
  stays loaded and any public CA's certificate for the broker's hostname verifies too. And a
  handler that raises is logged while the loop keeps reading (D64) — one device's malformed
  payload must not cost a whole deployment its control plane, which is also why
  `InboundMessage` carries raw bytes rather than parsed models.
- **Nothing constructs the manager yet.** E3.2 ships the library; the worker (D59) wires it
  into a lifespan at E3.7. Said plainly here so a later session does not go looking for the
  call site.
- **Manual verification:** the walkthrough's new §2 run end to end against a real `eoe-qa`
  stack (five services up on 18000/15173/15432/16379/18883, seeded `--demo`, broker accounts
  provisioned, mosquitto restarted). The probe printed both brokers as
  `redwood-coast broker at localhost:18883` with no credential anywhere, connected to both
  over TLS verified against the CA on the `deployment_service` row, and delivered a device
  publish on `.../reported`. A retained platform publish to the same aggregator's `desired`
  topic produced **nothing** — the platform does not read its own writes back. Then
  `compose restart mosquitto`: `lost the ... broker`, `reconnecting ... in 0.9s (attempt 1)`
  for high-desert and `1.2s` for redwood-coast — visibly jittered, not in lockstep — then
  both reconnected and an `after` publish arrived on the same handler. One finding, fixed in
  the same pass: the §2 snippet showed no log lines at all, because nothing configures
  logging outside the API process; it now calls `logging.basicConfig` first. Stack torn down
  with `down -v`; `git status` listed nothing from `deploy/dev-certs/`.

## 2026-08-10: E3.1 development broker — TLS, per-device ACLs, `deployment_service` (Gate 39 GREEN)

- **Tasks closed:** E3.1 (branch `e3-batch-1`; DECISIONS D59-D63, project-changes #19,
  #20, #21). **First task of epic E3.** Preflight: this checkout was 20 gates stale — E1
  and E2 had merged (gates 20-38) while it sat at gate-18 — so the batch began with a
  fast-forward pull and `npm ci`.
- **Gate:** 39, GREEN. Three genuine RED runs on the way, all worth recording:
  (1) the ACL acceptance test asserted the broker would *reject* a denied subscription;
  it does not — Mosquitto returns SUBACK 0 and then silently never delivers, filtering
  wildcards per message. Every denial assertion was rewritten around message delivery
  with a paired positive control, which is a stronger test than the one I set out to
  write. (2) `docker cp` of a pytest tmp dir handed the container a 0700 directory that
  uid 1883 could not traverse; fixed at the source in `write_artifacts`. (3) The
  generator failed with a bare `PermissionError` because Docker had created
  `deploy/dev-certs` as root when an earlier `compose up` preceded the first generation
  — it now exits with an explanation and the exact removal command.
- **Tests:** backend 396 (+22: `test_dev_broker.py` 16, `test_mqtt_contracts.py` 23,
  minus reorganisation), vitest 96, Playwright 4; 0 failed / 0 skipped / 0 xfailed /
  0 deselected.
- **Command:** `make gate`
- **Artifacts:** `deploy/mosquitto/mosquitto.conf` (TLS-only listener, no 1883 anywhere,
  persistence on so retained desired survives a restart) and the `mosquitto` compose
  service; `backend/app/devbroker.py` — private CA, server cert, per-deployment platform
  accounts, per-Aggregator device accounts, the Mosquitto PBKDF2-SHA512 `passwd` file and
  the ACL file, all into gitignored `deploy/dev-certs/`, plus the `deployment_service`
  rows with passwords through `SecretStore`; migration `a41f9c7b2e05`;
  `backend/app/contracts/mqtt.py` (topic builders, landed here rather than E3.3 so the
  ACL generator builds grants from the namespace instead of repeating it — D62);
  `conftest.ephemeral_broker` (ships files with `docker cp`, not a bind mount, so WSL,
  Windows and Linux behave identically); `guide/e3-verification.md` §0-1.
- **The ACL is the point.** Each aggregator gets exactly seven grants and they are spec
  7.2's Direction column read literally, so a device can neither read a neighbour's
  subtree nor write its own `desired` topic — the latter would let it manufacture
  agreement and defeat drift detection before E3.7 ever runs.
- **Ports moved (project-changes #21, PHASE0-2-02, owner instruction):** the published
  HOST ports are now 18000/15173/15432/16379/18883. The standard numbers collided with
  services already running on the dev machine, and under rule R0 that is not an
  inconvenience but an unpassable gate — `test_compose_stack` and `test_verify_tool`
  bind them for real. Container-side ports are unchanged, so no image, process argument,
  broker listener or in-network URL moved.
- **Manual verification:** the walkthrough's §0-1 run end to end against a real
  `eoe-qa` stack — five services healthy on the new ports; `--certs-only` then seed
  `--demo` then the full generator (2 platform + 6 device accounts, 2 service rows);
  broker restarted. Then, in the container: the platform account published retained to
  `demo-agg-rc-02/desired` and swept it back off `eoe/redwood-coast/#`; `dev-demo-agg-rc-01`
  subscribing to that same topic received **nothing**; a client without `--cafile` got
  `Protocol error` (TLS-only proven); a wrong password got `Connection Refused: not
  authorised`. The `deployment_service` rows carried `mosquitto:8883` with `tls_enabled`
  true and named — never held — their `deployment:{id}:mqtt_password` secrets. `git
  status` listed nothing from `deploy/dev-certs/`. Stack torn down with `down -v`.

## 2026-08-04: E2.8 bulk edit UI + the E2 walkthrough — EPIC E2 COMPLETE (Gate 38 GREEN)

- **Tasks closed:** E2.8 (branch `e2-batch-3`; DECISIONS D58). **This closes epic E2**
  (gates 31-38, three batches, PRs #14/#15 open + #16 next).
- **Gate:** 38, GREEN — run twice deliberately: the first green run was followed by the
  manual browser walk, which caught a REAL layout defect the component suites cannot
  see (the secret cell's write-only Replace control overflowed its fixed-width cell;
  the neighboring cell swallowed its clicks). One CSS fix (flex-wrap), full gate
  re-run GREEN, walk re-run 8/8. The walkthrough exists precisely for this class.
- **Tests:** backend 357, vitest 96 (+8: bulk-edit suite), Playwright 4; 0 failed /
  0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** the S4 bulk-edit modal (`.modal-wide` two-pane modifier on the one
  modal vocabulary): catalog-driven change form with write-at-level consequence copy,
  live impact grid with the Offline-now E3 slot, server-computed preview table with
  no-op muting and the Status E3 slot column, and the **commit-gating acceptance**
  (Commit deep-equal-gated on the last previewed payload; any change re-disables).
  Checkbox multiselect on the pod listeners table (spec 5.2's simple path) feeding
  the modal via the explicit `{ids}` predicate; the saved-selections rail block in
  the config tree (opens BY REFERENCE, D54; no delete — GET/POST only).
  **`guide/e2-verification.md`** — the 11-section manual walkthrough (rules 1.1.0's
  FIRST subject), added to guide/README.md; **`guide/e1-verification.md` §9 amended
  in this same batch** (its "/configuration → empty state naming E2" and "effective
  config with E2" assertions were invalidated by E2.7 — exactly the same-batch
  amendment the rule demands).
- **Manual verification:** a scripted chromium walk of the walkthrough's core path
  against the REAL API (ephemeral postgres + uvicorn + vite, no MSW): sign-in →
  tree/tabs/rail → pod override staged/bannered/saved in one PUT → secret set
  write-only with plaintext provably absent from the DOM → listener inherits from
  Pod read-only (the at-or-above rule live) → bulk multiselect → preview → gating
  held through a form change → commit → draft revisions listed on the Revisions tab
  → zero [data-status] anywhere. 8/8 after the layout fix above; stack torn down.

## 2026-08-04: E2.7 schema-driven config editor (Gate 37 GREEN)

- **Tasks closed:** E2.7 (branch `e2-batch-3`; DECISIONS D57).
- **Gate:** 37 — first full run RED on `frontend: eslint` (Prettier formatting across
  the ten new/touched files; my standalone eslint check hadn't run the format half of
  the lint script). `prettier --write`, rerun GREEN. Three test-authoring fixes on the
  pre-gate vitest run (ambiguous text matches scoped to the chip class; a missing
  cleanup() between renders) — all test-side, no production changes.
- **Tests:** backend 357, vitest 88 (+28: config-editor 10, config-secrets 3,
  config-rbac 5, config-lib 9, shell +2 rows, inventory-levels +1), Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** the /configuration editor — shared-tree layout (`lib/hierarchy.ts`
  extraction; InventoryLayout repointed, behavior-neutral), five level routes incl.
  the aggregator's own, ContextBar tabs (first consumer; Settings/Tags/Revisions,
  S3's five folded per D57), the S3 provenance table on the .data-table vocabulary
  (quiet/loud rows, group headers with rationale captions, U+00D7 revert), catalog-
  driven editors for every type (bool = new ink-track ToggleSwitch; schedule = raw
  JSON v1; unknown types fall back safely), the **test-key acceptance** (fixture-only
  `test.demo_knob` grows a working editor, gate-pinned), secret rows (bullets,
  write-only Replace, diff says "replaced", plaintext-never-in-DOM test), local
  staging with the draft banner/diff and ONE wholesale PUT, per-key 422 landing,
  the inheritance chain with live ancestor counts, Field-Tech/viewer LOCKED treatment
  (aria-described), the D40 zero-[data-status] guard extended to config routes,
  Publish rendered disabled naming E3, and the ListenerDetail effective-config card
  with its Edit deep-link. Tokens: `--eoe-color-warning-border`,
  `--eoe-width-configrail` (+dark values same commit). The E1 shell Configuration
  page is deleted; shell.test's route table updated in the same commit.
- **Manual verification:** the component suites ARE the walk at this gate (the
  test-key acceptance, provenance walk, secret discipline, and RBAC treatments all
  assert against rendered DOM over the MSW fixture's real miniature merge); the
  full in-browser walkthrough is gate 38's deliverable (guide/e2-verification.md)
  per the E1 gate-27/28 precedent, honestly recorded here rather than claimed early.

## 2026-08-04: E2.6 bulk preview/apply + revisions read (Gate 36 GREEN)

- **Tasks closed:** E2.6 (branch `e2-batch-2`; DECISIONS D55-D56; project-changes #18
  with addendum PHASE2-4-02). This closes e2-batch-2 (E2.4-E2.6) — PR next.
- **Gate:** 36, GREEN — **the pre-gate run caught a real security defect**: the first
  plan implementation merged raw change values into the after-state, which put a
  secret's PLAINTEXT into the revision snapshot (storage itself was safe — only the
  snapshot leaked). The fix models secret changes as storage holds them (markers +
  keep-sentinel resolution) in the plan builder; the failing test now guards the
  invariant forever. Full gate green after the fix.
- **Tests:** backend 357 (+13), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `config_revision` table (migration `e8f61ab39c17`; per-device, un-FK'd,
  marker snapshots, state-string draft-only — the E3 handoff, published verbatim in
  INTERFACES). `app/config/plan.py` — ONE plan builder behind both endpoints (the
  preview==apply parity guarantee), write-at-level with common-ancestor resolution
  (422 naming candidates on a split; org level needs an org-wide grant), honest
  blast-radius device enumeration, no_op flagging. `POST /config/preview` (paginated,
  redacted, no CSRF) and `POST /config/apply` (one transaction: merged overrides +
  draft revisions + one config.apply audit row per affected deployment; response
  reports state draft + the inert `EOE_PUBLISH_ENABLED`). Revisions read surface
  (`app/api/revisions.py`): per-device lists (D7, -created_at, state filter,
  identical-404) + the snapshot-bearing item route. `EOE_PUBLISH_ENABLED` joined
  Settings + deploy/.env.example (the pairing test enforces both). Readiness locks:
  E0_ROUTES 65→70, E0_TABLES 16→17.
- **Manual verification:** ephemeral database + demo fixture over real HTTP — previewed
  a coastal-tag selection change (1 device, correct changed_keys, no_op false), applied
  (state draft, publish_enabled false, 1 revision), listed the listener's revisions
  (draft, -created_at), fetched the item (snapshot carries the new value; checksum
  `sha256:`-prefixed).

## 2026-08-04: E2.5 selection engine (Gate 35 GREEN)

- **Tasks closed:** E2.5 (branch `e2-batch-2`; DECISIONS D54).
- **Gate:** 35, GREEN — first full run (pre-gate check surfaced five long-line lint
  findings, auto-formatted, and four mypy findings from a reused statement variable,
  renamed per branch)
- **Tests:** backend 344 (+14), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/config/selection.py` — the spec-5.2 grammar as Pydantic models
  (all/any nesting; tag/eq/ne/in/exists/`ids` predicates; depth 5 / 50-predicate caps;
  secret value queries rejected, `exists` allowed) and the evaluator: SQL prefilter +
  in-Python predicates through the pure merge engine with batch-loaded chains (constant
  query count), ALWAYS re-filtered through the caller's visible deployments.
  `selection` table (migration `d1e53fa27b06`) stores the validated query verbatim —
  re-evaluated at every use, never a materialized id list. `app/api/selections.py`:
  POST /selections/preview (VIEW_STATUS, D7 envelope, deterministic order),
  GET /selections, POST /selections (CSRF + MANAGE_CONFIG-anywhere, 409 on duplicate
  names, audited) — GET/POST only per spec 13, no PATCH/DELETE (D54). Readiness locks:
  E0_ROUTES 62→65, E0_TABLES 15→16.
- **Manual verification:** ephemeral database + demo fixture over real HTTP —
  `{"tag": "coastal"}` matches exactly the fixture's one coastal-tagged listener
  (`alder-creek-01`); the default-sample-rate value predicate matches all 28.

## 2026-08-04: E2.4 effective and override endpoints (Gate 34 GREEN)

- **Tasks closed:** E2.4 (branch `e2-batch-2` opens batch 2 of the E2 plan).
- **Gate:** 34, GREEN — first full run (four long-line lint findings auto-formatted on
  the pre-gate check)
- **Tests:** backend 330 (+12), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/api/entity_config.py` — fifteen endpoints (GET effective, GET/PUT
  overrides × five entities) over three shared helpers; E1's scope discipline verbatim
  (org any-role read / org-wide-grant write, deployment 403-before-lookup,
  pod/aggregator/listener identical-404 — D35); every response redacted; PUT folds all
  validation errors into one 422 `validation_error` with detail.errors, staging
  nothing. Audit `config.override_update` carries set/unset KEY NAMES + catalog_version,
  never values. The four E1 DELETE endpoints now call `delete_overrides_for` and delete
  orphaned config secrets after their commit (D51 ordering). Readiness locks:
  E0_ROUTES 47→62.
- **Manual verification:** ephemeral database + demo fixture over real HTTP — owner
  session PUT wifi ssid + secret password on "Pod 01 · Alder Creek" (200, plaintext
  absent from the response body); `alder-creek-03` effective shows the ssid with
  source "pod", the password as the keep sentinel, and `identity.name` from inventory.

## 2026-08-04: E2.3 effective-config merge engine — test-critical suite locked (Gate 33 GREEN)

- **Tasks closed:** E2.3 (branch `e2-batch-1`; DECISIONS D52-D53). This closes
  e2-batch-1 (E2.1-E2.3) — PR next.
- **Gate:** 33, GREEN — the pre-gate check caught one real semantic failure first:
  the engine handed container values out BY REFERENCE, so mutating a result reached
  back into the chain. The locked suite's side-effect-freedom case is the documented
  semantic, so the ENGINE was fixed (containers copied on the way out), not the test.
  Full gate green after that fix plus minor lint formatting.
- **Tests:** backend 318 (+37: the locked merge suite 26 + service walk 9 + property
  extras), vitest 60, Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/config/merge.py` (pure core: effective_config /redact_secrets/
  resolve_secret_refs; deepest-setter-wins, wholesale replacement, inventory keys
  listener-only, defensive reads, no input aliasing — D53).
  `app/config/canonical.py` (the FROZEN checksum recipe: sorted-keys compact UTF-8
  JSON, `sha256:` prefix, markers included — D52; three golden digests pinned).
  `app/config/service.py` (ancestry via E1 FKs, one-query chain load, three accessors
  by audience: effective_for REDACTED for routers, effective_raw for E2.6 snapshots,
  effective_resolved INTERNAL ONLY for E3/E4). `hypothesis` joins the dev group
  (owner-approved) under a derandomized `gate` profile in conftest —
  `test_config_merge.py` is now one of the four suites no session may weaken, holding
  15 documented example semantics, 4 property cases, the golden checksums, and (in
  `test_config_service.py`) the JSONB round-trip stability guard.
- **Manual verification:** fresh ephemeral database + `seed_demo_hierarchy`: set
  `audio.sample_rate_hz=96000` on Redwood Coast, and `alder-creek-01` resolves it with
  source "deployment"; `identity.name` resolves from the listener row with source
  "inventory"; unset secret renders None; back-to-back checksums identical.

## 2026-08-04: E2.2 sparse override storage (Gate 32 GREEN)

- **Tasks closed:** E2.2 (branch `e2-batch-1`; DECISIONS D50-D51; project-changes #17
  with addendum PHASE2-4-01).
- **Gate:** 32, GREEN — first full run (three lint findings and one mypy finding on the
  pre-gate check were fixed before the gate; the gate itself passed first try)
- **Tests:** backend 281 (+19), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `entity_override` table (migration `c9d42be17a05`; singular per D30 —
  one sparse JSONB map per entity, UNIQUE(entity_type, entity_id), un-FK'd untyped
  entity_id per the audit precedent). `app/config/validation.py` — pure per-key
  validation, every error naming its key, all errors at once: unknown key, inventory-
  resolved (points at PATCH /listeners), service-restricted (names E5), the
  **owner-resolved level rule** (at-or-above lowest level, never below — the phase doc's
  inverted sentence recorded as deviation #17/PHASE2-4-01/D50), type/enum/range per
  class, null-never-a-value, 2 KiB object cap, bool-is-not-int. `app/config/overrides.py`
  — get/put (wholesale replace)/delete_overrides_for; secret keys store the
  `{"$secret": "config:..."}` marker with plaintext in SecretStore under the new
  `config:` namespace (flagged E0-contract extension, D51), keep-sentinel round-trip,
  post-commit deletion protocol. Readiness locks: E0_TABLES 14→15; the SecretStore
  round-trip gains both config name shapes (incl. the colon-bearing MAC form).
  INTERFACES gains the override-storage contract.
- **Manual verification:** fresh ephemeral database — secret put through the service:
  row holds only the marker, plaintext absent from the stored JSON, SecretStore
  round-trips the value under `config:pod:{id}:network.wifi_password`; below-level
  write rejected with the spec-citing message naming the key.

## 2026-08-04: E2.1 versioned settings catalog (Gate 31 GREEN)

- **Tasks closed:** E2.1 (branch `e2-batch-1`; epic E2 opens per the approved plan —
  owner decisions of 2026-08-04 recorded in DECISIONS D47-D49).
- **Gate:** 31, GREEN — first run
- **Tests:** backend 262 (+7), vitest 60, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/config/` package opens (the inventory-pattern: pure core beside
  DB service). `app/config/catalog.py` — the 37-row spec-5.3 `CATALOG` constant
  (`CATALOG_VERSION = 1`, merge-order `LEVELS`) and the upsert-plus-prune
  `seed_catalog()`. Migration `b7c31a90d2e4` creates `settings_catalog` (closed-vocab
  CHECKs) and seeds it in-migration; replays converge on the constant (D47).
  `GET /config/catalog` (any assignment) serves the schema document `{version, items}`
  sorted by key — deliberately not a D7 list (D47). Catalog facts: 6 secret rows, 12
  service-restricted rows (`telemetry.*` + 4 S3 keys; `upload.s3_prefix` deliberately
  writable, owner ruling — D48), 4 inventory-resolved rows (`location.*` + `identity.*`,
  D49). `test_settings_catalog.py` pins constant↔spec (hardcoded 37-key list) and
  constant↔table (field for field), seed idempotence/convergence, endpoint shape/sort,
  and the auth matrix. Readiness locks extended in-file: E0_ROUTES 46→47,
  E0_TABLES 13→14. INTERFACES gains "Owned by E2" (catalog contract + evolution rule).
- **Manual verification:** fresh ephemeral database migrated from scratch — 37 rows at
  version 1 (6 secret / 12 restricted), first/last keys eyeballed against the spec
  table; unauthenticated `GET /config/catalog` → 401. Migration up/down with data
  re-proven by the readiness replay inside the gate.

## 2026-08-04: Rules 1.1.0 — walkthrough currency joins R1 (Gate 30 GREEN)

- **Tasks closed:** the owner-directed rules amendment (branch `rules-batch-1`;
  DECISIONS D45). Docs/rules only; no production code or test changes.
- **Gate:** 30, GREEN
- **Tests:** backend 255, vitest 60, Playwright 4; 0 failed / 0 skipped / 0 xfailed /
  0 deselected
- **Command:** `./gate.ps1`
- **First run was RED, environmental — and self-referentially so:** the owner's manual-QA
  stack (`eoe-qa`, mid-walkthrough) held ports 5173/6379, and the gate's compose tests
  failed to bind (`Bind for 0.0.0.0:5173 failed: port is already allocated`) — exactly
  the D44 collision this repository documents. Remedy per D44: `qa-stack.ps1 down` (data
  volume kept, owner resumes with one `up`), full rerun GREEN.
- **Artifacts:** `.claude/rules/project-rules.json` 1.0.0 → 1.1.0 —
  `R1_record_keeping.verification_walkthrough`: every epic ships its own
  `guide/e{N}-verification.md` before its final gate, and amends prior walkthrough
  assertions it invalidates in the same batch that invalidates them. CLAUDE.md restates
  the rule. Rationale and mechanics in D45 (the qa-stack tooling tracks the product
  automatically; walkthrough prose does not — the F6/stale-handoff drift class, now
  closed by rule). PR #12 (the QA platform) was merged at the start of this batch.
- **Manual verification:** governance suite run standalone first (13 passed — the
  rules-JSON structure assertions are the risk point for this change); the E2 planning
  session in flight will be the rule's first live subject.

- **Tasks closed:** the owner-requested manual-QA platform (branch `qa-platform-1`;
  DECISIONS D44). Tooling + operator docs only; no production code or test changes.
- **Gate:** 29, GREEN
- **Tests:** backend 255, vitest 60, Playwright 4; 0 failed / 0 skipped / 0 xfailed /
  0 deselected (suite unchanged by design)
- **Command:** `./gate.ps1`
- **Artifacts:** `qa-stack.ps1` (repo root) — `up` builds and starts the documented
  compose stack under project `eoe-qa`, generates `deploy/.env` with fresh local secrets
  when missing (values never printed), seeds `app.seed --demo`, health-probes both ends,
  and prints the site URL + owner credentials with the boxed tear-down-before-gate
  warning (the recorded remedy for gate-15-class port collisions, D44); `down` keeps QA
  data, `reset` wipes it, `status` reports. `guide/e1-verification.md` — an 11-section
  checkbox walkthrough with expected results for every E1 feature (roll-up numbers, both
  themes, tree, tables, the full build-a-hierarchy CRUD walk with the conflict dialog's
  both paths, tag replace semantics, bulk import dry-run → gated partial accept with a
  paste-ready CSV, viewer/scoped-operator boundary probes) and a **shells audit**: one
  checkbox per deliberately-empty surface confirming it names its owning epic.
  `guide/README.md` TOC gains the walkthrough AND the previously missing
  `bulk-import.md` row, plus the one-command note.
- **Manual verification (the script proving itself):** fresh `up` from a clean volume —
  images built, all four containers healthy, demo seeded (2/6/6/28), credentials printed
  exactly once, health probes green; second `up` — idempotent "already seeded" path with
  no second credential print; `status` listed four healthy services; `down` removed
  containers, kept the volume, and freed the ports — after which this very gate ran
  GREEN, closing the loop on the warning the script prints.

- **Tasks closed:** E1.9, closing e1-batch-3 and with it **every E1 task** (E1.1-E1.9,
  gates 20-28; DECISIONS D43)
- **Gate:** 28, GREEN
- **Tests:** backend 255 passed (+2 in the new `test_seed_demo.py`), vitest 60,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `uv run python -m app.seed --demo` — one command, fully deterministic:
  "Earth Echoes Demo" with Redwood Coast (`redwood-coast`) and High Desert
  (`high-desert`), six named pods with aggregators `demo-agg-rc-01..03`/`hd-01..03`, 28
  listeners with locally-administered MACs, even-index GPS, first-listener pod tags.
  Fresh DB seeds owner + hierarchy with the password still printed exactly once;
  an existing owner gets hierarchy-only; an existing demo org refuses. The no-flag path
  is byte-identical to E0.12's (`test_seed.py` unchanged and green). The fixture is
  documented BY NAME in INTERFACES for E2/E6 and mirrored exactly by the frontend test
  fixture. `verify.py` gains an 11-step E1 hierarchy walk over real HTTP (one-call
  pod+aggregator, E1.4 reject/suffix pair, E1.7 tag replace, both D35 boundary checks,
  409-with-blockers, leaf-up teardown) with a children-first cleanup safety net.
  `guide/seed-script.md` documents the flag for operators.
- **Manual verification (including the walk carried from Gate 27):** an 11-check
  Chromium walk of the live dev stack (vite → uvicorn → seeded postgres) executed the
  E1.8 acceptance literally — logged in, verified the 28-listener hero and night theme,
  navigated the seeded tree, then **built a deployment → pod → listener hierarchy and
  ran a CSV import dry-run → partial-accept entirely in the UI**, exercising the
  conflict dialog's explicit suffix path live; zero `[data-status]` elements confirmed
  on the live inventory (D40). Screenshots retained in the session workspace. Seed
  determinism verified twice (subprocess suite + the walk stack).

- **Tasks closed:** E1.8 (+ the #16 Overview change), opening e1-batch-3 (DECISIONS
  D39-D42; project-changes #16; addendum PHASE1-4-04)
- **Gate:** 27, GREEN
- **Tests:** backend 253, vitest 60 passed (+16 across five new suites), Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** the nested `/inventory` surface on the fragment page shape — ContextBar
  (crumbs now real links, D41) over the 246px tree rail (36px rows, 14px/level indent,
  weight ladder, mono aggregator labels, CSS-drawn carets, route-tracking selection)
  beside four level pages. Tables run headless TanStack Table (D39, the epic's one new
  dependency) over the generalized `.data-table` vocabulary (D42; `.admin-table` retired,
  UsersAdmin repointed) with server-driven sort/pagination hitting the D7 wire grammar,
  mono identifier cells, and footer captions. First reusable `.form`/button vocabulary
  including the never-filled-red `.btn-danger`. The E1.4 conflict dialog consumes the
  {field, suggestion} detail and retries with auto_suffix only on the explicit click
  (suite-proven: never silent). The import screen runs dry-run-first with the partial
  commit structurally gated behind the row report + explicit checkbox; row outcomes are
  colored words, never device states. Overview is the V2·S1 roll-up with only E1-owned
  data (#16): hero "Listeners registered" from the D7 total, real deployment cards,
  honest EmptyStates naming E3/E5/E7. **D40's no-fabricated-status rule is
  gate-enforced**: tests assert zero `[data-status]` elements on every inventory route
  and the Overview. New tokens (danger-border + dark, indent-tree, space-px,
  duration-slow, width-treerail) additive per D21/D24.
- **Test changes (D42):** shell.test's route table +4 rows and the "/" heading row plus
  two auth.test assertions follow the recorded Overview retitle; nothing weakened, suite
  44 → 60.
- **Manual verification:** the five new vitest suites drive every flow end-to-end in
  jsdom against the real wire contracts (sort grammar captured off the requests, conflict
  dialog both paths, import dry-run → partial accept, viewer read-only); Playwright
  exercised the shell and both themes in real Chromium; the gate's compose-stack suite
  ran the live platform. **Not done at this gate:** a per-route browser walk of the new
  inventory surfaces — deliberately carried to Gate 28, where the E1.9 demo fixture
  seeds real data to walk against (the E1.8 acceptance "build a full hierarchy entirely
  in the UI" is verified there).

- **Tasks closed:** E1.7, closing e1-batch-2 (E1.6-E1.7, gates 25-26)
- **Gate:** 26, GREEN
- **Tests:** backend 253 passed (+5 in the new `test_tags.py`), vitest 44, Playwright 4;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `GET/PUT /{entity}/{id}/tags` on all five entities (listeners by MAC) —
  **PUT is wholesale replace, never merge**, storage normalized (trim/dedupe/sort) and
  validated (422 on >64 chars or control characters). Filter-by-tag proven on every
  entity list via GIN-backed containment. D35 scope rules carry over verbatim: viewers
  read, MANAGE_DEVICES writes, out-of-scope child items 404, org/deployment routes keep
  their 403 patterns. Tag writes audit as `<entity>.update {"changed": ["tags"]}`.
  E0_ROUTES += 10 (total E1 surface now 36 routes, matching the epic plan's spec-13
  parity list exactly). INTERFACES gains the tag storage model section E2's selection
  engine builds on.
- **Manual verification:** the tag → filter round trip walked on every entity level over
  the live API; replace semantics confirmed against the persisted rows.

- **Tasks closed:** E1.6, opening e1-batch-2 (DECISIONS D38)
- **Gate:** 25, GREEN
- **Tests:** backend 248 passed (+8 in the new `test_bulk_import.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `POST /listeners/import` + `POST /aggregators/import` (JSON rows or raw
  CSV; `?partial=`/`?auto_suffix=` on the query string; 1000-row/1 MiB limits). Always
  200-with-report `{committed, created, failed, rows}` — row error codes reuse the D8
  strings as data, the wire vocabulary untouched. All-or-nothing default proven to roll
  back rows AND the request's audit record; partial accepts commit valid rows only;
  per-row SAVEPOINTs make in-file and DB duplicates one code path (flushed rows are
  visible to later rows' collision checks, so in-file auto-suffix ladders correctly).
  Scope enforced per row (cross-deployment rows are row-level `forbidden`). CSV formats
  normative in INTERFACES; `guide/bulk-import.md` gives operators worked examples.
- **Defect found and fixed during the task:** FastAPI parses `application/json` bodies
  before validating against a `bytes` parameter, 422ing JSON imports while CSV worked;
  fixed by reading the raw body via an async dependency and dispatching on content type
  in the endpoint.
- **Manual verification:** the mixed-file dry-run → partial-accept flow walked over the
  live API; CSV and JSON parity checked from one fixture including GPS floats and
  pipe-separated tags.

- **Tasks closed:** E1.5, closing e1-batch-1 (E1.1 through E1.5, gates 20-24;
  project-changes #15, addendum PHASE1-4-03, DECISIONS D37)
- **Gate:** 24, GREEN
- **Tests:** backend 240 passed (+10 in the new `test_identity_service.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/inventory/identity.py` — services callable without MQTT, the exact
  seam E3.5 wires ("do not reimplement" recorded in INTERFACES with verbatim
  signatures). `handle_reported_identity` returns MATCHED / NAME_CONFLICT / MAC_CONFLICT
  / PROVISIONING_REQUIRED / UNKNOWN_MAC; conflicts quarantine the report and open a
  deduped `duplicate_identity` alert while the inventory row stays byte-identical
  (proven by full-column reload). `check_aggregator_membership` is a lookup, never
  sentinel equality; `require_known_aggregator` is the raising variant for ingest paths.
  Tables via migration `05c4858bfab5`: `quarantined_report` (append-only, deliberately
  no listener FK) and `inventory_alert` (open-alert dedupe via partial unique index
  `WHERE resolved_at IS NULL`, proven by raw insert; un-FK'd deployment scope). Alert
  types are data — the closed D8 wire vocabulary is untouched. System audit rows
  (`inventory.quarantine`, `inventory.alert`) with NULL actor. Services stage and never
  commit; the rollback probe proves an uncommitted session leaves nothing.
- **Manual verification:** migration round trip (`alembic check` clean at head,
  downgrade/upgrade clean); the full outcome table driven directly against a live
  ephemeral postgres; alert lifecycle (open → dedupe → resolve → fresh) walked
  end-to-end.

- **Tasks closed:** E1.4
- **Gate:** 23, GREEN
- **Tests:** backend 230 passed (+6 in the new `test_uniqueness.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** spec 4.3 item 1 implemented literally. Name collision within a
  deployment rejects by default — `409 conflict` with `detail {"field": "name",
  "suggestion": "<name-2>"}`, the wire shape E1.8's conflict dialog consumes.
  `auto_suffix: true` (explicit body parameter, default false — the never-silent rule is
  itself a test) creates at the first free `name-N` and the audit row carries
  `{auto_suffixed, requested_name, final_name}`. MAC collision always rejects; no
  parameter overrides it. The compute/flush suffix race retries once with a recomputed
  name (proven by a monkeypatched stale-suffix simulation), then 409s.
  `next_free_name` joins `app/inventory/naming.py`; INTERFACES documents the parameter
  and the suggestion detail shape.
- **Manual verification:** the full suffix ladder (`sensor` → `sensor-2` → `sensor-3`)
  driven over the live API; cross-deployment name freedom re-proven.

- **Tasks closed:** E1.3
- **Gate:** 22, GREEN
- **Tests:** backend 224 passed (+4 in the new `test_pod_aggregator.py`), vitest 44,
  Playwright 4; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `POST /pods` accepts an optional inline `aggregator` block — create-and-
  attach in one transaction with two audit rows and a single commit; the rollback proof
  shows a duplicate `aggregator_uuid` leaves neither the pod nor its audit row behind.
  `aggregator_uuid` platform-assigned (`uuid4().hex`) when omitted (spec 4.2); attach to
  an occupied pod stays 409 via `uq_aggregator_pod_id`. No new routes, tables, or plan
  changes — this is the spec-13 sentence implemented literally.
- **Manual verification:** both creation paths exercised over the API in the suite; the
  gate's compose stack ran the full platform live.

- **Tasks closed:** E1.2 (project-changes #13/#14; addenda PHASE1-4-01/02; DECISIONS
  D34, D35, D36)
- **Gate:** 21, GREEN
- **Tests:** backend 220 passed (+34: `test_hierarchy_crud.py` 12, `test_scoping.py` 22
  incl. parametrized visibility cases), vitest 44, Playwright 4; 0 failed / 0 skipped /
  0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **First run was RED, one mypy error:** `app\api\aggregators.py:65: error: Function is
  missing a return type annotation [no-untyped-def]` — every test suite passed on that
  run. Fixed with an explicit `-> tuple[Aggregator, uuid.UUID]`; full rerun GREEN.
- **Artifacts:** 24 routes (written into the readiness lock from the OpenAPI dump):
  organizations without DELETE and with the single-org POST clamp (D34), full CRUD for
  deployments/pods/aggregators/listeners with listeners addressed by normalized MAC in
  every path. D7 envelopes with per-entity query models and filters (parent FKs, name
  icontains, tag containment, slug/mac/aggregator_uuid); child counts embedded in
  serializers for the E1.8 tree. Slug generation with collision suffixing and the D36
  freeze-at-first-pod rule. `app/scoping.py` ships the reusable visibility layer (D35):
  org-wide grants see all, scoped grants see their deployments, no-grant users get 403;
  deployment item routes keep the 403-before-lookup pattern while child items answer
  byte-identical 404s for out-of-scope and missing rows — the MAC-enumeration oracle
  defense, asserted by test. Deletes 409 with named blockers, including role assignments
  on deployments (D33). Every mutation audits `<entity>.<verb>` with deployment scope.
- **Manual verification:** the gate's compose-stack suite drove the live API end-to-end;
  scoping proven against five personas (owner, org viewer, scoped operator, scoped field
  tech, no-grants) across all four list surfaces and the item asymmetry.

- **Tasks closed:** E1.1, opening batch 1 of epic E1 (branch `e1-batch-1`)
- **Gate:** 20, GREEN
- **Tests:** backend 186 passed (+14: 12 in the new `test_hierarchy_schema.py` constraint
  suite, the users-admin 422 test, and the readiness seam test splitting into two), vitest
  44 passed, Playwright 4 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** five singular-named tables (D30) — `organization`, `deployment`, `pod`,
  `aggregator`, `listener` — with every phase-doc fixed choice expressed as a database
  constraint: MAC as the listener primary key with a format CHECK (D31), one aggregator per
  pod via `uq_aggregator_pod_id`, listener names unique within their deployment via the
  set-once `deployment_id` stamp (D32), slug format CHECK + global unique, name-in-parent
  uniques, global `aggregator_uuid` unique, tags as GIN-indexed arrays and GPS columns
  front-loaded for E1.4-E1.7. Migrations `ee260dc1c1a8` (tables) and `53181716569c`
  (orphan-grant delete + role_assignment FK), both autogenerate-derived, hand-reviewed,
  with verified empty diff at head and a full downgrade/upgrade round trip. The E0.7 seam
  closed per D33: readiness seam test split into FK-present + audit-scope-never-FK halves,
  `test_rbac.py` fixture additively references real deployment rows (zero assertions
  changed), `/users` pre-validates assignment scopes (422), `verify.py` bootstraps and
  cleans a real `verify-dep-{tag}` deployment. `docs/INTERFACES.md` gains "Owned by E1:
  Entity schema" including the GPS-is-inventory-owned sentence E2 depends on.
- **Manual verification:** migration round trip run twice against an ephemeral postgres
  (`alembic check` reports no drift at head; `downgrade -2` then `upgrade head` clean); the
  gate's compose-stack suite ran the shipped verifier end-to-end over real HTTP, which
  exercised the new scoped-deployment bootstrap and cleanup live.

- **Tasks closed:** the records-and-test-hygiene batch a post-DES review of `23eff5d..f93f061`
  motivated (project-changes #11, #12; DECISIONS D28, D29; addenda PHASE0-4-05, PHASE0-4-06,
  DES-7-03). No product code changed.
- **Gate:** 19, GREEN
- **Tests:** backend 172 passed, vitest 44 passed (one added in `users-admin.test.tsx`),
  Playwright 4 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **First run was RED, environmental:** 8 failed + 59 errors, every one
  `could not start ephemeral postgres: failed to connect to the docker API at
  npipe:////./pipe/dockerDesktopLinuxEngine … check if the daemon is running` — Docker
  Desktop was not running. Started Docker Desktop, engine reported up, full rerun GREEN with
  no test or code change in between.
- **Artifacts:** `phase-0-foundations.md` gains PHASE0-4-05 (D25's sidebar→top-bar
  replacement, previously recorded everywhere except the phase document) and PHASE0-4-06
  (correcting PHASE0-4-04's "acceptance criteria continue to hold unchanged" — the one-file
  token-sheet criterion stopped holding at the gate that appended it). project-changes #11
  gives Gate 17's backdrop change its own numbered entry and records the in-place amendment
  of #9; DES-7-03 mirrors it in the handoff doc. D28 records the fifth Gate 16 test change
  D26's inventory missed (theme-swap e2e rewrite, net +2 tests). Stale claims corrected in
  `DES.4-handoff.md` (`Screens.dc.html` is in the repo since Gate 16) and
  `DES-track-handoff.md` (working-document framing replaces the frozen-snapshot claim).
  `docs/frontend-guide.md` is subordinated to `docs/INTERFACES.md` "Frontend composition and
  shared components" and gains the owner's "Starting E1" brief (tokens via `tokens.ext.css`
  only; closed six-state `StatusChip` vocabulary; v1 mockups for layout, v2 for values, none
  visually QA'd; replace `EmptyState` panels, don't restyle around them; `ContextBar` is the
  breadcrumb home; `.admin-table` is the table pattern to generalize; TanStack Table not
  installed). `images/README.md` gains an explicit provenance/license TODO for
  `forest-background.jpg`, pending owner input.
- **Test changes (D29), both strengthenings, both verified red-then-green:** the
  `test_governance.py` baseline guard was pointed at a bogus path and failed loudly
  ("planning-baseline should pin at least 7 documents, got 0") before restoration; the new
  viewer-sees-the-Users-link test was pointed at a bogus link name and failed with the
  viewer's rendered DOM showing the real link present. The misleadingly named "hides the
  sidebar link from a viewer" test (vacuous on its viewer half since E0.9) is replaced by
  two honest assertions of D25's intent.
- **Manual verification:** red-then-green runs above, plus the governance suite (13 passed)
  and `users-admin.test.tsx` (6 passed) run standalone before the full gate.

- **Tasks closed:** DES.4 "three rules" item 3 (fonts vendored, never fetched) and the DES
  track's handoff into E1 (project-changes #10; addendum DES-7-02); D27 (font vendoring and
  the status-glyph subset)
- **Gate:** 18, GREEN
- **Tests:** backend 172 passed, vitest 43 passed (6 new in `fonts.test.ts`), Playwright 4
  passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `frontend/public/fonts/` carries seven latin-subset woff2 files (~160 KB)
  declared by the new `frontend/src/styles/fonts.css`: IBM Plex Sans 400/500/600, IBM Plex
  Mono 400/600, Source Serif 4 600, and `eoe-status-glyphs.woff2`. Only weights the CSS
  actually uses are vendored. `frontend/tests/fonts.test.ts` gate-enforces the rule that made
  this a task at all — no `url(https:…)` or `@import` on any sheet, every `@font-face` src
  resolving to a committed file, every first-choice family in a `--eoe-font-family*` token
  supplied by a face, the glyph subset covering every status glyph token, and
  `.status-glyph::before` still naming the glyph family. `docs/INTERFACES.md` gains "Frontend
  composition and shared components": component contracts for `PageHeader`, `ContextBar`,
  `StatusChip`/`StatusLegend`, `EmptyState`, and `Can`, the two page-composition shapes, and
  the conventions E1.8 and E2 inherit. Each OFL license text ships beside the fonts.
- **The finding that shaped it (D27):** none of the six status glyphs — `●` U+25CF, `◐`
  U+25D0, `▲` U+25B2, `■` U+25A0, `✕` U+2715, `◆` U+25C6 — exists in IBM Plex Sans, IBM Plex
  Mono, or Source Serif 4, verified with fontTools against the **complete** families, not just
  these subsets. Vendoring the text faces alone would therefore have left the status
  vocabulary's shape channel to system fallback, and to tofu on a minimal air-gapped host
  (spec §15.1) — silently deleting one of the three channels status depends on. The shapes are
  now vendored too: Noto Sans Symbols 2 (OFL) subsetted to exactly those six codepoints, 568
  bytes, behind the additive token `--eoe-font-family-glyph`. This closes the Gate 16 caution
  about `◐` rendering as a hairline.
- **Manual verification:** the project owner ran `make gate` to completion (GREEN) and viewed
  the running compose stack in a browser, confirming the vendored typography renders as
  intended. **Not done at this gate:** a per-route screenshot pass in both themes, and any
  programmatic assertion that the glyph subset's shapes render distinctly at chip size — the
  gate checks the subset's declared coverage and wiring, not its rasterisation. Carry both
  into DES.8.
- **Known gaps:** the map engine is not built (E6 owns it). `Design System.dc.html` (the DES.1
  surface inventory and DES.5 component library) is not in this repository; the component
  contracts a session needs now live in `docs/INTERFACES.md` instead. DES.8's usability review
  stays blocked on E4–E6.

## 2026-07-30: Forest backdrop lands on every page (Gate 17 GREEN)

- **Tasks closed:** DES.7 asset follow-up — the image gap the Gate 16 entry recorded as a
  known deliberate gap is now closed (project-changes #9, amended)
- **Gate:** 17, GREEN
- **Tests:** backend 172 passed, vitest 37 passed, Playwright 4 passed; 0 failed / 0 skipped
  / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `frontend/public/images/forest-background.jpg` is committed and now backs
  **every** page through `.shell-content`, not just the login hero. One additive token
  carries it: `--eoe-color-backdrop-scrim` in `tokens.ext.css` (0.93) with its night value in
  `tokens.ext.alt.css` (0.94), held a notch heavier because a daylight forest photo is far
  brighter than `--eoe-color-bg`. The scrim is near-opaque on purpose — the photo reads as
  texture, the effective background stays within a hair of `--eoe-color-bg`, and every
  contrast ratio the token sheets document still holds. `.login-page` keeps the much lighter
  `--eoe-color-overlay` and lets the photograph carry the screen, since its only content is
  an opaque card. Both treatments paint a `background-color` first, so a missing file
  degrades to a flat token color rather than to unreadable text. `public/images/README.md`
  documents the two treatments and the resize command.
- **Asset decision:** the file is committed web-sized — 1920×1280, ~925 KB, EXIF stripped —
  rather than as the 6000×4000 / 24 MB camera original supplied. It is fetched on every page
  now, not one, and git keeps binaries forever. `-strip` also drops the EXIF capture location
  the original carried.
- **Manual verification:** `make gate` was run to completion by the project owner and
  reported green. **Not done at this gate:** per-route screenshots of the new backdrop in
  both themes. The change is CSS-only and fails safe (flat token color) if the asset is
  missing, but the scrim values are a visual judgement that has not been eyeballed across all
  eight routes — carry it into DES.8's usability review.
- **Known gaps, unchanged from Gate 16:** the map engine is not built (E6 owns it). Fonts are
  not vendored, so IBM Plex and Source Serif 4 render in their fallback stacks, and the
  `sleeping` glyph (`◐`, U+25D0) still wants re-checking once they are.

## 2026-07-30: DES.7 applied — V2 shell, night theme, page skeletons (Gate 16 GREEN)

- **Tasks closed:** DES.7 for the shell and frame (project-changes #9; addendum DES-7-01);
  D24 (night theme ships, D21's dark-palette gap closed); D25 (shell restructure; primary nav
  lists every destination); D26 (four test fixes at this gate)
- **Gate:** 16, GREEN
- **Tests:** backend 172 passed, vitest 37 passed (10 in `tokens.test.ts`, including new
  checks 8-10), Playwright 4 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `Shell.tsx` is now V2·S1's dark top bar with horizontal nav, replacing the
  E0.4 sidebar (`shell-sidebar` → `shell-topbar`); `app.css` is rewritten against the v2
  direction, still literal-free. New shared components: `ContextBar`, `PageHeader`,
  `StatusChip`/`StatusLegend`, `EmptyState`, `ThemeToggle`. The night theme ships:
  `tokens.alt.css` stops being a fixture and the new `tokens.ext.alt.css` carries dark values
  for every extension color key, both scoped to `:root[data-theme="dark"]` so specificity —
  not import order — decides the theme; `lib/theme.ts` resolves a persisted manual override
  ahead of `prefers-color-scheme`. New keys in `tokens.ext.css` on D21's terms:
  `--eoe-color-action-contrast-muted`/`-action-raised`/`-accent-on-action`/`-brand-mark`,
  `--eoe-radius-pill`/`-round`, `--eoe-height-topbar`/`-contextbar`. Routes and v2-styled
  skeletons added for Map, Inventory, Configuration, and Provisioning, each naming the epic
  that fills it — no mock data. `docs/INTERFACES.md` documents the fourth sheet and the shell
  shape.
- **Manual verification:** all eight routes screenshotted at 1440×900 in Chromium in both
  themes; no horizontal overflow on any page; theme toggle, its persistence across reload,
  and the relit status palette confirmed against computed styles.
- **Known gaps, deliberate:** the map engine is not built (E6 owns it; Google Maps satellite
  online / operator-supplied local image offline per spec §15.1, ESRI later). Fonts are not
  vendored, so IBM Plex and Source Serif 4 render in their fallback stacks. The login
  backdrop expects `frontend/public/images/forest-background.jpg`, absent today, and degrades
  to a flat token color. **Caution:** the `sleeping` glyph (`◐`, U+25D0) rendered as a
  hairline mark in headless Chromium's fallback font — the status vocabulary depends on the
  glyph channel, so re-check it once the real fonts are vendored.

## 2026-07-30: DES.4 v2 tokens, DES-4-01 additive extension accepted (Gate 15 GREEN)

- **Tasks closed:** DES.4 (v2 "field notebook" value set, plus the DES-4-01 additive
  extension) (project-changes #8; addendum PHASE0-4-04); D21 (DES-4-01 accepted); D22 and
  D23 (test fixes at this gate)
- **Gate:** 15, GREEN
- **Tests:** backend 172 passed, vitest 33 passed (7 in `tokens.test.ts`, including new
  check 7), Playwright 2 passed; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `make gate`
- **Artifacts:** `frontend/src/styles/tokens.css` and `tokens.alt.css` take the DES.4 v2
  value set (warm paper neutrals, Okabe–Ito status colors, IBM Plex type, real night theme)
  — same 30 property names, same five namespaces, values only. `frontend/src/styles/
  tokens.ext.css` (new, accepted under D21/DES-4-01) adds the six-state status vocabulary
  with color/tint/glyph, plus `--eoe-border-width-*`, `--eoe-row-height-*`,
  `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`; imported in `main.tsx`.
  `app.css`'s four `var(--eoe-space-1)` border/outline widths now use the new
  `--eoe-border-width-hairline: 1px`, fixing a 4px-instead-of-1px rendering defect. `docs/
  INTERFACES.md` "Design tokens" documents the extension; `project_planning/phase-0-
  foundations.md` gets addendum PHASE0-4-04. Two test fixes at this gate, both recorded:
  `frontend/e2e/theme-swap.spec.ts` no longer asserts on `fontFamily`/sidebar padding, which
  the real (not synthetic) night theme deliberately keeps identical to the light theme (D22);
  `backend/tests/test_governance.py`'s planning-doc immutability check now diffs only the
  documents that were actually part of `planning-baseline`, not every file currently in
  `project_planning/`, so new non-baseline material (the DES track's handoff docs, moved
  there and renamed at the project owner's direction: `project_planning/DES.4-handoff.md`,
  `project_planning/DES-track-handoff.md`) doesn't crash the check (D23). Known, explicitly
  deferred gap: `tokens.alt.css` does not yet carry dark-mode equivalents of the D21
  extension keys — real design work (per-pair contrast verification), not done in this batch.
- **Manual verification:** full local gate run twice — first run surfaced the two test fixes
  above plus an unrelated environmental port collision in `backend-tests`
  (`test_compose_stack.py`, `test_verify_tool.py`) against an already-running dev Compose
  stack on the same host ports; the project owner stopped that stack and reran themselves,
  confirming green, before this final `make gate` run was captured for the record above with
  no containers competing for ports.

## 2026-07-24: E0.12+ deployment verifier, USER guide, client-facing group (Gate 14 GREEN)

- **Tasks closed:** E0.12 extension (project-changes #7; addendum PHASE0-2-01)
- **Gate:** 14, GREEN
- **Tests:** backend 172 passed (the verifier gate test runs the shipped tool as a
  subprocess against a real compose stack: exit 0, every step PASS, temp accounts gone,
  TOTP secrets gone, audit rows surviving with nulled actors, no password material on
  stdout), vitest 32, Playwright 2; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `backend/app/verify.py` (`uv run python -m app.verify [--api URL]`):
  20-check owner-journey across health, auth, CSRF, the full TOTP lifecycle, user
  administration, RBAC deny paths for viewer and scoped operator, the audit trail, and
  live session revocation; guaranteed cleanup in a finally block; httpx promoted to main
  dependencies (D20). Top-level **`guide/`** client-facing group (layout change
  PHASE0-2-01): index README, getting-started, seed-script usage with implications,
  verify-deployment with implications; root README banner links it and demarcates docs/
  as engineering-internal.
- **Manual verification:** operator-style run against a fresh compose stack: 20/20 checks
  passed, "removed 3 temporary account(s); audit trail retained (immutable)", zero
  `verify-*` accounts left in the database. Clean teardown.

## 2026-07-24: E0-R readiness flight (Gate 13 GREEN)

- **Tasks closed:** E0-R (project-changes #6; addendum PHASE0-5-01) — the E0 exit-exam
  verifying E0.1 through E0.11 as a production-poised whole
- **Gate:** 13, GREEN
- **Tests:** backend 171 passed (14 new readiness tests), vitest 32, Playwright 2;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `backend/tests/test_e0_readiness.py`, organized by consumer epic as
  executable handoff documentation: locked surface contracts (exact route and table sets
  that later epics extend consciously; env-var documentation parity; OpenAPI operation
  completeness), E1 seams (un-FK'd nullable UUID scope columns awaiting E1.1's foreign
  key; MAC-wide audit entity ids; intact naming convention), E3/E4/E5 SecretStore consumer
  name shapes round-tripped, the E8.5 OIDC seam proven by minting a session for a user
  whose password can never verify, and production posture (data-seeded migration round
  trip to base and back; the prod nginx image proven to actually SERVE the built app, not
  just build; the API image proven non-root at UID 10001). Two defects found by designing
  the flight and fixed within it (D19): the compose frontend never received
  `VITE_API_BASE_URL` (the in-stack browser-to-API path had never worked), and the API
  container ran as root.
- **Manual verification:** through the real compose stack: `id -u` in the api container
  returned 10001; the frontend container carried `VITE_API_BASE_URL=http://localhost:8000`;
  health served 200 through the published port. Clean teardown.

Dated, reverse-chronological, append-only log of verified project state changes (rule R1,
`.claude/rules/project-rules.json`). An entry is written only after its gate passed with
0 failed, 0 skipped, 0 xfailed, 0 deselected, AND the task's manual verification steps ran.
Never in anticipation.

## 2026-07-24: Merge-blocking verification, protection confirmed absent

- **Event:** At the project owner's request, verified whether a failing `ci-green` actually
  blocks merging. Findings: `main` is unprotected (`protected: false`); the working
  account cannot change that (`admin: false`); a scratch draft PR (#4) with deliberately
  red checks was `MERGEABLE` with `mergeStateStatus: UNSTABLE`. Detection is fully
  functional; GitHub-side blocking is inactive until the repository owner applies the D17
  one-checkbox setting (require `ci-green` on `main`). Scratch PR closed and its branch
  deleted; the red run remains in Actions history as evidence. Until D17 is applied, merge
  discipline is procedural per rule R3.

## 2026-07-24: E0.12 Seed script (Gate 12 GREEN) — E0 build tasks complete

- **Tasks closed:** E0.12, closing batch 3 (E0.6 through E0.12) and with it every E0 build
  task
- **Gate:** 12, GREEN
- **Tests:** backend 157 passed (the seed acceptance test drives an empty unmigrated
  database through one command to migrated-and-seeded, parses the once-printed
  credentials, proves only the Argon2id hash is at rest, logs in as the org-wide owner
  over HTTP, and confirms re-seeding is refused), vitest 32, Playwright 2; 0 failed /
  0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/seed.py` (`uv run python -m app.seed`: migrates to head, creates the
  org-wide owner, prints credentials exactly once, audited as a system bootstrap, refuses
  re-seed); README dev setup updated (10 steps, at the budget)
- **Manual verification:** against the real compose stack: one seed command, the password
  printed exactly once, and the printed credentials logged in with HTTP 200. Clean
  teardown.
- **E0 definition of done (phase-0 section 5), swept:** compose-up to running API and
  frontend ✓ (lifecycle test at every gate); owner logs in with TOTP if enrolled, creates
  users, assigns roles ✓ (Gates 6/9/10); every mutation audits ✓; RBAC across all four
  roles with table-driven tests ✓ (Gate 7, test-critical suite); the secrets envelope
  round-trips and rotates under test ✓ (Gate 11); CI runs the full check suite ✓ (E0.5,
  every push); the shell renders entirely through the token sheet ✓ (discipline tests at
  every gate). Handoff artifacts: INTERFACES.md (every E0 section now concrete),
  DECISIONS.md (D1 to D18), the six-revision migration chain, the seed script,
  .env.example, and the token namespace delivered to the DES track at E0.4.

## 2026-07-24: E0.10 Optional TOTP (Gate 10 GREEN)

- **Tasks closed:** E0.10 (batch 3)
- **Gate:** 10, GREEN
- **Tests:** backend 156 passed (7 TOTP tests: full enrollment walk with wrong-code
  rejection, the acceptance triple at login — missing code 401 with totp_required, wrong
  code indistinguishable, right code 200 — unenrolled unaffected, secret only in
  SecretStore and never in responses post-enrollment, re-enroll and no-enrollment 409s,
  CSRF required, both mutations audited), vitest 32 (login page reveals and submits the
  code field), Playwright 2; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/api/totp.py` (enroll/confirm under /auth/totp); login TOTP gate in
  `app/api/auth.py`; `user.totp_enabled` via migration `0dd2c6d5b1d2` (hand-fixed: NOT NULL
  needs a server_default on populated tables — autogenerate review catching a real
  deploy-breaker); pyotp; login-page code field driven by the totp_required signal
- **Manual verification:** through the real compose stack: enrolled via curl (secret
  delivered once), confirmed with a computed code (200), login without a code returned the
  totp_required envelope, login with a live code returned 200. Clean teardown.

## 2026-07-24: E0.11 Platform secrets envelope encryption (Gate 11 GREEN, before E0.10)

- **Tasks closed:** E0.11 (batch 3; implemented before E0.10 per project-changes #5 /
  addendum PHASE0-4-03 so TOTP secrets ride SecretStore from birth)
- **Gate:** 11, GREEN
- **Tests:** backend 149 passed (8 secrets tests: KEK validation fail-loud, round trip and
  upsert, lifecycle, ciphertext-only at rest with fingerprint, isolated-database rotation
  with old-KEK rejection, tamper authentication failure, the acceptance log-grep, consumer
  docstring contract), vitest 31, Playwright 2; 0 failed / 0 skipped / 0 xfailed /
  0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/secrets.py::SecretStore` (AES-256-GCM envelope per spec 12.4, fresh
  DEK per write, KEK fingerprinting, `rotate_kek`), `secret` table migration
  `3f3b87c6623f`, store wired as `app.state.secret_store`, `EOE_KEK` validated fail-loud at
  startup (all test fixtures and the compose lifecycle env upgraded to structurally valid
  KEKs); INTERFACES SecretStore section
- **Manual verification:** scratch-database run: round trip OK, rotation re-wrapped and the
  new KEK read the value back, the old KEK was rejected with fingerprints only, and a
  DEBUG-level log capture contained no plaintext. Two silent-integrity catches during
  iteration, both fixed: a helper named `test_kek` was being collected as six phantom
  passing tests, and a blind rename briefly dropped the rotation test from collection.

## 2026-07-24: E0.9 User administration (Gate 9 GREEN)

- **Tasks closed:** E0.9 (batch 3)
- **Gate:** 9, GREEN
- **Tests:** backend 141 passed (10 new admin tests incl. the end-to-end acceptance flow:
  owner creates viewer over HTTP, viewer logs in, viewer 403 on administration; CSRF
  required on mutations; duplicate-email 409; unknown-role 422; audit row without secrets;
  deactivation kills the victim's live session mid-flight; password rotation; wholesale
  assignment replacement; self-lockout guards; D7 list with filters), vitest 31 (admin
  page, viewer denial, gated sidebar link, create flow, conflict surfacing), Playwright 2;
  0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/api/users.py` (every E0 mechanism on one surface: RBAC gate + CSRF +
  audit + D7 + D1 revocation + self-lockout guard); `UsersAdmin` page behind `<Can>`, gated
  sidebar link, users client; test-infra hardening: the ephemeral-Postgres fixture moved to
  conftest with unique container names and Docker-assigned free ports after an orphaned
  fixed-port container broke iteration (docker_cli/docker_env centralized in conftest)
- **Manual verification:** through the real compose stack: owner created
  `new-viewer@example.com` via POST /users (201, viewer assignment echoed, no secrets);
  the viewer logged in (200), read their own identity, and got 403 on GET /users. The
  audit-row check for user.create is gate-tested (a display-only shell quoting slip cut
  the manual audit printout; the API behaved correctly throughout). Clean teardown.

## 2026-07-24: E0.8 Audit log (Gate 8 GREEN)

- **Tasks closed:** E0.8 (batch 3)
- **Gate:** 8, GREEN
- **Tests:** backend 131 passed (8 new audit tests: login/logout rows with request-id
  correlation, transaction atomicity via rollback, D3 revoke presence, no mutating audit
  routes, owner-gated read with the D7 envelope, action/actor/scope filters, newest-first
  default), vitest 26, Playwright 2; 0 failed / 0 skipped / 0 xfailed / 0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** immutable `audit_log` table via hand-reviewed migration `c872acca01ec`
  with the reversible D3 `REVOKE UPDATE, DELETE`; `app/audit.py::record_audit` riding the
  caller's transaction; login/logout retrofitted (services now flush, endpoints commit once
  atomically with their audit row); `GET /audit` behind VIEW_AUDIT using the D7 contract;
  `script.py.mako` fixed to carry autogenerate imports (caught when the audit migration
  needed the JSONB dialect import); the `AuditQuery`-extends-`PageParams` pattern recorded
  as binding for all later list endpoints
- **Manual verification:** through the real compose stack: two logins and a logout produced
  exactly three audit rows; `?action=auth.logout` returned precisely the logout row with
  actor, entity, and request-id populated; default ordering newest-first; a revoked session
  querying /audit got 401 (the viewer-403 path is gate-tested). Clean teardown.

## 2026-07-24: E0.7 RBAC framework (Gate 7 GREEN)

- **Tasks closed:** E0.7 (batch 3)
- **Gate:** 7, GREEN
- **Tests:** backend 123 passed (37 new RBAC checks: 24-row table-driven permission
  matrix, no-assignment and union-of-assignments cases, all four roles against an
  org-level mutation and a deployment-scoped read over real HTTP with real sessions,
  scope isolation, invalid-scope 422, /me assignments), vitest 26 (can() mirror, Can
  component, cross-language parity), Playwright 2; 0 failed / 0 skipped / 0 xfailed /
  0 deselected
- **Command:** `./gate.ps1`
- **Artifacts:** `app/auth/rbac.py` (Role, Permission, ROLE_PERMISSIONS, pure
  `has_permission`, `require_permission` factory with optional path-param scoping);
  `role_assignment` table via hand-reviewed migration `658a7e1ad594` (nullable un-FK'd
  scope per phase-0, NULL = org-wide); `/auth/me` returns assignments; frontend `can()`
  mirror + `<Can>`/`useCan` gating helper with a gate-enforced parity test against the
  Python source; INTERFACES RBAC section. This is the first of the four spec-14.5
  test-critical suites.
- **Manual verification:** through the real compose stack: seeded an owner with an
  org-wide assignment; login and `GET /auth/me` returned
  `assignments: [{role: owner, deployment_id: null}]`. One red gate during development:
  the vocabulary parity test's unanchored parse matched the module docstring and compared
  against an empty list; fixed with anchored bounds plus non-emptiness guards so an empty
  parse can never silently pass.

## 2026-07-24: E0.6 Local accounts and sessions (Gate 6 GREEN)

- **Tasks closed:** E0.6 (batch 3)
- **Gate:** 6, GREEN
- **Tests:** backend 86 passed (9 new auth tests incl. login/logout/expiry/wrong-password
  acceptance paths against a migrated ephemeral Postgres, plus signing/hashing primitives),
  vitest 19 (login page, session affordance), Playwright 2; 0 failed / 0 skipped /
  0 xfailed / 0 deselected; ruff, mypy strict (16 files), eslint, prettier, tsc clean
- **Command:** `./gate.ps1`
- **Artifacts:** `user` + `session` tables via hand-reviewed autogenerated migration
  `c07e17281417` (convention-named constraints, real downgrade; the E0.2 migration suite
  covers it automatically); Argon2id hashing; HMAC-signed opaque session cookie (D1);
  double-submit CSRF (D4); login/logout/me endpoints under the envelope contract with
  indistinguishable bad-credential 401s; `get_db`/`require_session`/`require_csrf`
  dependencies for E0.7+; token-styled login page, shell session affordance, MSW auth
  handlers; `EOE_SESSION_TTL_SECONDS` documented; INTERFACES auth section filled
- **Manual verification:** through the real compose stack with curl: login returned the
  user JSON and set both cookies; wrong password returned the 401 envelope; `/me` 200 with
  the session; logout without the CSRF header 403, with it 204; `/me` after logout 401
  (immediate revocation); the password string appeared nowhere in responses or the cookie
  jar. Stack torn down clean.

## 2026-07-24: E0.5 acceptance completed, CI green on main

- **Event:** PR #2 merged to `main` (merge commit `ea70563`); the push run `30118657466`
  on `main` completed with every job green. The phase-0 E0.5 acceptance criterion "CI green
  on main" is satisfied. "A failing test blocks merge" is mechanically proven by the
  red-path run (30117111859) and becomes enforced the moment the repository owner applies
  the one-time D17 setting (require the `ci-green` check on `main`).

## 2026-07-24: E0.5 CI pipeline (Gate 5 GREEN)

- **Tasks closed:** E0.5 (batch 2; PR #1 merged to main beforehand)
- **Gate:** 5, GREEN
- **Tests:** backend 77 passed (10 new CI-structural checks), vitest 14, Playwright 2;
  0 failed / 0 skipped / 0 xfailed / 0 deselected; all quality stages clean
- **Command:** `./gate.ps1`
- **Artifacts:** `gate.sh` refactored into the canonical stage registry (7 stages,
  per-stage invocation, `--list`, unknown-stage rejection); `.github/workflows/ci.yml`
  (7 jobs each invoking one registry stage, `ci-green` fan-in with fail-not-skip
  semantics, push+PR triggers, per-ref concurrency cancel, uv/npm/Playwright caching,
  Postgres service container for the literal D9 reversibility commands, BUILD_SHA from the
  commit SHA); `backend/tests/test_ci_pipeline.py` (two-way registry/workflow parity,
  fan-in completeness, checkout history/tags, migrations and containers coverage,
  extension-recipe documentation); INTERFACES "CI pipeline" section (3-step add-a-stage
  recipe); DECISIONS D15 (CI shape) and D16 (LF line-ending pin after the Gate 5 red run);
  `.gitattributes` with history renormalized
- **Manual verification:** registry proven by hand: `--list` prints all seven stages,
  unknown stage exits 2, `migrations-check` without DATABASE_URL exits 1, `backend-quality`
  correctly went red on an unformatted file during development. **Live-run verification
  complete:** first workflow run 30116966780 on `e0-batch-2` succeeded, all 8 jobs green in
  about 2.5 minutes wall clock, with the migrations job executing the literal D9
  reversibility commands against the service container (D9 closed). **Red-path proof:**
  scratch branch `ci-red-proof` with a deliberately failing test produced run 30117111859:
  `backend-tests` and `backend-quality` failed, unrelated stages stayed green, and
  `ci-green` ran and FAILED rather than skipping; run conclusion `failure`; branch deleted,
  run retained in Actions history as evidence. **Branch protection:** API attempt returned
  404 (admin required; account has WRITE); exact owner instructions recorded as D17.

## 2026-07-24: E0.4 React skeleton with neutral design tokens (Gate 4 GREEN)

- **Tasks closed:** E0.4 (closes the E0.0 to E0.4 batch)
- **Gate:** 4, GREEN
- **Tests:** backend 67 passed, frontend vitest 14 passed, Playwright 2 passed; 0 failed /
  0 skipped / 0 xfailed / 0 deselected across all stacks; ruff, mypy strict, eslint,
  prettier, tsc all clean
- **Command:** `./gate.ps1`
- **Artifacts:** routing (Overview, System, 404) inside a token-styled layout shell
  (sidebar plus content); TanStack Query provider with a health query; the binding token
  sheet `frontend/src/styles/tokens.css` (five namespaces, neutral theme) plus the
  test-only `tokens.alt.css` mirror; API client on `VITE_API_BASE_URL` (fail-loud, D2);
  MSW handlers so the frontend tests run with no backend; ESLint flat config with
  no-explicit-any as error plus Prettier; vitest suites (token discipline, shell/routing/
  query, api client); Playwright theme-swap suite; frontend prod image still builds
- **Manual verification:** through compose from outside the harness: the dev server served
  the shell HTML and the token sheet with `--eoe-*` definitions; the Playwright run proved
  in a real browser that loading the alternate value set changes computed background,
  color, font family, and spacing with zero code changes (E0.4's literal acceptance
  criterion). Clean teardown.

## 2026-07-24: E0.3 FastAPI skeleton (Gate 3 GREEN)

- **Tasks closed:** E0.3
- **Gate:** 3, GREEN
- **Tests:** 67 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected; ruff, mypy strict
  (9 source files), frontend typecheck clean. New suites: settings precedence (6), API
  skeleton (17), pagination contract (8); compose lifecycle strengthened to prove the
  versioned health endpoint, API-to-Postgres reachability, and the 404 envelope through the
  real stack
- **Command:** `./gate.ps1`
- **Artifacts:** `app/main.py::create_app` factory (uvicorn `--factory` mode);
  `app/settings.py` (env > TOML > default per D5, fail-loud, alias-named errors);
  `app/errors.py` (envelope as the only error shape, D8 vocabulary frozen, AppError rejects
  unknown codes at raise time); `app/middleware.py` (request-id middleware plus
  log-record-factory binding, security-header baseline); `app/api/health.py` (build/version/
  db-ping payload); `app/api/pagination.py` (D7 contract: PageParams, ListResponse,
  parse_sort, apply_page); CORS-with-credentials from `EOE_CORS_ORIGINS`; Dockerfile
  BUILD_SHA arg and versioned healthcheck; DECISIONS D14
- **Manual verification:** through compose from outside the harness: health returned
  `build_sha` injected via the BUILD_SHA build arg and `database: ok`; unknown path returned
  the exact envelope with `not_found`; inbound `X-Request-ID` echoed with security headers
  present; CORS returned allow-credentials, the configured origin, and exposed X-Request-ID.
  Clean teardown.

## 2026-07-23: E0.2 Postgres and migrations (Gate 2 GREEN)

- **Tasks closed:** E0.2
- **Gate:** 2, GREEN
- **Tests:** 36 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected, zero warnings
  (migration suite: chain integrity, non-trivial-downgrade AST check, filename template,
  conventions doc, plus upgrade/downgrade/round-trip/empty-autogenerate-diff/constraint
  naming against a real ephemeral Postgres); ruff, mypy strict, frontend typecheck clean
- **Command:** `./gate.ps1`
- **Artifacts:** `backend/app/db.py` (binding MetaData naming convention plus `Base`);
  `backend/alembic.ini` (no URL stored; `path_separator = os`); `backend/alembic/env.py`
  (targets `app.db.Base`, URL exclusively from `DATABASE_URL`); reversibility-annotated
  `script.py.mako`; baseline root migration `4a07fe3a8e54` (deliberately empty, the single
  downgrade exemption); `docs/migration-conventions.md`; Gate 2 suite
  `backend/tests/test_migrations.py`; DECISIONS D13 (ephemeral Postgres via direct docker
  run, amending D6)
- **Manual verification:** by hand against a scratch Postgres container: `upgrade head`,
  `downgrade -1`, `upgrade head`, `downgrade base`, `upgrade head` all succeeded and
  `alembic current` reported `4a07fe3a8e54 (head)`; container removed cleanly.

## 2026-07-23: E0.1 Repository and container scaffolding (Gate 1 GREEN)

- **Tasks closed:** E0.1
- **Gate:** 1, GREEN
- **Tests:** 27 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected (integration
  included: compose lifecycle up, HTTP probes, pg_isready, clean teardown, prod image
  build); ruff check, ruff format, mypy strict, and frontend `tsc --noEmit` all clean
- **Command:** `./gate.ps1`
- **Artifacts:** fixed monorepo layout (`backend/app` typed ASGI placeholder, `frontend/`
  minimal Vite React TS app, `deploy/`, `sim/`, `docs/`); `backend/Dockerfile` (Python 3.12
  plus uv); `frontend/Dockerfile` (multi-stage: dev Vite server, prod nginx static build per
  D2); `deploy/docker-compose.yml` (api, frontend, postgres, redis on ports 8000/5173/5432/
  6379, healthchecks, fail-fast env interpolation); `deploy/.env.example` (names only);
  `README.md` (dev setup in 9 steps); Gate 1 suites `test_repo_layout.py` and
  `test_compose_stack.py`; `gate_runner.py` refactored with fail-closed `enforce()`
- **Manual verification:** from outside the test harness: `docker compose up -d --wait`
  brought all four services healthy; `curl` returned the API placeholder JSON on 8000 and
  HTTP 200 on 5173; `down -v` removed everything. Compose fail-fast interpolation observed
  firing on a deliberately missing `EOE_KEK`. Two red-gate infra fixes recorded (D12 docker
  credential-helper PATH; first run's failure output preserved in the session log).

## 2026-07-23: E0.0 Governance scaffolding (Gate 0 GREEN)

- **Tasks closed:** E0.0 (task added by project-changes #1)
- **Gate:** 0, GREEN
- **Tests:** 15 passed / 0 failed / 0 skipped / 0 xfailed / 0 deselected; ruff check and
  ruff format both clean
- **Command:** `./gate.ps1`
- **Artifacts:** `.claude/rules/project-rules.json` (rules R0 to R3); `CLAUDE.md` rules
  loader; `docs/DECISIONS.md` (D1 to D11); `docs/project-changes.md` (#1 to #4);
  `docs/INTERFACES.md` skeleton; four addenda across `project_planning/`; `.gitmessage`
  wired as `commit.template`; `gate.ps1`, `gate.sh`, `Makefile`; backend test scaffold
  (governance suite, git-hygiene suite, `tests/gate_runner.py`)
- **Manual verification:** R0 guard proven end to end: a deliberately skipped test makes
  `tests/gate_runner.py` exit 1, and removing it returns exit 0 (this check exposed and
  fixed the pytest 9 exitstatus defect recorded as D11). Planning documents verified
  byte-identical to the `planning-baseline` tag after addendum stripping.
