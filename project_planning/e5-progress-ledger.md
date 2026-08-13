# E5 Progress Ledger

Per-unit state for epic E5 (Deployment services onboarding), maintained as the epic is
implemented. The phase document (`phase-5-deployment-services.md`) is the binding scope; this
file is the running record of where the work is, so a session joining mid-epic does not have to
reconstruct it from git.

**The gate cadence for this epic is not R0's default, and the deviation is deliberate.** The
owner authorized targeted tests per unit and the full `make gate` at five checkpoints (C1-C5)
rather than after every numbered unit, for wall-clock reasons; it is recorded in
`docs/DECISIONS.md`. The compensating discipline is absolute and preserves what R0 protects:
**nothing reaches the remote without a full green gate.** Commits between checkpoints are local
and unpushed. A checkpoint is green only when `make gate` passed the ENTIRE accumulated suite —
0 failed, 0 skipped, 0 xfailed, 0 deselected — and only then is anything pushed or tagged.

**Gate numbers are decided at tag time, not pencilled.** The SIM epic is being built in
parallel and its numbering moves. Each checkpoint takes `max(existing gate-* tags) + 1` and
records the actual integer here.

**Branch:** `e5-batch-1`, one PR for the whole epic, worked in a separate git worktree so the
SIM sessions keep the primary tree.

| Unit | Status | Checkpoint | Decisions | Notes |
|---|---|---|---|---|
| E5.0 Phase document and records | **done (gate-54)** | C1 | D104-D108 | Phase doc, this ledger, project-changes entries, plan §3 / phase-4 §2 / spec item 14 addenda. Docs only. |
| E5.1 Services data model | **done (gate-54)** | C1 | D109 | Migration `a31287354e23`. Widened `service_key` to five, conditional `mqtt` CHECK, `config`/`secret_names` JSONB, five status columns, `deployment.services_status`, `app/services/store.py`. Fixed the `DELETE /deployments/{id}` 500. One additive E3-owned edit in `controlplane/broker.py`: a row missing its connection columns is skipped, not fatal. Targeted: `test_services_model.py`, 13 passed. |
| E5.2 Write-only secrets API | **done (gate-54)** | C1 | D110 | `app/services/schemas.py` (five `extra="forbid"` models, `KeepSecret`, pure `plan_write`, `redacted_settings`) and `app/api/services.py`. `MANAGE_SERVICES`/`VIEW_SERVICES` added to `rbac.py`, `rbac.ts`, `test_rbac.py` and `rbac.test.tsx` — additions only, the test-critical matrix is not weakened. D110: the PUT is a partial collection of wholesale members and has no delete. `E0_ROUTES` in `test_e0_readiness.py` extended by the two routes. Targeted: `test_services_api.py` 24 passed, plus `test_rbac`/`test_api_skeleton`/`test_audit`/`test_scoping`/`test_e0_readiness`/`test_services_model` green; ruff, `mypy app`, `tsc --noEmit` clean. |
| **C1 full gate** | **GREEN, gate-54** | — | — | 839 backend / 115 vitest / 4 Playwright, 0 failed / 0 skipped / 0 xfailed / 0 deselected. Backend stage 262.7s. Carried E5.3 as well. Manual verification ran against a real uvicorn (see project-updates). |
| E5.3 Connection test framework | **done (gate-54)** | C2 | D111 | `app/services/testers/{__init__,base}.py`: `ServiceTester` protocol, `TestResult`/`CheckResult` with a required remedy, `ServiceCredentials` (secrets `repr=False`, the D66 precedent), the concurrent runner with per-tester **and** whole-call budgets, `resolve_credentials` (candidate beats stored, sentinel reaches back), and `POST .../services/test`. D111: four outcomes, two of which are not failures. `REGISTRY` ships EMPTY — E5.4a-e fill it. `E0_ROUTES` extended by the one route. Targeted: `test_service_testers.py` 25 passed; ruff, `mypy app` clean. |
| Concurrency-safe harness adopted (not an E5 unit) | **done (local)** | C2 | D113 | `conftest.py` and `test_mqtt_manager.py` taken verbatim from the SIM branch's `959ff23`. This worktree was cut before that commit and was still running the pre-fix harness. Retires D112's interim rule. E0-owned infrastructure, imported not authored. Targeted: `test_dev_broker.py` + `test_mqtt_manager.py`, 40 passed. |
| E5.4a MQTT tester + dynsec probe | **done (local)** | C2 | D114, D115, D116 | `app/services/dynsec.py` (created one unit early on the owner's decision, D115), `clients/mqtt.py`, `testers/mqtt.py`, `REGISTRY["mqtt"]`. Three checks on one connection: connect, round trip on `eoe/{slug}/_selftest`, dynsec. **The probe's discriminator is the SUBACK, not the publish (D114)** — an `acl_file` broker grants the subscribe and silently refuses the publish, so the intuitive test would report `denied` for the dev broker where the phase doc requires `absent`. `conftest.dynsec_broker` + `ephemeral_broker(conf=…)` + `Broker.logs()`. Targeted: `test_tester_mqtt.py` 24 passed; ruff, `mypy app` clean. **Independently re-verified by a later session** (24/24 still green, changes to gated files confirmed additive) — that re-run is what found **D118**. |
| E5.4b InfluxDB 3 tester | **done (local)** | C2 | D119 | `clients/httpbase.py` (the shared HTTP dial + failure taxonomy for b/c/d), `clients/influx.py`, `testers/influx.py`, and **the container rig** (`conftest.service_rig`): five containers started in PARALLEL on Docker-assigned ports, one session fixture on one xdist group, **8.3s to ready**. HTTP query API, no `pyarrow`. Influx 3 creates a database on first write, so the rig seeds one — without it the happy path would correctly read `not_found`. D119: importing the `rig` fixture built it three times; measured and fixed. `test_tester_influx.py` 12 passed. |
| E5.4c Prometheus tester | **done (local)** | C2 | — | `clients/prometheus.py` + `testers/prometheus.py`. Two rig containers, one with `--web.enable-remote-write-receiver` and one without, because the flag has no runtime equivalent. Measured: 204 accepted / 401 rejected / 404 receiver-disabled — and **401 on BOTH builds**, since Prometheus checks basic auth before it routes, which is why the probe reads 401 before 404. The probe sends an EMPTY remote-write body and leaves no series; asserted either way as the phase doc requires. `test_tester_prometheus.py` 10 passed. |
| E5.4d Grafana tester | **done (local)** | C2 | — | `clients/grafana.py` + `testers/grafana.py`. Health (unauthenticated, so it separates "not a Grafana" from "wrong token"), datasource enumeration, contact-point registration. Idempotence diffed against **Grafana's own listing** across two runs. Provisioning is a separate deliberate call and a test run creates nothing — asserted. Contact point targets `POST /webhooks/grafana-alerts`, **E7.6's to build**, pinned in a test as well as a comment. `test_tester_grafana.py` 11 passed. |
| E5.4e Object storage tester | **done (local)** | C2 | — | `clients/s3.py` + `testers/s3.py`, boto3 through `asyncio.to_thread`. `forbidden` vs `not_found` asserted against a real MinIO user carrying a deny-all policy, with the bucket's existence proven in the same test. Reserved prefix empty after a pass, asserted by listing. **`not_required` keys on both credentials being absent** — see the note below, this is a reading of spec 16.2 rather than a quotation. `boto3` added to deps; mypy override for its missing stubs, `strict` relaxed nowhere else. `test_tester_s3.py` 12 passed. |
| E5.5 Services status lifecycle | **done (local)** | C2 | D117, D118 | `app/services/status.py` (`roll_up`, `recompute` as its only writer, `DEGRADE_AFTER_FAILURES = 2`, `apply_test_results`, the re-check sweep as a **callable that E5.7b registers** so no third E3-owned edit is taken), `GET .../services/status`, and the save path unverifying a service whose credentials just changed. **D117: `deployment_service.required` is a stored column** (migration `b7d41f0c2e93`), not an argument to `roll_up` — the save path and the invariant sweep recompute with no test results in hand, so a parameter would make the denormalized column irreproducible from its own rows. A test of CANDIDATE credentials is not a verdict of record and writes no status. Targeted: 130 passed across `test_services_status`, `test_services_model`, `test_services_api`, `test_service_testers`, `test_tester_mqtt`, `test_migrations`; ruff, `ruff format`, `mypy app` clean. |
| **C2 full gate** | **GREEN, gate-57** | — | D117, D118, D119 | 942 backend / 115 vitest / 4 Playwright, 0 failed / 0 skipped / 0 xfailed / 0 deselected. Backend stage **279.06s**. **The first C2 run was RED** and is recorded honestly in project-updates: three failures, none of them contention — the committed-secret scanner tripped on three `*_PASSWORD = "..."` constants, `E0_ROUTES` had never been extended for E5.5's status endpoint, and E5.4a's registry test still asserted `set(REGISTRY) == {"mqtt"}`. Two were latent in `09b5271`, which was committed after running only its own test file. Manual verification ran against a real uvicorn (see project-updates). |
| E5.6 Broker credential minting (dynsec) | **done (local)** | C3 | D120, D121 | `app/services/credentials.py` (the `BrokerCredentialProvider` protocol E4.6 imports, `DynsecCredentialProvider`, `DevBrokerCredentialProvider`, `drain_pending_revocations`), `broker_credential` (migration `c4e9b21f83da`), three routes on `/aggregators/{id}/broker-credential`, and `aggregator_acl_grants` extracted from `devbroker.acl_file_text`. **D120: one grant list, two renderers** — `read` becomes TWO dynsec acltypes, and only `publishClientReceive` beside `subscribePattern` gives a device messages rather than a silent subscription. **D121, owner's call: three states.** An unreachable broker leaves `revoke_pending` and never blocks a device delete; the sweep finishes it. `DELETE /aggregators/{id}` became `async` (its one E1-owned edit). Targeted: `test_broker_credentials.py` 26 passed against a real dynsec broker, plus 90 across the eight affected suites; ruff, `mypy app` clean. |
| E5.7a Projection and privileged write | **done (local)** | C3 | D122, D124 | `app/services/projection.py` (`PROJECTION` asserted against `CATALOG` at import), `allow_write_restricted` through **four** signatures not three (D122 — `apply_change_plan` calls `put_overrides`), the E2-owned `changed_keys` fix (D124), and `publisher.publish_all` with both callers on it. The flag also carries fixed choice 3's wholesale regeneration, so a cleared optional field leaves the projection instead of surviving forever. `PUT .../services` became `async`. Targeted: `test_service_projection.py` 21 passed, plus 147 across nine E2/E3 suites; ruff, `mypy app` clean. |
| E5.7b The two authorized E3 edits | **done (local)** | C3 | D125, D126 | `MqttClientManager.refresh()` + `_begin`/`_cancel`, refresh loops in BOTH hosts (`runner.py::_refresh_loop`, `main.py::_refresh_forever`), `_async_sweep_loop`, and **two** sweep registrations — `service-config` (body in `app/services/config_sweep.py`) and `broker-credential` (D121's retry). **D125 states the whole E3-owned surface taken and what was not.** Two settings + `.env.example`. D126: a retained desired message carries secret MARKERS, and the suite now pins E5's projection to that boundary. Targeted: `test_broker_refresh.py` 9 passed against a real broker, plus 126 across seven E3 suites; ruff, `mypy app` clean. |
| **C3 review + full gate** | — | — | — | Cross-epic diff reviewed INLINE at the owner's instruction (2026-08-12), not by a subagent. |
| **INFRA.1 Warm container pool** (not an E5 unit) | **done (gate-60)** | — | D128, D129 | E0-owned test infrastructure, landed before C4 on the owner's decision (project-changes #29, addendum PHASE5-2-03). `ephemeral_postgres` keeps its signature and stops being a container: one machine-wide warm Postgres on tmpfs, migrations run ONCE into a template keyed by a fingerprint of the migration DIRECTORY (not the head id — two branches can share a head with different bodies), and `CREATE DATABASE ... TEMPLATE` per caller at **0.017s against 4.02s**. Mosquitto/Prometheus/Grafana/MinIO state also moved to tmpfs. `test_container_pool.py` (7 tests) asserts the contract rather than assuming it. **Breaks phase-5's additive-only-in-conftest rule deliberately**; SIM imports it the way this worktree imported SIM's fix at `167aa6e`. Cost two real defects, both found by running the gate: tmpfs mounts default root-owned (37 rig errors, fixed with `mode=1777`) and a latent Prometheus self-scrape race that Grafana's old 14s startup had been hiding. D129: brokers and rig containers gained `_start_ephemeral_postgres`'s whole-container retry, because removing 55 startups removed the pacing that kept D99's forwarder fault rare. `make testpool-down` closes the pool by hand. |
| E5.8a Broker material extraction | **done (local)** | C4 | — | `app/brokerconfig.py` takes `Account`, `AclGrant`, `aggregator_acl_grants`, `password_hash`, `password_file_text` and `generate_tls_material`; `devbroker.py` keeps its CLI, `acl_file_text` and the database side, and re-exports the moved names through `__all__` so **`test_dev_broker.py` passes unchanged** (30 tests, untouched by this batch). `Account` moved too, one name wider than the phase doc's list, because `password_file_text` consumes it and a shim would have left the password format in two places. `credentials.py` now imports the grant list from its new home. `generate_tls_material` gained `hostnames`/`ips` with E3.1's values as defaults, and refuses an empty hostname list rather than building a certificate with no CN. Plus the generated broker: `dynamic_security_config` (platform admin only — devices are minted at provisioning time, and a bundle cannot ship credentials it cannot revoke) and `stack_mosquitto_conf`. mTLS comment in `deploy/mosquitto/mosquitto.conf` amended to name E8 instead of E5. **Acceptance met: the generated conf and dynsec JSON start a real broker through `ephemeral_broker(conf=…)` and E5.4a's probe answers `available`.** Targeted: `test_brokerconfig.py` 28 passed, `test_dev_broker.py` 30 passed unchanged, plus 137 across eight affected suites; ruff, `ruff format`, `mypy app` clean. |
| E5.8b Compose and service configs | **done (local)** | C4 | D130 | `app/services/stack.py`: `StackSpec`/`StackSecrets`, `compose_file`, `prometheus_yml`/`prometheus_web_config`, `grafana_datasources`/`grafana_contact_points`, `render_configs`, and the README (`deploy/stack-templates/README.md` prose + a generated port table). Every YAML built as a dict and `yaml.safe_dump`ed; `mosquitto.conf`/`dynamic-security.json` imported from E5.8a rather than copied. **`pyyaml` promoted from the dev group to runtime dependencies** — `app/` now imports it — and `types-pyyaml` added for `mypy --strict`. **D130: `dynamic_security_config` now takes an already-hashed password.** It salted a fresh hash per call, so two renders differed and fixed choice 7's byte-identical download was already broken before E5.10 could assert it; `dynsec_password_hash` does the salting once and E5.9 stores the result. Found by writing the determinism test, not by reasoning. All three acceptances met: `docker compose config` exits 0 for both shapes (proven falsifiable — a corrupted file returns 1 with `services.broken must be a mapping`), README/compose ports asserted in both directions off the rendered artifacts rather than the shared constant, and no dev-conf literal in any generated file. Targeted: `test_stack_generator.py` 37 passed, plus 132 across six suites; ruff, `ruff format`, `mypy app` clean. |
| E5.9 Stack credential generation | **done (local)** | C4 | D131 | `app/services/stackgen.py`: `generate_stack` (generate → store → write rows → commit, all before a byte renders), `load_generated_stack`, `tls_material`, `delete_stack_secrets`, `STACK_SECRET_ITEMS`. **No `deployment_stack` table** — fixed choice 7 says the download re-renders "from those rows", so every generation parameter is recovered from stored state: object storage is *whether an `s3` row exists*, the hostname is the `mqtt` row's `host`, the CA is its `ca_cert_pem`, and the rest lives under deterministic `deployment:{id}:stack:*` secret names kept separate from E5.2's per-service names so a hand save cannot clobber them. `bcrypt` added as a dependency (Prometheus's `web_config.yml` takes bcrypt and nothing else; user auth stays on argon2). **`SecretStore.put` commits on its own session, so "zero secrets after a fault" is compensation, not a shared transaction** — and the honest limit (a kill between put and rollback leaves harmless unreferenced ciphertext) is stated in the module docstring. **D131: the first compensation was destructive.** It deleted every name it wrote, but regeneration overwrites the SAME deterministic names, so a failed rotation wiped the working stack it was replacing; it now snapshots and restores prior values and deletes only genuinely new names. Found by the test, not foreseen. All three acceptances met. Targeted: `test_stack_generation.py` 15 passed, plus 129 across six suites; ruff, `ruff format`, `mypy app` clean. |
| E5.10 Stack bundle endpoints | **endpoints done (local); keystone RED** | C4 | — | `POST .../services/stack` + `GET .../services/stack/download`, `app/services/bundle.py` (deterministic tar.gz: entry order, mtime, uid/gid, uname/gname and the gzip header all pinned — `tarfile`'s `w:gz` cannot set the gzip mtime, hence the explicit `GzipFile`). Both routes `MANAGE_SERVICES`; download audit detail is the byte count and nothing else; a tempfile spy proves nothing is written server-side. `E0_ROUTES` extended by both. Fixed a defect found by reading rather than running: the compose file mounted `./prometheus/scrape_password`, which `render_configs` never produced, so Docker would have made a directory and Prometheus would have scraped nothing — `StackSecrets` now carries the Prometheus password in both forms (hash for `web_config.yml`'s incoming auth, plaintext for the outgoing self-scrape). Then the keystone found the permissions defect: the bundle shipped `server.key` at 0600 and **Mosquitto would not start** (`Unable to load server key file`), because these files are bind-mounted into containers that drop privileges. Only `.env` is 0600 now. Targeted: `test_stack_endpoints.py` 14 passed, 66 across three stack suites. |
| E5.10 keystone: the four failures it found | **done (local)** | C4 | D132, D135 | The keystone was RED on arrival and every failure was a real gap in the generated stack, not a test defect — it is the one test in this epic that runs the artifact instead of inspecting it, and it has now paid for itself four times over. **mqtt:** `dynamic-security.json` wrote the platform password as one `encoded_password` `$7$` group, which **Mosquitto 2.0's plugin does not read** — it wants `password`/`salt`/`iterations` separately, ignores the combined form silently, and refuses every connect with CONNACK 135 naming nothing. The reason nothing caught it is worse than the bug: the fixtures ran `eclipse-mosquitto:2`, which Docker Hub has moved to 2.1.2, while `IMAGES` pins 2.0.20 — **a pinned artifact tested against a floating tag proves nothing about what ships** (D132, project-changes #30). **grafana:** a generated stack has no service account token, and E5.9 stored the ADMIN PASSWORD under the `service_account_token` name; resolved as a stop-and-ask, see the E5.10b row. **s3:** nothing created the bucket the phase document's E5.8b line asks for. **influx:** `INFLUXDB3_AUTH_TOKEN` configures the CLI and not the server, so Influx 3 minted its own admin token and 401'd the platform's — `serve --admin-token-file` is the offline form that lets the token be generated and committed *before* the stack exists (fixed choice 7). Both fixed with short-lived init containers (`influx-init`, `minio-init`, `restart: "no"`, retry loops rather than healthcheck conditions) and a sixth image, `minio/mc`, since `minio/minio` carries no shell (D135). Targeted: `test_stack_keystone.py` 1 passed — the bundle unpacks, comes up in ~14s, and **all five E5.4 testers pass against it**. |
| E5.10b Grafana service-account bootstrap | **done (local)** | C4 | — | `app/services/provision.py` + `GrafanaAdminClient`: a service account token is issued by Grafana at runtime and shown exactly once, so it cannot be generated ahead of the stack the way every other credential is. The platform generates an admin account, and the FIRST verification uses it once as a bootstrap — create `echoes-platform` with the Admin role, have Grafana mint its token, store that as the deployment's Grafana credential. Every test afterwards sends the scoped token; the admin password stays in SecretStore until a rotation needs it. **E5.4d's rule that provisioning is never a side effect of a test still holds** — `GrafanaTester.run` writes nothing, and an operator who supplied their own service account token never reaches this module. Targeted: `test_grafana_bootstrap.py` 12 passed, including minting twice leaving exactly one account. |
| E5.11 Rotation and regeneration | **done (local)** | C4 | D133, D134 | `POST .../services/stack/rotate`: regenerate through E5.9 → re-render → re-verify (E5.3) → republish (E5.7a). Rotating a deployment that never generated a stack is a **404 and not a silent generate**, so a mistyped id cannot mint a fresh stack for the wrong deployment. **D134: rotation was invisible to devices and the measurement is why.** A desired snapshot carries secret MARKERS, and a marker is a SecretStore NAME — the same string before and after — so rotating every credential minted **zero** revisions while rotating to a different hostname minted one per Aggregator and none per Listener. The projection was working; there was nothing to say. `services.credentials_generation` (catalog key, `write_restricted=SERVICE_ONBOARDING`, column + migration `d5f28c60a419`) is the non-secret counter that changes — E2-owned surface plus a migration, taken **only on the owner's explicit decision**, and `write_restricted` is what keeps the "zero for Listeners" half true. **The publish happens BEFORE the re-verification decides anything and unconditionally**, which is the acceptance's inverted order and carries the comment saying so: the likeliest reason re-verification fails is that the operator has not restarted the stack yet, which is exactly when the devices need the new credentials. Left `degraded`, honestly, and the revisions go out. `verified` is never optimistic — generation puts every service back to `untested`. **No sweep is registered (D133).** Targeted: `test_stack_rotation.py` 16 passed. |
| **C4 full gate** | **GREEN, gate-61** | — | D132-D139 | 1139 backend / 115 vitest / 4 Playwright, 0 failed / 0 skipped / 0 xfailed / 0 deselected. Backend stage **242.05s** against INFRA.1's 224.15s baseline — **+17.9s for 133 new backend tests** (1006 → 1139), including the keystone's full `docker compose` bring-up, which the rig design paid for in advance by BEING the generated stack. **The first C4 run was RED with eight failures and is recorded honestly in project-updates:** six were one root cause (D134's catalog key, whose ripple nobody had run — two constants in `test_settings_catalog`, the frozen merge checksum, the provenance count, and the restricted-set identity), one was the missing `PHASE5-4-04` addendum that `test_governance` correctly refused, and **one was a real broker leak masquerading as a flake (D138)**. Two later runs were **invalid measurements**, not red gates: a `/forwards/expose` container fault (D99/D112), and the two fixed-port compose suites colliding with the manual-verification stack still holding 15432/16379 — self-inflicted, so the cause was removed rather than the run repeated. **Manual verification ran against the full compose stack and a real uvicorn, and found D139**, which 1138 green tests did not. |
| E5.12a Wizard UI: Path A | not started | C5 | — | Schema-rendered forms, per-service result rows with remedy. |
| E5.12b Path B, gating, walkthrough | not started | C5 | — | S5 layout at v2 values. `guide/e5-verification.md` plus e3 amendments in the same batch. |
| **C5 full gate + PR** | — | — | — | |

## Measured gate durations

| Point | Warm duration | Note |
|---|---|---|
| Baseline (pre-E5) | ~260s | Reported at gate-53, before SIM.1 landed. |
| **C1 (E5.0-E5.3)** | **262.7s backend stage** | gate-54. Essentially flat against the baseline: this batch added 73 backend tests and no container fixture. The rig arrives with E5.4b, and the ~300s ceiling in phase-5 §5 is measured from here. |
| **C2 (E5.4a-e, E5.5)** | **279.06s backend stage** | gate-57. **+16.4s over C1 for the whole rig**, against the ~300s ceiling: five containers, 103 new backend tests (839 → 942). The design held — the rig is built ONCE per gate, and D119 records the run where it was not (three builds, invisible, +27s on four suites alone). Container-test scope did not have to be cut. **The margin is now ~21s**, so E5.8b's compose-config tests and E5.10's keystone bring-up are the next things to measure, not to assume. |
| **INFRA.1 (the warm pool)** | **224.15s backend stage** | gate-60. **-66s against C3's 299.16s, on 1006 tests rather than 999** — the seven added are the pool's own contract suite. Containers created per gate **193 → 65**; disk written per gate **4.05 GB → 0.69 GB**. **Section 5's ~300s ceiling is superseded and this is the new baseline** (addendum PHASE5-2-03): C4 measures against 224.15s, and the margin it has to spend is real rather than notional. The remaining writes are NOT container churn and were attributed before stopping: `docker build` accounts for ~638 MB (400 MB in `test_e0_readiness`, 238 MB in the `containers-build` stage) and the shipped compose stack's named volumes for 184 MB in `test_compose_stack` + `test_verify_tool`; the whole service rig now writes 30 MB and 186s of broker-heavy tests write 19 MB. Cutting further means not proving the images build or not bringing the shipped compose file up. |
| **C3 (E5.6, E5.7a, E5.7b)** | **299.16s backend stage** | gate-59. **+20.1s over C2** for 57 new backend tests (942 → 999), and the ~300s ceiling in phase-5 §5 is now **reached rather than approached**. Nothing new is container-heavy — C3 reuses `dynsec_broker` and `ephemeral_broker` and adds no fixture — so the increase is the tests themselves plus two real-broker modules. **E5.8b's compose-config tests and E5.10's keystone bring-up now have NO margin to spend**; the next batch either measures first or raises the ceiling deliberately. The three slowest tests in the suite are still E3's (`test_end_to_end_loop` at 91s each) and unrelated to this batch. |

## Notes for whoever picks this up next

- **The green C4 gate still shipped a bug that manual verification caught in minutes (D139).**
  The once-a-minute service-config sweep was resetting `services.credentials_generation` from
  N back to 0 and minting a revision to publish the reset, because `service_settings` omits the
  key when no generation is passed and the effective config then falls back to the catalog
  default. **A rotation's signal to its devices survived less than a minute**, and D134's whole
  purpose with it. Every existing sweep test used a deployment whose counter was 0, where the
  bug is invisible — which is why 1138 green tests did not see it and running
  generate → rotate → regenerate through a real uvicorn did. **The fix belongs AFTER the
  sweep's early return**, not in the `service_settings` call: putting it there makes the
  projection never empty and costs a full change plan per deployment per minute. Both halves
  are now pinned by tests.
- **A rare failure in a well-worn concurrency test was a real leak, not a flake (D138).** C4's
  first gate failed `test_shutdown_leaves_no_running_tasks` once, with
  `socket=LIVE state=MQTT_CS_CONNECTED`; it then passed 3/3 in isolation and on every later
  gate. **The instinct to write that off as flakiness — or to soften the test's
  fail-immediately-on-a-live-socket rule with a grace period — would have deleted the detector
  and kept the bug.** The bug: a cancellation inside aiomqtt's `__aenter__` abandons
  `enter_async_context` before the client is registered on the stack, while paho's executor
  thread finishes connecting anyway, stranding a CONNECTED client that `stop()` cannot close.
  `_open_client` is the fix and is the mirror of D94's `_close_client`. **The third
  discretionary E3 edit, taken on the owner's authorization** (project-changes #34, addendum
  PHASE5-4-06); a fourth is still a stop-and-ask. The reproduction is forced, not awaited, and
  was measured failing with the shield removed before it was believed.
- **A catalog key is never a one-file change, and this is the measured blast radius.** D134's
  thirteenth key was written correctly and its own suites passed, but the unit was committed
  without running the rest — and the first C4 gate came back with **six** failures in suites
  nobody had thought about: `test_settings_catalog` (two constants: the spec-key set AND the
  write-restricted set, plus `len(CATALOG)`), `test_config_merge` (the frozen golden checksum),
  `test_config_endpoints` (the per-key provenance count), and `test_service_projection` (the
  restricted-set identity). **The exact lesson the ledger already recorded for E5.4a — run the
  affected suites, not only the new ones — cost a second gate anyway.** If you add a catalog
  key, grep the suite for `== 37` and for the key sets before you run anything.
- **The frozen merge checksum can move, and there is now a right way to move it (D137).** Do
  not regenerate a golden digest from current behaviour. The re-freeze ships with an assertion
  that removes the newly added key and requires the ORIGINAL digest back byte for byte, so the
  new constant is pinned to "the old snapshot plus exactly one key" rather than to whatever the
  code now emits. That is what separates a recorded addition from a silently laundered
  regression, and it is the pattern to copy if it ever has to happen again.
- **`test_repo_layout.SECRET_PATTERNS` matches an uppercase constant NAME, not just a value,**
  and it caught four sites in this epic at once: `SERVICE_ACCOUNT_TOKEN_NAME`,
  `INFLUX_TOKEN_MOUNT` (a filesystem PATH), and two test-account `PASSWORD` constants. The
  regex is `(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY)\w*\s*[=:]\s*"[A-Za-z0-9+/_-]{20,}`, so any
  identifier CONTAINING one of those words before the `=` trips when the value is 20+
  URL-safe characters. **Rename the constant or shorten the value; do not touch the scanner** —
  it cannot tell a field name from a field value, and blunting it to save a nicer identifier
  trades a real guarantee for cosmetics. Note the scanner asserts on the FIRST offending file
  it meets, so a green-looking fix may just be revealing the next one; scan the whole tree in a
  loop instead of re-running the gate per site.
- **Baseline-protected planning documents take SINGLE-LINE addenda only.** The spec, the plan,
  the handbook and the phase-0..4 documents are pinned by the `planning-baseline` tag, and
  `test_governance._strip_addenda` removes only lines matching `^> **Addendum ID (date, ref
  project-changes #N):** ...$` — one line, however long. A multi-line blockquote fails as an
  unauthorized modification, and **editing the body at all fails even when the edit is
  obviously correct**: adding the thirty-eighth row to spec 5.3's own table was rejected, and
  rightly. The addendum IS the row. `phase-5-deployment-services.md` is NOT baseline-pinned (it
  postdates the tag), which is why its addenda are multi-line blockquotes — do not copy that
  shape into the spec.

- **THE KEYSTONE IS GREEN, and all four of its failures were real.** It is committed now:
  the generated bundle unpacks, comes up under `docker compose` in ~14 seconds, and **all five
  E5.4 testers pass against it**. None of the four was a test defect and none was fixed by
  weakening the test — see the E5.10-keystone row for what each one was (D132 mosquitto
  password fields and the floating fixture tag, D135 the Influx admin-token file and the two
  init containers, and the Grafana bootstrap that became E5.10b). **Do not weaken it later
  either.** Its whole value is that it is the only test in this epic that runs the artifact
  instead of inspecting it, and it has now paid for itself four times over — plus the 0600
  private key that stopped Mosquitto booting and the missing `scrape_password` before that.
- **Two cheap lessons from building it,** both already written into its comments so they are
  not re-learned: Compose merges `ports` by CONCATENATION, so a `docker-compose.override.yml`
  that remaps a port ADDS a binding rather than replacing it (the fixed port still binds and
  still collides); and Mosquitto prints no startup banner under the generated conf's
  `log_type` set, so log-scraping for readiness waits forever on a broker that is serving
  perfectly well — wait on the port, as `conftest._wait_until_accepting` does.
- **E5.11 is done, and the sweep question is closed.** Rotation was mostly the endpoint, the
  re-verify and the republish, because `generate_stack` was already idempotent — the one thing
  it needed that did not exist was a non-secret counter, D134, without which rotation is
  invisible to every device. **OWNER DECISION, 2026-08-12: no periodic re-checks, ever** —
  timed polling is stale and the platform should fail fast and loudly off real liveness
  instead, so E5.11 registers no sweep and spec 16.5's item is closed as *deliberately not
  built*. `services_recheck_sweep` stays as an on-demand bulk re-test invoked by an operator
  action, never a scheduled job, and its docstring says so. Written up in **D133 and
  project-changes #31 / addendum PHASE5-4-03.**
- **D127 is fixed and closed (D136, project-changes #33 / addendum PHASE5-4-05).** The
  owner authorized it as an E0-owned fix taken by E5. `install_root_handler` in
  `app/middleware.py` is called by BOTH `create_app` and `runner.py::main`, so "which
  processes log" is one fact rather than two beliefs; pinned by a test at INFO, not WARNING,
  because WARNING worked throughout and is what disguised the bug for three epics.

- **ANSWERED (2026-08-12, owner): what makes object storage "not required".** The question was
  that spec 16.2 and section 721 make object storage conditionally required on "raw-audio upload
  enabled" and **there is no such toggle** in the settings catalog, so E5.4e keyed `not_required`
  on the only observable fact available: both S3 credentials absent. **The owner kept that
  reading.** The platform supports raw audio only for now; an operator who is not uploading
  leaves the credentials blank, and a half-entered form still fails loudly. **No
  `upload.raw_audio_enabled` catalog key is added** — that would be an E2-owned catalog change
  plus a migration and out of E5's scope under rule R2. The alternatives declined were making
  object storage unconditionally required (which would block every non-uploading deployment from
  ever reaching `verified`, and therefore from generating a bundle under spec 16.5) and adding
  the toggle. DECISIONS D123, project-changes #28. Nothing in the code changed; the note at the
  top of `app/services/testers/s3.py` stands as written.

- **CLOSED (was: the API logs nothing below WARNING, D127).** Fixed in this batch on the
  owner's authorization — see the D136 note above. C3's manual walkthrough still proves
  `refresh()` behaviourally (draft before the broker row, `pending` after one refresh
  interval, same process) rather than by grepping the log, and that is worth keeping: it is
  the stronger assertion of the two whether or not the logging works.

- **CLOSED (was: spec 16.5's periodic re-checks are not running).** Not a gap any more and not
  a later unit's problem — the owner closed the question as *deliberately not built* (D133,
  project-changes #31). `status.py::services_recheck_sweep` keeps its single caller-less state
  on purpose: it is an on-demand bulk re-test for an operator action to invoke, and the E5.5
  notes and the INTERFACES entry that once said E5.7b would register it on a timer were
  already corrected (D125). **Do not "finish" this by adding a scheduler.**

- **Cross-branch record collision, for whoever merges.** `docs/project-changes.md` numbers
  **#24, #25 and #26 twice** — once on `e5-batch-1` (E5 topics) and once on `sim-batch-1` (SIM
  topics). Both branches were appending independently and both are pushed. C3's entries take
  **#27 and #28** as the next free numbers ON THIS BRANCH. Whoever merges the two lines has to
  renumber one side and fix the `ref project-changes #N` citations in its addenda; nothing in
  this epic can fix it from here.

- **On 2026-08-11/12 an agent session died mid-refactor in this worktree, and the recovery is
  worth knowing about.** It had committed E5.4a (`09b5271`) and the harness adoption
  (`167aa6e`), then began E5.4b and E5.5 together and terminated on an account spend limit.
  It left the tree **not importing** — `models.py` used `sqlalchemy.true()` without importing
  it — plus a new `deployment_service.required` column with **no migration**, `status.py` still
  on the parameter form the column was meant to replace, and a model comment citing a "D117"
  that had never been written. The next session finished it in place: the import, migration
  `b7d41f0c2e93`, the column threaded through `roll_up` / `required_keys` /
  `apply_test_results`, and D117 written to say why. **Two lessons, both cheap to act on.**
  Run the affected suites, not only the new ones — E5.4a's own 24 tests passed while it had
  silently invalidated an E5.3 test (D118). And a unit that adds a column is not partially done
  without its migration: the failure surfaces as 38 unrelated-looking errors in four other
  suites, all of them `column ... does not exist`.

- **Concurrent runs are safe now, and were not when D112 was written.** The fix arrived from
  the SIM branch on 2026-08-11 (**D113**): `conftest.py` gained a machine-wide cross-process
  lock and port-claim registry plus host-side TCP readiness probes, and this worktree was
  running the pre-fix harness until then because it was cut before that commit. Two full
  backend suites now run at once. **D112's "check that nothing else is running first" is
  retired**; what survives it is the half that still binds — a run that comes back with
  container-startup errors (`/forwards/expose returned unexpected status: 500`) is an
  **invalid measurement** to re-run, never a red gate to record, and `docker_retry` is narrow
  on purpose (D99) and is not to be widened. The measurement that produced D112 is preserved
  in that entry: two sweeps, 829 passed / 7 errors and 831 passed / 5 errors, every error a
  container fixture and none a test-logic failure, with the errored modules moving between
  runs.
- **The nine fixed choices in phase document section 2 were decided at plan approval.** They
  resolve spec 17 item 14 (dynsec required) and reverse the E4/E5 `BrokerCredentialProvider`
  ordering. Do not relitigate them mid-epic; a unit that seems to need one reopened is a
  stop-and-ask.
- **Two *discretionary* E3-owned edits are authorized**, both in E5.7b, both named in the phase
  document. A third landed in E5.1, **forced not chosen** — the nullability change breaks
  `load_broker_coordinates` under `mypy --strict`, so an under-specified row is now skipped
  rather than fatal (D109). A further discretionary E3 edit is a stop-and-ask.
  `app/contracts/mqtt.py`, `app/controlplane/consumer.py` and
  `app/controlplane/revision_state.py` are not touched at all.
- **The `service_restricted` refusal in `app/config/validation.py` stays true for operators.**
  E5 writes the twelve keys through a keyword-only `allow_write_restricted` flag defaulted off,
  not by deleting the check. The operator-facing 422 has its own independent test precisely so
  the shortcut cannot pass unnoticed.
- **`put_overrides` is the only writer of `entity_override`.** E5 does not grow a second one;
  duplicating the D51 secret-marker convention is how it drifts.
- **The duplicated secret at rest is deliberate** — the service row's copy and the config
  override's copy have independent lifecycles on purpose. Phase document fixed choice 3 says
  why. Do not "fix" it.
- **The rig is one container set on one shared xdist group**, started once per gate. It becomes
  the generated stack at E5.10. Adding a second per-service container fixture undoes the whole
  gate-time design.
- `app/services/` means **deployment services**. `app/config/service.py` is the merge accessor
  and is unrelated.
