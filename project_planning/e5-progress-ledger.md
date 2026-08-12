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
| **C2 full gate** | — | — | — | The rig's measured cost against the ~300s ceiling recorded below. |
| E5.6 Broker credential minting (dynsec) | not started | C3 | — | Dedicated short-lived `$CONTROL` client, never `MqttClientManager`. `broker_credential` table. One source for the ACL grants. |
| E5.7a Projection and privileged write | not started | C3 | — | `allow_write_restricted` through three signatures. Includes the E2-owned `changed_keys` fix and the `publish_all` extraction. |
| E5.7b The two authorized E3 edits | not started | C3 | — | `MqttClientManager.refresh()` in both hosts, `service_config_sweep`. **A third E3 edit is a stop-and-ask.** |
| **C3 review + full gate** | — | — | — | Review subagent over the cross-epic diff before the gate. |
| E5.8a Broker material extraction | not started | C4 | — | Move to `app/brokerconfig.py`. `test_dev_broker.py` must pass **unchanged**. |
| E5.8b Compose and service configs | not started | C4 | — | Dicts + `yaml.safe_dump`, never string-templated. `deploy/stack-templates/`. |
| E5.9 Stack credential generation | not started | C4 | — | Credentials, secrets and rows committed in one transaction before any byte is rendered. |
| E5.10 Stack bundle endpoints | not started | C4 | — | Two downloads byte-identical. Re-points the rig at the generated bundle — the keystone. |
| E5.11 Rotation and regeneration | not started | C4 | — | A failed re-verification still publishes. The intuitive order is wrong. |
| **C4 full gate** | — | — | — | |
| E5.12a Wizard UI: Path A | not started | C5 | — | Schema-rendered forms, per-service result rows with remedy. |
| E5.12b Path B, gating, walkthrough | not started | C5 | — | S5 layout at v2 values. `guide/e5-verification.md` plus e3 amendments in the same batch. |
| **C5 full gate + PR** | — | — | — | |

## Measured gate durations

| Point | Warm duration | Note |
|---|---|---|
| Baseline (pre-E5) | ~260s | Reported at gate-53, before SIM.1 landed. |
| **C1 (E5.0-E5.3)** | **262.7s backend stage** | gate-54. Essentially flat against the baseline: this batch added 73 backend tests and no container fixture. The rig arrives with E5.4b, and the ~300s ceiling in phase-5 §5 is measured from here. |

## Notes for whoever picks this up next

- **OPEN QUESTION for the owner: what makes object storage "not required"?** Spec 16.2 makes it
  conditionally required and the phase document's E5.4e acceptance says the tester must answer
  `not_required` "for a deployment with raw-audio upload disabled". **There is no such toggle.**
  The settings catalog carries `upload.s3_bucket`, `s3_prefix`, `s3_endpoint`, `s3_access_key`
  and `s3_secret_key`, and nothing that switches the feature on or off. E5.4e therefore keys
  `not_required` on the only observable fact available: **both credentials absent** — no access
  key and no secret key means the platform cannot upload, so it is not uploading. A half-entered
  form (one credential present) is tested for real and fails loudly, so the reading cannot
  excuse a mistake. This is a reading of the spec rather than a quotation of it, it is stated at
  the top of `app/services/testers/s3.py`, and if the owner wants an explicit
  `upload.raw_audio_enabled` catalog key instead, that is an E2-owned catalog change and a
  stop-and-ask rather than something E5 should have taken.

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
