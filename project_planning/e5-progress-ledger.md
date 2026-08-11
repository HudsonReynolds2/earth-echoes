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
| E5.0 Phase document and records | built, awaiting C1 | C1 | D104-D108 | Phase doc, this ledger, project-changes entries, plan §3 / phase-4 §2 / spec item 14 addenda. Docs only. |
| E5.1 Services data model | built, targeted tests green | C1 | D109 | Migration `a31287354e23`. Widened `service_key` to five, conditional `mqtt` CHECK, `config`/`secret_names` JSONB, five status columns, `deployment.services_status`, `app/services/store.py`. Fixed the `DELETE /deployments/{id}` 500. One additive E3-owned edit in `controlplane/broker.py`: a row missing its connection columns is skipped, not fatal. Targeted: `test_services_model.py`, 13 passed. |
| E5.2 Write-only secrets API | built, targeted tests green | C1 | D110 | `app/services/schemas.py` (five `extra="forbid"` models, `KeepSecret`, pure `plan_write`, `redacted_settings`) and `app/api/services.py`. `MANAGE_SERVICES`/`VIEW_SERVICES` added to `rbac.py`, `rbac.ts`, `test_rbac.py` and `rbac.test.tsx` — additions only, the test-critical matrix is not weakened. D110: the PUT is a partial collection of wholesale members and has no delete. `E0_ROUTES` in `test_e0_readiness.py` extended by the two routes. Targeted: `test_services_api.py` 24 passed, plus `test_rbac`/`test_api_skeleton`/`test_audit`/`test_scoping`/`test_e0_readiness`/`test_services_model` green; ruff, `mypy app`, `tsc --noEmit` clean. |
| **C1 full gate** | — | — | — | Baseline and post-C1 warm gate durations recorded below. |
| E5.3 Connection test framework | not started | C2 | — | `ServiceTester` protocol, `TestResult`/`CheckResult` with remedy, concurrent runner with timeout budgets, `POST .../services/test` over candidate or stored credentials. |
| E5.4a MQTT tester + dynsec probe | not started | C2 | — | Three-valued probe: `available`/`denied`/`absent`. Under fixed choice 4 a non-`available` verdict fails the tester. |
| E5.4b InfluxDB 3 tester | not started | C2 | — | HTTP query API, not FlightSQL — no `pyarrow`. First unit to need the container rig. |
| E5.4c Prometheus tester | not started | C2 | — | Must distinguish receiver-disabled from credentials-rejected from accepted. |
| E5.4d Grafana tester | not started | C2 | — | The one mutating tester. Idempotence asserted against Grafana's own listing. |
| E5.4e Object storage tester | not started | C2 | — | `not_required` when raw-audio upload is off, never `fail`. |
| E5.5 Services status lifecycle | not started | C2 | — | `roll_up` is the only writer of `deployment.services_status`; `DEGRADE_AFTER_FAILURES = 2`. |
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
| Baseline (pre-E5) | to be measured at C1 | `sim-batch-1` tip; ~260s reported at gate-53 before SIM.1 landed. |

## Notes for whoever picks this up next

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
