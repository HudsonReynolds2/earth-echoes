# SIM Verification Walkthrough

A hands-on test platform for the simulation harness — the mock fleet that stands in for real
Aggregators and Listeners. Work top to bottom, ticking each box. Every step says what to do and
what you should see; if you see anything else, that is a finding worth writing down.

This is a living acceptance document (rule R1): epic SIM ships it, and any later epic that
invalidates an assertion here amends it in the same batch. Siblings:
[the E1 walkthrough](e1-verification.md) covers hierarchy and inventory,
[E2](e2-verification.md) the configuration model, [E3](e3-verification.md) the device control
plane this harness talks to.

Section 6 is the **full-scale load procedure E8.6 inherits**. It is a manual check, not a gate
test: CI runs a small fleet (2 × 3) on every push through `sim-protocol`, and the 20 × 30 run is
performed by hand against a complete stack, with its numbers recorded here.

> **The harness is a client of the platform, never a part of it.** Nothing below reaches into a
> database to make something happen, and nothing below changes platform behaviour to make a
> scenario work. If you find yourself wanting to, that is the finding.

---

## 0. What you need

- [ ] Docker running, and the repo's dev stack able to start (`guide/getting-started.md`).
- [ ] `uv` on PATH.
- [ ] `/sim`'s own virtual environment: `cd sim && uv sync`. `/sim` is its own uv project (D100)
      and does **not** share the backend's venv.

Every command below is run from the repository root unless it says otherwise.

## 1. The harness stands alone

- [ ] `cd sim && uv run ruff check . && uv run ruff format --check . && uv run mypy`
      → clean. This is exactly what `sh gate.sh sim-quality` runs.
- [ ] `cd sim && uv run python fleet.py --list-scenarios` → the six shipped scenarios, each with
      its `expects:` line, then the behaviour registry beneath them. Two scenarios are marked
      `[suite only: …]`.

      That marking is not decoration. `duplicate_mac` needs an Aggregator claiming a MAC filed
      under a different parent, and `unprovisioned_aggregator` needs a device inventory has never
      heard of. A fleet provisions every device it runs and mints each one a credential from that
      inventory row, so neither is producible here (D113); both are exercised in
      `sim/tests/test_scenario_outcomes.py`, which stages exactly those conditions.

- [ ] `cd sim && uv run python fleet.py --scenario duplicate_mac --certs deploy/dev-certs`
      → exits non-zero with a message naming the behaviour and pointing at that suite. **Before
      any socket is opened**: startup refusals are the whole point (D107).
- [ ] `cd sim && uv run python fleet.py --scenario drft` → refused, listing the scenarios that do
      exist. Also before anything connects.

## 2. Start the platform

The dev stack, exactly as [getting-started](getting-started.md) describes it. In short, from the
repo root:

```bash
cd backend && uv run python -m app.devbroker --certs-only     # TLS material, so Mosquitto starts
cd ../deploy && docker compose up -d --wait
cd ../backend && DATABASE_URL=<host URL> uv run alembic upgrade head
DATABASE_URL=<host URL> uv run python -m app.seed --demo      # RECORD the printed password
```

- [ ] `<host URL>` is the host-facing database URL — `postgresql+psycopg://…@localhost:15432/…`
      with the credentials from `deploy/.env`. The URL in that file names `postgres:5432`, which
      only resolves *inside* the compose network.
- [ ] `app.seed --demo` prints an owner password **exactly once**. Record it; you need it in the
      next step, and it is not stored anywhere. On a database that already has an owner, seed
      refuses and adds hierarchy only — use the credentials you already have.
- [ ] `curl -s http://localhost:18000/api/v1/health` → `{"status":"ok","database":"ok",…}`.

## 3. Provision a fleet

Broker accounts are minted **from inventory**, so inventory has to exist first. `provision.py`
does both halves in that order:

```bash
export EOE_SIM_OPERATOR=<the owner email>
export EOE_SIM_PASSWORD=<the password seed printed>     # NEVER a command-line flag (rule R2)
export DATABASE_URL=<host URL>
export EOE_KEK=<the value in deploy/.env>
cd sim && uv run python provision.py --aggregators 2 --listeners-per-aggregator 3 \
      --platform-broker mosquitto:8883
```

- [ ] It logs `created deployment sim-fleet`, one `created pod SIM Pod NNN with aggregator …` per
      Aggregator, then `imported N Listeners`, then the devbroker summary.
- [ ] `--platform-broker mosquitto:8883` is what the **platform** dials — a service name on the
      compose network. It is not what a device on your host dials (that is `localhost:18883`).
      Getting these two confused is the single easiest way to lose an hour.
- [ ] The last line tells you to reload the broker. Do it, and restart the API and worker too:

      ```bash
      cd deploy && docker compose restart mosquitto api worker
      ```

      **All three, and each for its own reason.** Mosquitto re-reads `passwd`/`acl` only on
      restart, so without it every new account is refused with a bare "not authorised". And
      re-running the generator rotates *every* password including the platform's own, while
      `MqttClientManager.start()` reads its broker coordinates exactly once (E3.2, deliberately —
      the worker owns that lifecycle). Skip the API and worker and the fleet will connect
      perfectly while every revision sits `draft`, because the platform has no live connection to
      publish over.

- [ ] Run the same `provision.py` command **again** → it creates nothing and logs
      `all N Listeners already exist`. Provisioning is idempotent by asking, not by swallowing
      conflicts; re-running with a *larger* fleet extends it.
- [ ] In the UI at <http://localhost:15173>, the `sim-fleet` deployment holds the pods, and each
      Listener carries the `sim` tag — which is how an operator tells simulated hardware from the
      real thing in their own inventory.

## 4. One command brings the fleet up

```bash
cd sim && uv run python fleet.py --aggregators 2 --listeners-per-aggregator 3
```

- [ ] It logs the staggered connects, publishes one Aggregator-level revision
      (`listener.wake_grace_seconds=45`), waits, and prints:
      - `time-to-all-applied: N.Ns`
      - a counters line — Aggregators connected, Listeners, publishes, applies split into
        aggregator/listener, reports, commands
      - `peak resident set size: N MiB`
- [ ] Every counter is read off the mock devices themselves, so `applies` is what the fleet
      genuinely applied. `applies` should be `aggregators × (1 + listeners_per_aggregator)`: an
      Aggregator-level change moves its Listeners' effective config too, so E2 cuts a revision per
      device and all of them go out.
- [ ] In the UI, every fleet Aggregator shows **online** and its revision reaches **applied**.
      Nothing was hand-driven: the operator's edit was the fleet's own REST call, and the
      checksums that matched were computed by the devices from the D52 recipe reimplemented in
      `sim/checksum.py`.
- [ ] The command **exits by itself** once the fleet has converged, and the fleet is gone from the
      UI's online set within a few seconds — because shutdown publishes an explicit `offline`
      rather than leaning on the will. A harness whose only exit looked like a crash could not be
      used to test a crash.
- [ ] Run the SAME command a second time. It reports that nothing needed publishing: the fleet
      already holds `listener.wake_grace_seconds=45`, so no device's effective config changed and
      E2 correctly cut no revision. It waits for nobody and exits. Add `--wake-grace 60` and the
      run publishes again. (A runner that waited for 620 devices nobody had told anything was a
      real defect here — D115.)
- [ ] Now add `--stay` and run it again. The fleet stays up. Press Ctrl-C → it shuts down
      politely, same counters, same `offline`. `docker compose down` sends SIGTERM and gets the
      same treatment.

## 5. Making the fleet misbehave

Each of these is produced the way a real device produces it — by publishing the bytes a real
device would publish. Run them with the fleet from section 4 provisioned.

- [ ] `uv run python fleet.py --scenario drift --scenario-devices 1 --stay`
      → one Aggregator's revision moves to **drifted** in the UI while the others stay
      **applied**. The device reports a config that differs, carrying the checksum of what it
      actually holds; nothing edited a row.
- [ ] `uv run python fleet.py --scenario apply_error --scenario-devices 1 --stay`
      → publish a change from the UI. That device's revision goes to **failed** immediately
      rather than waiting out the pending window (D70): the device answered, with the wrong
      config, coherently.
- [ ] `uv run python fleet.py --scenario disconnect --scenario-devices 1 --stay`
      → that Aggregator goes **offline** in the UI within the keepalive window, and the
      `offline` the platform recorded is *older* than the `online` before it — because a will is
      composed at CONNECT time. That timestamp ordering is how you tell an LWT from a polite
      goodbye.
- [ ] `uv run python fleet.py --scenario missed_wake_window --scenario-devices 1 --stay`
      → the device's first Listener declares an off-window, reads as **healthy while sleeping**,
      and flips **offline** once its declared wake time plus `listener.wake_grace_seconds` has
      passed. A `listener_missed_wake_window` event lands on that device's timeline. The grace is
      the device's own, from its own applied config: publish a shorter one and the same scenario
      resolves faster.

## 6. The full-scale load run (20 × 30) — the procedure E8.6 inherits

**The binding scale is 20 Aggregators × 30 Listeners each = 600 Listeners** (addendum PLAN-3-02;
spec 14.2 and the project plan read it differently and the plan's reading wins). This is a manual
check on one host. E8.6 runs it again against the complete platform and records its own numbers
beside the ones below.

Procedure:

1. Start from a **clean** stack, so the numbers are not measuring somebody else's leftovers:
   `cd deploy && docker compose down -v && docker compose up -d --wait`, then migrate and
   `app.seed --demo`.
2. Provision at scale:
   `cd sim && uv run python provision.py --aggregators 20 --listeners-per-aggregator 30
   --platform-broker mosquitto:8883`
3. `cd deploy && docker compose restart mosquitto api worker` (all three — section 3 says why).
4. Run the fleet, and time it:
   `cd sim && /usr/bin/time -v uv run python fleet.py --aggregators 20
   --listeners-per-aggregator 30`
5. While it runs, sample the platform side: `docker stats --no-stream`.
6. Record: provisioning wall clock, time-to-all-applied, the counters line, the harness's peak
   RSS, and the broker/API/worker CPU and memory at their peak.

### Measured, 2026-08-12 (SIM.4/SIM.5, gate 58)

One host: WSL2 on Windows, 16 logical CPUs, Docker Desktop, the full dev compose stack
(Postgres, Redis, Mosquitto, API, worker, frontend) plus the harness in one process. Clean
database, demo hierarchy seeded, nothing else running.

**The harness side**

| Measure | Result |
|---|---|
| Provisioning: 20 pods + 20 Aggregators + 600 Listeners, over REST | **3.0s** wall, 79 MiB peak RSS |
| Fleet connect: 20 TLS sessions on per-device credentials, 0.05s stagger | **2.1s** |
| Time-to-all-applied: 620 revisions (20 Aggregator + 600 Listener) | **5.2s** after the fleet was up |
| Whole `fleet.py` command, connect to polite shutdown | **8.0s** wall, 8% CPU, **45 MiB** peak RSS |
| Counters reported | 20/20 connected, 600 Listeners, 630 publishes, 620 applies (20 aggregator / 600 listener), 610 reports |
| Platform state afterwards | **620 of 620 revisions `applied`** — 20 aggregator, 600 listener |

**The platform side**, peak of six `docker stats` samples across the run:

| Container | Peak CPU | Memory |
|---|---|---|
| `worker` (reconciliation + consumer) | 59.3% | 68 MiB |
| `postgres` | 20.1% | 56 MiB |
| `api` (publisher) | 3.0% | 91 MiB |
| `mosquitto` | 0.03% | 5.4 MiB |

**What dominates, and what to watch.**

- **The consumer dominates, and the broker is nowhere near loaded.** 620 retained publishes out
  and 620 reports back cost the worker ~60% of one core and Mosquitto essentially nothing (5 MiB
  RSS, 0.03% CPU). The bottleneck at this scale is the platform's per-report database work, not
  the wire.
- **A fleet-wide apply is 620 publishes, not 20.** An Aggregator-level change moves every
  Listener's effective config too — `listener.wake_grace_seconds` is inherited, not filtered out
  — so E2 cuts one revision per device and all of them go out. That is the number to reason about
  when a larger fleet is planned (D115).
- **Re-running with the same value publishes nothing, correctly.** E2 cuts a revision only for a
  device whose effective config actually changed. `fleet.py` says so and waits for nobody; pass a
  different `--wake-grace` to make a repeat run do something. This was a real defect in the
  runner, found by this load run (D115).
- **Counters can trail applies by a few reports.** Above, 620 applies against 610 reports: a
  waiter is satisfied by the apply, and the report that follows it may still be awaiting its
  PUBACK when the counters are read. The platform's rows are the authority, and all 620 arrived —
  the polite shutdown's own `offline` publish is queued behind them on the same ordered session,
  which is what gets them out.
- **The wake sweep is the harness's only background work.** Every device compares its clock to
  its Listeners' declared wake times every 0.5s (`wake_sweep_interval`). At 20 × 30 that is 40
  sweeps a second over 30 in-memory objects each, with no I/O unless something is actually
  overdue. It is the first thing to look at if a full-scale run shows unexplained CPU — it did
  not here.
- **A fleet is one process** (phase doc fixed choice), so the harness's memory is one number: 45
  MiB for 620 device objects and 20 TLS sessions.

## 7. The gate

- [ ] `sh gate.sh --list` shows **`sim-quality`** and **`sim-protocol`** in the registry.
- [ ] `sh gate.sh sim-quality` → clean.
- [ ] `sh gate.sh sim-protocol` → the whole `/sim` suite, 0 failed / 0 skipped / 0 xfailed /
      0 deselected. It runs `sim/tests/gate_runner.py`, which imports `GateGuard` and `enforce`
      from the backend's runner — one implementation of R0, launched from two projects.
- [ ] `make gate` runs both alongside the backend and frontend stages, and
      `backend/tests/test_ci_pipeline.py` enforces that each has a CI job and that both job ids
      are in the `ci-green` needs list.
- [ ] `docker compose --profile sim up sim` runs a fleet against the stack;
      `docker compose up` does **not** — the service sits behind an optional profile, and
      `backend/tests/test_repo_layout.py` asserts both halves of that.

      The container connects devices and nothing else: it makes no REST calls and publishes no
      revision (`--no-provision --no-apply --stay`), because the other half of provisioning
      cannot be done from a container at all — accounts are minted into `deploy/dev-certs` and
      Mosquitto only reads them on restart, and a container restarting a sibling service to fix
      its own credentials would be reaching into the platform. Provision on the host first
      (section 3); the fleet then applies whatever desired config is retained, so an operator
      publishing from the UI watches the whole fleet converge.
