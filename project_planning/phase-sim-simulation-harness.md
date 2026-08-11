# Phase SIM Document: Simulation Harness (Epic SIM)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 7 (as a consumer), 14.2
**Depends on:** E0 complete; converges with E3, which has now landed in full (gate 51). The E3.3
contracts module is the interface this phase builds against, and it is published.

---

## 1. Scope

Build a mock fleet: Aggregators and Listeners that speak the real spec 7 wire protocol against a
real broker, at the spec 14.2 simulation scale, on one host. When this phase ends, one command
launches a configurable fleet that connects, receives retained desired config, applies it,
reports state, reports Listener liveness, and can be told to misbehave in the specific ways the
platform is supposed to notice; and a small fleet runs in CI on every push.

This phase exists because every epic after it is defined in terms of a fleet it does not have.
E6's definition of done is the map rendering "against a SIM fleet". E8.6 is "the SIM.4 fleet at
target scale against the complete platform". Until this phase, the only mock device in the
repository is a twenty-line class inside one E3 test file, which was correct — the Phase 3
document scoped E3 to "only the minimal in-test mock fixtures its own integration tests need".
This phase is where that becomes a fleet.

**The harness is a client of the platform, never a part of it.** It has no privileged access, no
import of platform internals beyond the published wire contract, and no ability to reach into a
database to make a scenario work. If a simulated behaviour cannot be produced by publishing the
bytes a real device would publish, it is not a behaviour a real device can produce either, and
the finding is worth more than the test.

## 2. Prerequisites and inherited interfaces

Read `docs/INTERFACES.md` first. What this phase consumes, and does not redefine:

**The MQTT contract module (`backend/app/contracts/mqtt.py`, E3.1/E3.3) — the whole of this
phase's wire surface.** Topic builders (`desired_topic`, `reported_topic`, `status_topic`,
`event_topic`, `command_topic`, `listener_desired_topic`, `listener_reported_topic`),
`parse_topic`/`ParsedTopic`/`TopicKind`, `deployment_subscriptions`, the payload models
(`DesiredConfig`, `ReportedAggregatorState`, `ReportedListenerState`, `ListenerLiveness`,
`HealthBlock`, `StatusMessage`, `DeviceEvent`, `Command`), `encode`/`decode`/`describe`, and the
constants `QOS`, `SCHEMA_VERSION`, `DETAIL_MAX`, `EVENT_LISTENER_STREAM_GAP`,
`EVENT_LISTENER_MISSED_WAKE_WINDOW`. The module names SIM as its consumer in its own docstring.
This phase is the first outside caller, which makes it the first real test of that claim: where
the module turns out to be missing something a device genuinely needs, **say so and stop** — an
additive change to a published contract is a decision for the owner, not a convenience for a
mock.

**The development broker (`backend/app/devbroker.py`, E3.1).** `plan_accounts` mints one
account per Aggregator directly from the inventory table, so it already scales to a
twenty-Aggregator fleet with no changes; `device_username(aggregator_uuid)` and
`load_manifest(out_dir)` are how the harness learns credentials, and `accounts.json` is
deliberately the one place they live. The two-pass behaviour is inherited as-is: `--certs-only`
writes TLS material before Mosquitto can start, the full run needs seeded inventory. The ACL is
generated from the topic builders and grants an Aggregator exactly seven topics — **a device
cannot publish its own desired config**, which is the property that stops a mock manufacturing
agreement and defeating drift detection. A scenario that appears to need a wider ACL is a
scenario that is wrong.

**The dev compose broker.** TLS only on 8883 (host 18883), no 1883 listener anywhere,
`persistence true` so retained desired messages survive a restart.

**The D52 checksum recipe** (`docs/DECISIONS.md`, implemented at `app.config.canonical`): JSON
with keys sorted at every depth, compact separators, `ensure_ascii=False`, UTF-8, no trailing
newline; the checksum is `"sha256:"` plus the hex digest of those bytes. A device echoes the
checksum of what it applied and matching is string equality.

**The E1 inventory API** for provisioning a fleet: `POST /deployments`, `POST /pods`
(create-or-attach the Aggregator, E1.3), and `POST /listeners/import` with
`{"rows": [...]}` keyed on `mac`, `name`, `aggregator_uuid`, `gps_lat`, `gps_lon`, `tags`
(E1.6). Session cookie plus `X-CSRF-Token` on every mutation, as any client must.

**The E1.5 identity services** (`app.inventory.identity`), which E3.5 already wires to live
reported messages: `duplicate_identity` quarantine and the `provisioning_required` membership
check. SIM.3 asserts these outcomes; it never reimplements them.

**Test fixtures.** `backend/tests/conftest.py` owns `ephemeral_broker` (with `stop()`/`start()`),
`ephemeral_postgres`, `free_port` and `make_kek`. This phase's suite imports them by path rather
than growing a second Mosquitto fixture: there is one broker fixture in this repository and it
stays that way.

### Fixed choices for this phase

- **Location and packaging:** `/sim`, reserved by E0.1 and pinned by `test_repo_layout.py`, as
  **its own uv project** (`sim/pyproject.toml`) with ruff and mypy-strict settings matching the
  backend's. It reaches the platform by path (`../backend` on the Python path), not by making
  the backend an installable package — E0 owns that packaging decision and nothing here needs
  it changed.
- **Client library:** aiomqtt, matching E3's fixed choice. One asyncio process holds every
  Aggregator connection; a fleet is concurrency, not processes.
- **The wire surface is the contract module, exclusively.** No topic string and no control-plane
  JSON body is built or parsed by hand anywhere in `/sim`. A hand-rolled `f"eoe/..."` is a
  defect even when it happens to be correct, because the point of the harness is to prove the
  published contract is sufficient.
- **Checksums are reimplemented, not imported.** `sim/checksum.py` implements the D52 recipe
  from its written description; a cross-check test asserts byte-for-byte agreement with
  `app.config.canonical` over generated inputs. A simulator standing in for firmware that calls
  the platform's own function proves only that the function is self-consistent — this way, a
  recipe that cannot be reimplemented from its description fails here rather than in the field.
- **Scenarios are declarative:** TOML files (matching D5's config-file choice) naming behaviours
  from a typed Python registry, with parameters validated by Pydantic. The registry is the
  extension point; the files are the interface later epics use without writing code.
- **Provisioning goes through the platform's own API.** Inventory is created over REST as an
  operator would; broker credentials come from running `app.devbroker`. No direct database
  writes: a harness that seeds its own rows would not notice the day the API stops accepting
  them.
- **Compose:** a `sim` service behind an optional profile, off by default, so
  `docker compose --profile sim up` runs a fleet against the dev stack and an ordinary
  `docker compose up` does not.
- **CI:** two stages, `sim-quality` (ruff, mypy) and `sim-protocol` (the suite against a real
  Mosquitto). The second name is already reserved for this epic in `INTERFACES.md`.
- **Scale.** Spec 14.2 and the project plan read the simulation target differently. **The
  project plan's reading is binding here: 20 Aggregators × 30 Listeners each = 600 Listeners**,
  as the default and as the documented load check. CI runs a small fleet (2 × 3). Every count is
  a parameter; nothing in the design may assume either size.

## 3. Out of scope

Real firmware, and the Listener-to-Aggregator local link (spec 17 item 2 tracks its framing;
this phase models wake declarations as an in-process call between two objects and must not
invent a wire format for them). Any change to platform behaviour to make simulation easier — if
a task appears to need one, stop and ask (rule R2). Per-device broker credential minting and the
dynsec path (E5.6; this phase uses devbroker's dev accounts). Provisioning bundles and the
bootstrap block (E4). Map rendering (E6). Alerts (E7). Telemetry generation of any kind: the
harness simulates the **control plane**, not the audio pipeline, and does not write to Influx or
Prometheus (E7 owns reads; spec 10.1 keeps device telemetry out of MQTT deliberately, and a mock
that published metrics over MQTT would model the wrong architecture). The full-scale load run
against the complete platform and the findings from it (E8.6 — this phase ships the runner and
the written procedure, E8 runs it once the platform is complete). Weakening or editing the four
test-critical suites (spec 14.5), including E3's existing mock fixtures: this phase supersedes
them in capability, never by deleting them.

## 4. Task list

> **Addendum PHASESIM-4-01 (2026-08-11, ref project-changes #24):** SIM.1, SIM.2 and SIM.3 share
> one gate, taken after SIM.3, rather than one gate each. `/sim`'s suite does not join `gate.sh`
> until SIM.5, so the accumulated suite a per-task gate would assert is identical for all three;
> the folded gate runs one unfiltered `make gate` plus `/sim`'s quality and protocol runs, with
> R0's counts unchanged. SIM.4 and SIM.5 gate individually as written. The cost is recorded in
> project-changes #24: SIM.3 was written before SIM.2 was gated, and the combined suite's first
> run found a harness defect (DECISIONS D108) a task later than per-task gating would have.

**SIM.1 Mock Aggregator.** The `/sim` project skeleton (pyproject, lint and type configuration,
test conftest bridging to the backend fixtures), `sim/checksum.py`, and one `MockAggregator`: a
TLS connection on its own devbroker credential with the `offline` `StatusMessage` registered as
its LWT before connect and `online` published retained on connect; subscriptions to its desired
and cmd topics built from the topic builders; apply of a received `DesiredConfig` (the config
dict copied verbatim, per the contract's load-bearing rule) and publication of a
`ReportedAggregatorState` carrying its own computed checksum; `DeviceEvent` publication; and
command handling deduplicated by `command_id` in the manner of the existing `MockDevice` fixture
(spec 7.4) — not by command name, so an operator's deliberate second restart still runs.
*Acceptance:* against a real broker and a real platform, a published revision reaches `applied`
with nothing hand-driven; a device that connects **after** the publish still receives it (spec
6.4's retained property); an unclean disconnect flips the Aggregator offline through the LWT; the
checksum cross-check against `app.config.canonical` passes.

**SIM.2 Mock Listener behaviour.** `MockListener`, owned by its parent Aggregator and holding no
MQTT session of its own (spec 6.4): the Aggregator receives its desired config on the `lst/{mac}`
subtopic, applies it over the modelled local link, and reports on its behalf. The spec 6.5
liveness state machine — `streaming`, `sleeping` with a self-declared `expected_wake_at`, and
`offline` once that time plus `listener.wake_grace_seconds` has passed — with the **Aggregator**
computing grace and raising `listener_missed_wake_window`, never the platform and never the
Listener. *Acceptance:* a sleeping Listener reads as healthy on the platform; a missed wake
window flips it offline and lands on the timeline as an event; `expected_wake_at` is present
exactly while sleeping; a `listener_stream_gap` under `capture.mode=continuous` is distinguishable
from an expected off-window.

**SIM.3 Scenario scripting.** A typed registry of injectable behaviours with Pydantic-validated
parameters, loaded from TOML scenario files, and the catalogue the project plan names: apply
errors, drift, disconnects, missed wake windows, duplicate MACs, and an unprovisioned
`aggregator_uuid`. Each ships as a scenario file and a test asserting the **platform's** reaction
— `failed`, `drifted`, LWT offline, Listener offline, `duplicate_identity` quarantine with
inventory provably unchanged, and `provisioning_required` — routed through the E1.5 services that
E3.5 already wires. *Acceptance:* every shipped scenario loads, runs and asserts its platform-side
outcome; an unknown behaviour name or an out-of-range parameter fails at load with a message
naming the file and the key, not at hour two of a load run.

**SIM.4 Fleet runner.** `sim/fleet.py` as a CLI (`--aggregators`, `--listeners-per-aggregator`,
`--scenario`, `--deployment`, `--broker`, defaulting to 20 × 30), `sim/provision.py` creating the
hierarchy over the REST API and invoking `app.devbroker` for accounts and ACLs, and the `sim`
compose service behind its profile. Connection start-up is staggered rather than simultaneous;
shutdown is clean, publishing an explicit `offline` rather than leaning on the LWT — a harness
whose only exit looks like a crash cannot be used to test a crash. Counters for connected
devices, publishes, applies and reports. *Acceptance:* one command brings a fleet up against the
dev stack and every Aggregator reaches `applied`; a 20 × 30 run completes on one host with
memory, CPU and time-to-all-applied recorded in the walkthrough. Extends `COMPOSE_SERVICES` in
`test_repo_layout.py`, deliberately and with its `INTERFACES.md` entry, exactly as E3.7 did for
`worker`.

**SIM.5 CI integration.** The three-step stage recipe twice: `sim-quality` and `sim-protocol`
functions plus `STAGES` entries in `gate.sh`, a CI job per stage invoking `sh gate.sh <stage>`,
and both job ids in the `ci-green` needs list — `backend/tests/test_ci_pipeline.py` enforces the
parity in both directions. CI runs a small fleet (2 × 3) against a real Mosquitto.
`guide/sim-verification.md` ships in this task (rule R1), carrying both the hand-verification
walkthrough and the full-scale load procedure E8.6 inherits. *Acceptance:* `sh gate.sh --list`
shows both stages, `make gate` runs them, and the parity test is green.

## 5. Definition of done

One command launches a configurable fleet — default 20 Aggregators of 30 Listeners each — that
connects to a real broker over TLS on per-device credentials, receives retained desired config,
applies it, reports state the platform accepts as matching, reports Listener liveness under spec
6.5, and produces on demand every failure the platform is supposed to notice. CI runs a small
fleet on every push through `sim-protocol`. No topic string or control-plane payload is
constructed by hand anywhere in `/sim`. SIM's checksum implementation agrees byte-for-byte with
the platform's. The 20 × 30 run is documented with measured numbers, and the procedure for
running it against the complete platform is written down for E8.6.

## 6. Handoff artifacts

- `docs/INTERFACES.md` gains an **Owned by SIM** section: the `/sim` project layout and how it
  reaches the backend, the `MockAggregator`/`MockListener` surface later epics drive, the
  scenario file format and behaviour registry, the fleet CLI and its parameters, the `sim`
  compose profile, and the two gate stages.
- `docs/DECISIONS.md`: the 20 × 30 scale reading, the reimplemented-checksum choice, and every
  deviation found while building against the published contract.
- `guide/sim-verification.md`: the living acceptance walkthrough, including the full-scale load
  procedure E8.6 consumes.
- `project_planning/sim-progress-ledger.md`: per-task status, gates, tags and decisions.
- The scenario catalogue in `sim/scenarios/`, which E6 and E8 drive rather than rebuild.
