# Decisions

Deviations from the spec or a phase document, and implementation choices the documents left
open, with rationale (implementation-handbook.md section 1, rule R1). Feed these back into
the next spec or phase-doc revision. Newest first within each batch.

## D139 (2026-08-13): The service-config sweep was resetting the rotation counter it exists to
deliver, and the fix has to sit after the early return

- **Decision:** `config_sweep.py::_plan_one` writes `services.credentials_generation` into the
  projection, **after** the "nothing to deliver" early return rather than by passing
  `generation=` to `service_settings`.
- **The defect, and it defeated D134 entirely.** `service_settings` OMITS the counter when no
  generation is passed, so the projection stops asserting a value and the effective config
  falls back to the catalog default of `0`. The sweep did not pass one. Since the sweep runs
  once a minute over every deployment with services, it did not merely fail to deliver the
  counter — it **actively reset every rotated deployment's counter from N back to 0 and minted
  a revision to publish the reset**. A rotation's signal to its devices survived less than a
  minute, and the device-visible value moved BACKWARDS, which is worse than never sending it
  for a number whose stated purpose is "a count a device compares cheaply against what it last
  acted on".
- **Why the obvious fix was wrong.** Passing `generation=` to the `service_settings` call
  makes the key always present, so the projection is never empty, the early return becomes
  unreachable, and every deployment holding only an `mqtt` row rebuilds a full change plan
  every minute — on a 20x30 fleet, 620 merges per deployment per pass, which is exactly the
  cost that return was written to avoid. Measured: it broke
  `test_a_broker_only_deployment_never_builds_a_plan` immediately. The counter is added after
  the return instead: a deployment with nothing else to deliver has no generated stack and so
  no rotation to announce.
- **Found by hand, against a live stack, and not by the suite.** The C4 gate was green. The
  manual verification ran generate → rotate → regenerate through a real uvicorn and read the
  minted snapshots: the platform's column said 3 while the devices had been told
  `2 -> 0`. No test covered "what does the sweep do to a rotated deployment", because every
  existing sweep test used a deployment whose counter was 0, where the bug is invisible.
  `test_the_sweep_does_not_reset_the_credentials_generation` now sets it to 7 first, which is
  what makes the assertion able to fail.
- **The general shape, worth remembering:** `service_settings`' `generation=None` means "do not
  assert this key", and for a WHOLESALE projection that is indistinguishable from "assert the
  default". Any third caller that projects for delivery has the same trap; the parameter's
  docstring says every such caller must pass one, and this one did not.

## D138 (2026-08-13): A cancelled connect stranded a live broker connection, and the third
E3-owned edit was taken to fix it

- **Decision:** `app/controlplane/broker.py` gains `_open_client`, the mirror of the existing
  `_close_client`, and `_connection_loop` establishes its client through it. Taken as an
  **E3-owned fix made by E5 on the owner's explicit authorization** — the phase document
  authorizes exactly two discretionary E3 edits, both in E5.7b, and makes a third a
  stop-and-ask. This is the third.
- **The defect.** aiomqtt's `__aenter__` awaits twice: paho's blocking `connect()` inside an
  executor thread, then the CONNACK. Cancel the task at either point — which is exactly what
  `stop()` does — and `enter_async_context` never returns, so **the client is never registered
  on the `AsyncExitStack`**, while the executor thread runs the connect through to completion
  anyway, because a thread that has already started cannot be cancelled. paho opens the socket,
  `_on_socket_open` schedules the `_misc_loop` task, and what survives is a fully CONNECTED
  client with a live socket that nothing owns and `stack.aclose()` has never heard of. It
  outlives `stop()` for the life of the process.
- **This is D94's leak from the other end of the lifecycle.** `_close_client` already existed
  because aiomqtt's `__aexit__` could be abandoned mid-teardown; nobody had asked what happens
  when the ENTRY is abandoned instead. The fix is deliberately the same shape — run it in its
  own task, shield it, and on cancellation await it to completion so the stack really owns the
  client before the cancellation continues — so the file has one idiom rather than two.
- **Found by `test_shutdown_leaves_no_running_tasks`**, once, in a loaded six-worker gate, as
  `socket=LIVE state=MQTT_CS_CONNECTED`. It passed 3/3 in isolation afterwards. **The test's
  fast-fail-on-live-socket rule was right and must not be softened:** a teardown in flight has
  already resolved `_disconnected` and cannot present as CONNECTED, so a live socket there is
  evidence of a leak and not of a slow shutdown. The first instinct — to treat a rare failure
  in a well-worn concurrency test as flakiness and give it a grace period — would have deleted
  the detector and kept the bug.
- **Proven falsifiable rather than assumed.** `test_a_cancelled_connect_cannot_strand_a_
  connected_client` forces the cancellation against a real broker, and asserts the socket is
  open BEFORE cancelling so a pass cannot be the vacuous one where nothing had connected.
  Measured with the shield removed: the test fails on the stranded socket. Measured with it
  restored: 25 passed.
- **A correction this turned up.** The helper's docstring said aiomqtt cancels `_misc_loop`
  from `__aexit__`. It does not — `__aexit__` never touches that task; the cancel is scheduled
  from `_on_socket_close` (aiomqtt 2.5.1). The conclusion drawn from it was still right, but
  the stated reason was wrong and is now corrected in place.
- **Reference:** project-changes #34, addendum PHASE5-4-06. Extends D94, D97, D109, D111.

## D137 (2026-08-13): A frozen golden checksum moved, for the first and only time so far, and
what makes that not a weakening

- **Decision:** digest A in `test_config_merge.py::test_golden_checksums_are_frozen` is
  re-frozen from `sha256:3f23f037…` to `sha256:91ff585c…`, and the three key-count assertions
  in `test_config_merge`, `test_config_endpoints` and `test_settings_catalog` move from 37 to
  38.
- **Why it had to move.** D134 added spec 5.3's thirty-eighth key,
  `services.credentials_generation`. Digest A is the defaults-only listener snapshot over the
  WHOLE catalog, so a new catalog key changes it by construction. That suite is one of the four
  test-critical suites rule R0 says no later session may weaken, and its own docstring says
  changing these constants is "a wire-protocol break, not a test update" — so the change is
  recorded here rather than made quietly.
- **What makes it an addition and not a regression, proven rather than asserted.** The
  re-freeze ships with a second assertion that DELETES the new key from the snapshot and
  requires the original `3f23f037…` digest back, byte for byte. Measured before the constant
  was edited. A merge-semantics change hiding inside the re-freeze — a reordered key, a
  changed default, an altered float repr — would fail that line, so the new constant is pinned
  to "the old snapshot plus exactly one key" instead of to whatever the code now happens to
  produce. **Regenerating a golden digest from current behaviour is the failure mode this
  avoids**, and it is the reason the entry exists.
- **Scope.** The catalog addition itself is D134 and the owner's decision; this entry covers
  only its consequence for the frozen contract. Spec addendum SPEC-5-01, project-changes #32.
- **Still true afterwards:** any future movement in these constants is a wire-protocol break.
  One recorded exception does not make the next one routine.

## D136 (2026-08-12): The API's root logger gets a handler, and `runner.py`'s docstring was
wrong (D127 closed)

- **Decision:** `app/middleware.py::install_root_handler` is called by BOTH `create_app` and
  `runner.py::main`. Authorized by the owner as an E0-owned fix taken by E5.
- **What was broken.** Uvicorn attaches handlers to its own `uvicorn.*` loggers and leaves the
  ROOT logger bare, so Python's last-resort handler passed WARNING and above and silently
  dropped every `app.*` INFO line in the API process — broker connected, coordinates
  refreshed, publish outcomes. `runner.py::main`'s docstring asserted the opposite ("under
  uvicorn the server installs the handlers"), which is why it survived three epics.
- **Why one helper and not two `basicConfig` calls.** The defect was precisely that one
  process configured logging correctly and the other believed it did not have to. A shared
  function makes "which processes log" a single fact. `basicConfig` is a no-op when the root
  logger already has handlers, so a host with its own logging configuration is unaffected.
- **Pinned by a test at INFO**, not at WARNING: WARNING worked throughout and is what
  disguised the bug for so long (`tests/test_api_skeleton.py`).
- **Reference:** D127, which recorded this as a stop-and-ask and is now closed.

## D135 (2026-08-12): The generated stack creates its own database and bucket, and Influx
takes its admin token from a file

- **Decision:** the generated compose file gains two short-lived init services, `influx-init`
  and `minio-init`, and Influx is started with `serve --admin-token-file` rather than an
  environment variable.
- **`INFLUXDB3_AUTH_TOKEN` configures the CLI, not the server.** Setting it on the server
  container looks exactly like preseeding a token and does nothing: Influx 3 Core mints its own
  admin token and refuses every other, so the platform's stored token got 401 on every call.
  `--admin-token-file` reads a `{"token": ..., "name": "_admin"}` JSON document at startup —
  the offline form of `influxdb3 create token --admin` — which is what lets the token be
  generated, stored and committed BEFORE the stack exists (fixed choice 7) instead of being
  scraped out of a container's log afterwards. The `apiv3_` prefix is required; measured, by
  starting a server on a token file without it and watching it refuse to come up.
- **Two pieces of state cannot be configuration.** Influx 3 creates a database on first WRITE
  and E5.4b's tester reads first (deliberately: a check whose first-run failure is normal is a
  check people learn to ignore); MinIO has no declarative bucket, and the phase document's
  E5.8b line asks for "optional MinIO **with a created bucket**". Both are seeded by an init
  container with `restart: "no"` and a retry loop rather than a healthcheck condition, because
  the Influx image ships no healthcheck and a compose file an operator debugs should not hang
  on a condition that never becomes true.
- **A sixth image, `minio/mc`.** Unavoidable: `minio/minio` carries the server and not even a
  shell's worth of utilities to script a bucket creation. It and the MinIO server are the two
  images allowed to float on `:latest`, because MinIO publishes nothing else; the carve-out in
  `test_no_image_is_floating` is now keyed by IMAGE rather than by service name.
- **Found by the keystone**, which is the only test that runs the artifact instead of
  inspecting it.

## D134 (2026-08-12): A rotation bumps a non-secret counter, because secret markers make
rotation invisible to devices

- **Decision:** a new catalog key `services.credentials_generation` (int,
  `write_restricted=SERVICE_ONBOARDING`) and a new column
  `deployment.services_credentials_generation` (migration `d5f28c60a419`), bumped in the same
  transaction as every credential generation and projected onto device config.
- **The problem, measured before it was believed.** A device's desired snapshot carries secret
  MARKERS and never plaintext (spec 5.4 and 8; D51, D126), and a marker is a SecretStore NAME —
  the identical string before and after a rotation. So a rotation that changed every credential
  a deployment has minted **zero** revisions: every snapshot unchanged, every plan entry a
  no-op, no device told anything. Rotating to a different hostname minted one revision per
  Aggregator and none per Listener, which proved the projection path was working and the marker
  convention was behaving exactly as designed. There was simply nothing to say.
- **Why it matters.** The phase document's E5.11 acceptance asks rotation to produce one
  revision per Aggregator so that "rotation is a config revision, not a manual redistribution"
  (spec 16.3). Without a non-secret signal that sentence is false of every device.
- **A count, not a timestamp.** Two renders of one generation must be byte-identical (fixed
  choice 7) and a clock is not; a count is also what a device compares cheaply against what it
  last acted on.
- **Owner decision, asked and answered on 2026-08-12.** The alternatives declined were
  accepting zero revisions and amending the acceptance, and deferring to E7. This is an
  **E2-owned catalog change plus a migration**, out of E5's scope under rule R2, and was taken
  only on that authorization. `write_restricted` keeps it off operator-writable surface and out
  of Listener snapshots, which is what preserves the "zero per Listener" half of the acceptance.
- **Reference:** project-changes #32, addendum PHASE5-4-02.

## D133 (2026-08-12): Periodic service re-checks are deliberately not built

- **Decision, the owner's, asked directly and answered directly on 2026-08-12:** the platform
  runs **no timed re-verification of deployment services, ever**. Spec 16.5's "periodic
  re-checks" item is closed as *deliberately not built* rather than left outstanding, and
  **E5.11 registers no sweep**.
- **Rationale, in the owner's terms:** timed polling reports a fact that was true minutes ago,
  and the platform should "fail fast and loudly and accurately" off real liveness instead.
  Degradation now comes only from observed events: an operator-run test, a rotation's
  re-verification, and for MQTT the control plane's own live connection and LWT.
- **What survives.** `status.py::services_recheck_sweep` stays as an **on-demand bulk
  re-test** — a callable an operator action invokes, not a scheduled job — on the owner's call
  in the same conversation. Its docstring says so rather than promising a timer.
- **What this closes.** The E5.5 note and the INTERFACES entry that both expected E5.7b to
  register the sweep, and the ledger's "OUTSTANDING for a later unit" item. Registering it
  would have needed a production `ServiceTestRunner` dialling every deployment's services on a
  timer, which no unit in this phase scoped.
- **Reference:** project-changes #31, addendum PHASE5-4-03.

## D132 (2026-08-12): Mosquitto 2.0 ignores `encoded_password`, and the fixtures were running a
different broker from the one that ships

- **Two findings, and the second is why the first survived so long.**
- **The format.** `dynamic-security.json` wrote the platform account's password as one
  `encoded_password` `$7$` field group. **Mosquitto 2.0's dynamic security plugin does not read
  that member**; it reads `password`, `salt` and `iterations` separately, silently ignores the
  combined form, and leaves the account with no password at all. Every connect was refused with
  CONNACK 135 and `not authorised`, with nothing in the broker's log naming the field it had
  skipped. Mosquitto 2.1 writes and reads the combined form. Measured against real brokers with
  `mosquitto_sub`: `encoded_password` is REFUSED on 2.0.20 and accepted on 2.1.2; the
  three-field form is accepted on BOTH. `brokerconfig.dynsec_password_fields` renders the
  three-field form and carries the table.
- **The reason nothing caught it.** `IMAGES["mosquitto"]` pins `eclipse-mosquitto:2.0.20`, and
  the test fixtures used `eclipse-mosquitto:2` — a floating tag Docker Hub has since moved to
  2.1.2. Every dynsec test in the suite passed against a broker no operator would ever run,
  including E5.8a's acceptance that the generated config starts a real broker whose probe
  answers `available`. **A pinned artifact tested against a floating tag proves nothing about
  what ships.**
- **Fixed, on the owner's authorization (E0/E3-owned files):** `conftest.MOSQUITTO_IMAGE`
  now reads `IMAGES["mosquitto"]` rather than repeating a tag, so the next version bump moves
  both; `deploy/docker-compose.yml` pins 2.0.20 for the same reason a developer's dev broker
  should be the deployment broker. `conftest.dynsec_config` renders through the same helper the
  shipped stack uses.
- **Found by E5.10's keystone**, and only by it: it is the one test that runs the generated
  artifact instead of inspecting it.
- **Reference:** project-changes #30, addendum PHASE5-4-04.

## D131 (2026-08-12): A failed stack generation restores prior secrets rather than deleting
them, and there is no `deployment_stack` table

- **Two decisions from E5.9, both about where state lives.**
- **No new table.** Fixed choice 7 says a download "re-renders deterministically from those
  rows", so E5.9 persists nothing besides the five `deployment_service` rows and a set of
  deterministically-named secrets (`deployment:{id}:stack:*`). Whether object storage was
  included is *whether an `s3` row exists*; the broker hostname is the `mqtt` row's `host`.
  A `deployment_stack` table holding generation parameters would be a second store to keep in
  step with the rows, and the first divergence would render a bundle that does not match the
  credentials the platform holds. The stack-owned names are deliberately in their own
  `:stack:` namespace so an operator saving service settings by hand (E5.2's wholesale PUT)
  cannot overwrite the material the bundle is rendered from.
- **Compensation restores, it does not just delete.** `SecretStore.put` opens its own session
  and commits (E0.11), so secrets do not roll back with the row transaction; E5.9's acceptance
  ("a fault before commit leaves zero rows and zero secrets") is met by deleting what the call
  wrote. The first implementation deleted *everything* it wrote — which is correct for a first
  generation and **destructive for a regeneration**, because rotation overwrites the same
  deterministic names. A failed rotation therefore destroyed the working credentials of the
  stack it was replacing, leaving a deployment whose devices hold credentials nothing accepts.
  It now snapshots any pre-existing value before overwriting, restores those on failure, and
  deletes only names that did not exist before the call.
- **Found by the test, not by design.** The regeneration case was written as "compensation must
  not be destructive" on the assumption it would pass. It did not. The generalisation worth
  keeping: a compensating action written against the create path has to be re-read against the
  update path, because the two differ exactly in whether the thing being undone already existed.
- **The limit, stated rather than hidden.** A process killed between the puts and the rollback
  leaves unreferenced ciphertext and no rows. That is harmless — nothing points at it — and is
  named in `stackgen.py`'s docstring so the next reader does not take it for an oversight.
- **Reference:** phase-5-deployment-services.md section 2 fixed choice 7, task E5.9;
  `backend/tests/test_stack_generation.py::test_a_failed_regeneration_leaves_the_previous_stack_intact`.

## D130 (2026-08-12): `dynamic_security_config` takes a hashed password, because hashing at
render time had already broken the byte-identical download

- **What changed.** `app/brokerconfig.py::dynamic_security_config` accepted a plaintext admin
  password and hashed it internally with a fresh `os.urandom` salt. It now takes an
  already-hashed `admin_password_hash`, and a new `dynsec_password_hash` does the salting
  once. E5.9 calls it, stores the result, and every render after that is a pure function of
  stored state.
- **Why it matters.** Phase-5 fixed choice 7 says the platform stores no bundle and re-renders
  it on every download, and the property that makes that legitimate is that **two downloads
  are byte-identical** — E5.10's stated acceptance. A per-call random salt made
  `dynamic-security.json` different on every render, so that property was already false as
  soon as E5.8a landed, two units before the test that would have caught it.
- **How it was found, which is the part worth keeping.** Not by reasoning about the design. By
  writing E5.8b's determinism test, hitting the JSON file, and beginning to write a comment
  explaining why *that one file* was allowed to be an exception. The exception was the bug.
  A carve-out being written into a test is a signal to re-read the requirement before writing
  the carve-out.
- **The shape this pushes onto E5.9.** Every credential that reaches a rendered config must be
  hashed in the same transaction that generates and stores it, in the form its own service
  demands — bcrypt for Prometheus's `web_config.yml`, PBKDF2-`$7$` for the broker's dynsec
  JSON. `StackSecrets` therefore carries hashes rather than plaintexts, and says so.
- **Reference:** phase-5-deployment-services.md section 2 fixed choice 7, tasks E5.8a/E5.9/
  E5.10; `backend/tests/test_stack_generator.py::test_rendering_is_deterministic`.

## D129 (2026-08-12): The whole-container retry extends to brokers and rig services, and
`docker_retry` is still not widened

- **What changed.** `ephemeral_broker` and `_rig_container` now retry the WHOLE container, up
  to three times, when its published port never answers from the host —
  `_start_ephemeral_postgres` has always done this and they never did.
- **Why it surfaced now.** D128 removed 55 Postgres container startups from the gate, and those
  startups were accidentally PACING the suite. Without them the remaining container starts land
  in much tighter bursts, and D99's forwarder fault — the container is up and accepting inside,
  `docker port` reports a mapping, the host connection is refused — went from rare to seven
  `test_dev_broker` setup errors in a single run whose 999 tests all passed. Making the suite
  faster made this more likely, which is a real cost of D128 and is recorded as one.
- **Why not widen `docker_retry`.** D99 made it narrow on purpose: it matches three specific
  stderr signatures on a command that FAILED. This fault is different in kind — the command
  succeeds, and the failure is only observable later, from the host, over TCP. There is nothing
  to retry at the command level and nothing that improves by waiting longer on the same
  container; only a new container helps. The two mechanisms answer different questions and
  collapsing them would make `docker_retry` a general-purpose "try again", which is exactly what
  D99 refused.
- **What this does not do.** It does not mask a test failure. Every retry is on container
  STARTUP, before any assertion runs, and each one prints a line naming the attempt. A genuine
  broker misconfiguration still fails on the first attempt with the broker's own logs attached,
  because that path raises rather than retrying.

## D128 (2026-08-12): The test suite stops starting a Postgres container per module, and every
container's writable state moves to RAM

- **What was measured, first.** A full green gate on the C3 tree, instrumented: **290.21s
  backend stage, 193 containers created, and 4.05 GB written to disk.** 57 of those containers
  were Postgres, at 4.02s each — 2.65s to start, 1.01s for `alembic upgrade head`, 0.36s to
  tear down — so the suite ran initdb 57 times and all 22 migrations 57 times, against a real
  filesystem, every gate.
- **What changed.** `ephemeral_postgres` keeps its signature and its guarantee — a migrated,
  empty, private database — but is no longer a container. One machine-wide Postgres runs with
  its data directory on tmpfs, the migrations run ONCE into a template database, and each
  caller gets `CREATE DATABASE ... TEMPLATE`. Measured on the same machine: **0.017s against
  4.02s.** Mosquitto's persistence, Prometheus's TSDB, Grafana's SQLite and MinIO's data
  directory moved to tmpfs as well.
- **Why the contract could survive this.** What every caller needed was never "a container";
  it was an isolated database at head. A clone from a template satisfies that exactly, and
  `test_container_pool.py` asserts it rather than assuming it: a pooled database is compared
  table-for-table AND row-count-for-row-count against one produced by a real `alembic upgrade
  head` in the same test, two live databases are proven not to see each other's DDL, and the
  clone is proven gone once its context manager exits.
- **Why the template is keyed by a fingerprint of the migration DIRECTORY and not by the head
  revision id.** Two worktrees can sit at the same head with different migration bodies — one
  of them mid-edit — and an id-keyed template would hand the second branch the first branch's
  schema. That failure would surface as wrong-column errors in unrelated suites, three layers
  from its cause. Keyed by content, identical migration sets share a template (the common case,
  and the win) and any difference at all forks one. Asserted by editing a migration byte and
  putting it back.
- **Why `fsync=off` is safe here and nowhere else.** The whole data directory is tmpfs, so
  there is nothing for a crash to leave half-written that a restart would need to recover. A
  pooled server that dies is REPLACED, not repaired — `_pool_container_healthy` checks both that
  the container runs and that its port answers, and starts a new one otherwise.
- **Concurrency, which was a requirement and not an afterthought.** Coordination reuses D113's
  existing primitives rather than inventing any: `GATE_STATE_DIR` for machine-wide state,
  `gate_lock` around every read-modify-write of the registry, `wait_for_host_port` for host-side
  readiness. Two agents in two worktrees share ONE server and are isolated by having different
  templates and uuid-named databases. A labelled container the registry does not name is an
  orphan and is always safe to remove, because acquiring one goes through the registry.
- **What it cost, stated rather than hidden.** Two real defects, both found by running the gate
  rather than by reasoning about it. Docker mounts a `--tmpfs` root-owned at mode 0755 while
  these images drop to unprivileged users, so the first run panicked Prometheus on `Unable to
  create mmap-ed active query log` and took the whole rig down — 37 errors in four modules,
  fixed by `mode=1777`. And Grafana's startup on tmpfs got fast enough to expose a latent race
  the rig had always had: nothing waited for Prometheus to SCRAPE ITSELF, and `/-/ready` answers
  strictly earlier than having data, so E5.4c's read check saw `up ... 0 series`. The fixture now
  waits for the thing it promises. See also D129 for the third.
- **What it did not fix, measured so the next attempt aims correctly.** The remaining writes are
  not container churn: `docker build` accounts for ~638 MB of them (400 MB in
  `test_e0_readiness`, which builds and runs the prod frontend image, and 238 MB in the
  `containers-build` stage), and the real production compose stack's named volumes for another
  184 MB in `test_compose_stack` and `test_verify_tool`. The whole service rig now writes 30 MB
  and 186 seconds of broker-heavy tests write 19 MB. Cutting further means either not proving
  the images build or not bringing the shipped compose file up, and neither is worth it.

## D127 (2026-08-12): Every `app.*` INFO log is invisible in the API process, and `runner.py`'s
docstring says the opposite (found by C3's manual verification; NOT fixed here)

- **What was observed, and how.** C3's manual walkthrough first asserted that the API's new
  refresh loop had connected to a late deployment by grepping its uvicorn log for
  `connected to the c3later broker`. The line was absent — **and so was the identical line for
  the deployment that had been connected since startup.** A WARNING from the same module
  (`lost the c3manual broker at ...`) WAS present. So the assertion was measuring log
  visibility, not behaviour.
- **The cause.** `app/controlplane/runner.py::main` configures logging for the worker process
  and its docstring explains why the API does not need to: "under uvicorn the server installs
  the handlers". That is true of the `uvicorn.*` loggers and **false of `app.*`** — uvicorn's
  default config attaches handlers to its own loggers and leaves the ROOT logger bare, so
  Python's last-resort handler passes WARNING and above and silently drops everything below.
  Every INFO line the API emits — broker connected, coordinates refreshed, publish outcomes —
  goes nowhere.
- **Why it matters more after E5.7b than before.** The API now runs a coordinates poll whose
  only intended operator-visible output is an INFO line. An operator watching for "did my new
  deployment's broker connect?" would see nothing and conclude it had not.
- **What the walkthrough does instead, and it is the better test anyway.** It applies a config
  change to the late deployment's Aggregator and reads the revision's `state`: `draft` before
  the broker row exists, `pending` after one refresh interval, same process, no restart. A
  revision only reaches `pending` if the manager holds a LIVE connection, so this answers the
  acceptance decisively and without depending on a log line at all.
- **Deliberately NOT fixed in C3.** The fix is one call to `configure_logging()` plus a
  `basicConfig` in `create_app`, but logging configuration for the whole API process is
  E0-owned, it changes log volume for the compose stack and CI, and no unit in this phase
  scoped it. **It is a stop-and-ask** (rule R2), recorded here and in the E5 ledger rather than
  taken quietly on the way past.
- **Reference:** `backend/app/main.py`, `backend/app/controlplane/runner.py::main` (the
  docstring that is wrong), `backend/app/middleware.py::configure_logging`.

## D126 (2026-08-12): A retained desired message carries secret MARKERS, and E5's projection is
pinned to that boundary rather than allowed to cross it (E5.7b)

- **Found by a test that asserted the wrong thing, which is why it is worth an entry.** The
  E5.7b acceptance says a cold-start Aggregator finds "a retained desired message carrying all
  twelve keys". The first version of `test_a_late_aggregator_finds_the_twelve_keys_retained_at_the_broker`
  read that as twelve VALUES and asserted the Influx token arrived as plaintext. It does not,
  and it must not.
- **The contract it collided with is E3.4's and it is correct.** A `config_revision.snapshot`
  holds the D51 secret markers E2 put there (spec 5.4, 8); `publisher.desired_payload` sends
  `revision.snapshot` verbatim and the module docstring says it "has no secret-handling of its
  own and must never grow any". So the twelve keys all arrive; the eight non-secret ones as
  values and the four secret ones as `{"$secret": "config:deployment:{id}:{key}"}`. The
  plaintext reaches a device through E4's provisioning bundle, which is a file an operator
  carries, not a message on a broker every device in the deployment can be told to subscribe
  near.
- **What changed as a result: nothing in the code, and one assertion in the test.** It now
  asserts the markers explicitly AND that no plaintext appears anywhere in the payload
  (`"late-influx-token" not in json.dumps(payload)`). That second half is the point of keeping
  it: if a later change ever put a credential into a retained broker message, this is the test
  that fails.
- **Recorded because the misreading is the natural one.** "Delivered post-connect" (spec 16.4)
  reads like "the device gets the credential over MQTT", and the next session to read the E5.7b
  acceptance will read it the same way.
- **Reference:** `backend/app/controlplane/publisher.py::desired_payload`,
  `backend/tests/test_broker_refresh.py`, phase-5 section 4 (E5.7b), D51, D55.

## D125 (2026-08-12): The E3-owned surface E5.7b actually took is three sweep registrations and
two refresh loops, not the one registration the phase document counted

- **What the document authorized.** Phase-5 section 2 names two discretionary E3-owned edits,
  both in E5.7b: `MqttClientManager.refresh()` plus "the refresh loops in both hosts", and
  `service_config_sweep` on the existing sweep runner. "A third discretionary E3 edit is a
  stop-and-ask."
- **What was taken, stated exactly.** In `app/controlplane/broker.py`: `refresh()`, plus
  `_begin`/`_cancel` extracted so `start()` and `refresh()` create a task and its bookkeeping in
  one place. In `app/controlplane/runner.py`: `_async_sweep_loop`, `_refresh_loop`, and **three**
  registrations — `service-config`, `broker-credential`, and the refresh task. In
  `app/main.py`: `_refresh_forever` and its task. Nothing in `consumer.py`,
  `revision_state.py` or `contracts/mqtt.py`.
- **Why the second registration is not a third EDIT.** `broker-credential` is the retry loop
  D121 promises. It is the same mechanism as the one the document authorized — a `(name,
  interval, callable)` entry on a runner explicitly built generic for exactly this — with an
  E5-owned body, and it exists because the owner chose `revoke_pending` over a 503 on
  2026-08-12. Registering it elsewhere would mean a second scheduler in the codebase to avoid
  a second line in an authorized diff.
- **Why `_async_sweep_loop` is a second loop and not a branch in the first.** The two existing
  sweeps are blocking SQLAlchemy and go through `asyncio.to_thread`; these await a broker and
  must not. A single loop branching on `inspect.iscoroutinefunction` would hide that difference
  at the one point where it decides whether the event loop blocks.
- **The refresh task is deliberately OUTSIDE `_sweeps`.** `_sweeps` is what the heartbeat
  vouches for. A worker whose coordinates poll died is still reconciling every deployment it
  already holds, and widening "healthy" to include the poll would make a new deployment's
  absence look like a dead worker.
- **What was NOT taken, and stays a stop-and-ask.** `services_recheck_sweep` (E5.5's spec 16.5
  periodic re-check) is still an unregistered callable. Registering it needs a production
  `ServiceTestRunner` that dials every deployment's real services on a timer, and that is a
  behaviour no unit in this phase scoped. It is named in the E5 ledger as outstanding.
- **Reference:** phase-5 section 2 ("The E3-owned edits this phase is authorized to make"),
  `app/services/config_sweep.py`, `app/services/credentials.py::drain_pending_revocations`,
  D108 (the three edits authorized in advance), D121.

## D124 (2026-08-12): `changed_keys` is computed through `snapshot_from_raw`, closing the
E2-owned defect that made one services save mint a revision per Listener (E5.7a)

- **The defect, as phase-5 section 2 describes it.** `DevicePlan.changed_keys` compared the raw
  before/after effective maps **including** the write-restricted keys, while its sibling
  `snapshot_from_raw` correctly strips them from Listener snapshots. So one services save marked
  every Listener in the deployment as changed, `no_op` was False, and `apply_change_plan` minted
  a revision per Listener whose published snapshot — after stripping — was byte-identical to the
  previous one.
- **The scale, which is what makes it a defect rather than an inefficiency.** On the SIM fleet
  this phase's own harness runs (20 x 30), that is ~600 pointless revisions and ~600 pointless
  retained publishes **per services save**, each one a row, a state machine transition, a
  `reconciliation_event` and a message a device has to parse to discover nothing changed.
- **The fix is at the source and uses the composition rule that already existed.** `changed` is
  now `snapshot_from_raw(target_type, before)` versus `snapshot_from_raw(target_type, after)`.
  That is the same function `revision_snapshot` and E3.7's drift sweep compose through, so the
  three cannot disagree about what a device is actually told.
- **It also makes preview honest.** "This listener is unaffected" is now true rather than
  nearly-true, which matters because the E2.6 acceptance is that preview matches what apply
  produces.
- **Pinned so it cannot be dropped as an optimization**, as the phase document asks:
  `test_service_projection.py` asserts one revision for the Aggregator and zero for thirty
  Listeners, and separately asserts the thirty Listener snapshots are byte-identical across a
  services save — the second one is about the BYTES a device would have received, so it holds
  even if the plan's bookkeeping is later rewritten.
- **Reference:** `backend/app/config/plan.py::build_change_plan`, phase-5 section 2 ("The
  E2-owned defect this phase must fix on the way through"), D55, D56.

## D123 (2026-08-12): Object storage stays conditionally required on "both credentials absent",
and the platform supports raw audio only (owner, closing the E5 ledger's open question)

- **The question the ledger raised at C2.** Spec 16.2 and section 721 make object storage
  "required only when raw-audio upload is enabled for the deployment", and **there is no such
  toggle** — the settings catalog carries `upload.s3_bucket`, `s3_prefix`, `s3_endpoint`,
  `s3_access_key` and `s3_secret_key`, and nothing that switches the feature on or off. E5.4e
  therefore keyed `not_required` on the only observable fact available: both credentials absent.
- **The owner's answer, 2026-08-12: keep that reading. No toggle is added.** The platform
  supports raw audio only for now, and an operator who is not uploading simply leaves the S3
  credentials blank. A half-entered form (one credential present) is still tested for real and
  still fails loudly, so the reading cannot excuse a mistake.
- **The two alternatives, and why they were declined.** Making object storage unconditionally
  required would mean no deployment reaches `verified` — and therefore none can generate a
  bundle under spec 16.5 — until S3 is configured, which punishes every deployment that does not
  upload. Adding an explicit `upload.raw_audio_enabled` catalog key is an E2-owned catalog change
  plus a migration, out of E5's scope under rule R2, and would have widened C3 for a flag with
  one consumer.
- **This closes the "OPEN QUESTION for the owner" in `e5-progress-ledger.md`.** Nothing in the
  code changes; E5.4e's reading is now a decision rather than a placeholder, and the note at the
  top of `app/services/testers/s3.py` stands as written.
- **Reference:** `backend/app/services/testers/s3.py`, spec 16.2 and section 721,
  `project_planning/e5-progress-ledger.md`, D111 (the four tester outcomes), D117.

## D122 (2026-08-12): `allow_write_restricted` is four signatures rather than three, and it
means two things that are one idea (E5.7a)

- **The document said three.** Phase-5 fixed choice 3: the flag is "threaded through
  `validate_override_map` -> `put_overrides` -> `build_change_plan`. Three signatures, one
  default, one meaning."
- **It has to be four.** `apply_change_plan` calls `put_overrides`. Without the flag reaching it,
  the plan a caller was just handed could not be executed — the write would 422 on the same keys
  the plan validated. Recorded rather than silently added, because a document that says "exactly
  three" while the tree has four is worse than no document.
- **The second thing the flag means, which the document implies but does not say.** Fixed choice
  3 also requires the projection to be "regenerated wholesale, never merged". That behaviour is
  carried by the same flag: with it on, every write-restricted key currently stored at the write
  target is dropped before the change map is applied. Unrestricted keys are untouched either way.
  Without this, an S3 endpoint an operator cleared would survive in the deployment's overrides
  forever and keep being delivered to devices — asserted by
  `test_a_cleared_optional_field_leaves_the_projection`.
- **The default is proven, not assumed.** `test_config_overrides_still_refuses_every_one_of_the_twelve`
  walks all twelve keys through `PUT /deployments/{id}/config/overrides` and requires a 422 with
  code `service_restricted` for each. A separate test asserts the flag does not make validation
  lenient in any other way: unknown keys, inventory-resolved keys and level violations still fail
  with it on. That is why the check was gated rather than deleted.
- **Reference:** `backend/app/config/validation.py`, `backend/app/config/overrides.py`,
  `backend/app/config/plan.py`, `backend/tests/test_service_projection.py`, phase-5 fixed
  choice 3.

## D121 (2026-08-12): A broker credential has three states, because deleting a device must
never be blocked by somebody else's outage and never strand a live login (E5.6, owner)

- **The question the phase document left implicit.** E5.6's acceptance says "deleting an
  aggregator revokes its dynsec client against a real broker and leaves the row `revoked`", and
  the table it specifies carries `state` in `minted`/`revoked`. Neither says what happens when
  the broker is unreachable at that moment — and it will be, because a decommissioning is
  exactly the kind of work that happens while a site is down.
- **The two failure modes, both real.** Refusing the delete (503 until the broker answers) lets
  one deployment's outage block inventory work indefinitely. Letting the delete pass silently
  leaves a decommissioned Pi holding a working credential for its deployment's broker, forever,
  with no record that it does.
- **The owner's decision, 2026-08-12: the delete proceeds and the revocation is retried.** The
  row lands in `revoke_pending`, `DELETE /aggregators/{id}` still returns 204, and
  `credentials.drain_pending_revocations` retries on the worker's sweep until the broker
  confirms. The cost is one extra state beyond the document's two, and it is recorded in
  project-changes #27 with an addendum on the phase document.
- **Two things keep the third state from rotting into ambiguity.** A CHECK constraint,
  `(state = 'revoked') = (revoked_at IS NOT NULL)`, so `revoked_at` means "the broker
  confirmed" on every row rather than "we asked" on some of them. And the distinction between a
  broker that is UNREACHABLE (retried) and a plugin that ANSWERED AND REFUSED (raised, logged
  with a stack trace, counted as a failure) — retrying a configuration fault forever would hide
  it.
- **The row outlives the device**, which is why `broker_credential.aggregator_uuid` is a plain
  string and not a foreign key to `aggregator`. A cascade would delete the platform's only
  record of a login that is still live on somebody's broker, which is precisely the state this
  decision exists to make impossible.
- **Asserted on the broker, not on the row.** `test_deleting_an_aggregator_revokes_its_client_on_the_broker`
  connects with the minted password before the delete, deletes, and then requires the login to be
  REFUSED — the row is the platform's belief and the broker is the fact.
- **Reference:** `backend/app/models.py::BrokerCredential`, migration `c4e9b21f83da`,
  `backend/app/services/credentials.py`, `backend/app/api/aggregators.py::delete_aggregator`,
  `backend/tests/test_broker_credentials.py`, phase-5 section 4 (E5.6).

## D120 (2026-08-12): The Aggregator ACL grants are ONE list with two renderers, because two
literal readings of spec 7.2 eventually disagree by one line (E5.6)

- **Why this needed doing at all.** Until E5.6 there was one authorization backend: the dev
  broker's `acl_file`, written by `devbroker.acl_file_text`. E5.6 adds a second — the dynamic
  security role every minted credential holds — reading the same spec 7.2 Direction column.
- **The disagreement that matters is not cosmetic.** It is a single missing line. An Aggregator
  that may publish to its own `desired` topic can manufacture agreement with itself and defeat
  drift detection entirely, which is the one guarantee the whole spec 6.4 loop rests on.
- **The shape.** `devbroker.aggregator_acl_grants(slug, aggregator_uuid)` returns seven
  `AclGrant(access, topic)` values built from the topic builders. `acl_file_text` renders
  `topic {access} {topic}`; `credentials.dynsec_role_acls` renders the plugin's vocabulary.
  Neither writes a topic literal.
- **`read` becomes TWO dynsec acltypes, and both are needed.** `subscribePattern` decides
  whether the SUBSCRIBE is accepted; `publishClientReceive` whether a matching message is
  actually delivered. Granting only the first produces a device that subscribes successfully and
  then receives nothing — indistinguishable, from the device's side, from a platform that never
  published. There is deliberately no `unsubscribePattern` grant: `defaultACLAccess.unsubscribe`
  is true and a device dropping its own subscriptions harms nobody but itself.
- **Asserted against the list rather than against two expectations.** Two renderer tests read
  `aggregator_acl_grants` and check each renderer against it; a third asserts the property about
  the LIST (no write grant on `desired`), because the first two would both still pass if the
  list itself grew the line. `test_dev_broker.py` passes unchanged, which is the regression proof
  that the ACL file's bytes did not move.
- **E5.8a moves this to `app/brokerconfig.py`.** It lives in `devbroker.py` for now because
  `acl_file_text` is its first caller and a move is a separate, provable step.
- **Reference:** `backend/app/devbroker.py`, `backend/app/services/credentials.py`,
  `backend/tests/test_broker_credentials.py`, phase-5 section 4 (E5.6 acceptance), spec 7.1/7.2.

## D119 (2026-08-12): Importing a conftest FIXTURE into a test module silently defeats session
scope, and it cost the rig its whole gate-time design (E5.4b)

- **The defect.** All four E5.4b-e modules opened with `from conftest import rig`. A fixture
  defined in `conftest.py` is discovered automatically; importing it binds a **separate fixture
  object** into the importing module, and `scope="session"` then applies **per copy**. The
  five-container rig was therefore built once per module.
- **Measured, not reasoned about.** Sampling `docker ps` during a run of the four suites:
  5 containers, then 10, then 15. After deleting the imports: a flat 5 for the whole run, and
  the four suites went from **49.3s to 22.1s**. Same 43 tests, same assertions, all passing
  before and after — which is exactly what makes this worth an entry.
- **Why it is dangerous rather than merely wasteful.** It is invisible. Nothing fails, no
  warning is emitted, and the only symptom is a slower gate — the phase-5 section 5 budget
  ("one session-scoped rig on one shared xdist group so it starts once per gate") would have
  been quietly untrue while the document still claimed it, and the pre-authorized response to
  a slow gate is to *cut container-test scope*. The cure for a self-inflicted 3x would have
  been deleting tests.
- **What the grouping does and does not buy.** `RIG_MODULES` sharing one `xdist_group` is still
  necessary — `--dist loadgroup` puts the group on one worker, and a session fixture is
  per-worker. But it is not sufficient, and this is the trap: with the imports in place the
  group was working perfectly and the rig was still built three times. Both properties are
  needed and neither implies the other.
- **The guard.** A comment at the import block in each of the four modules says why `rig` is
  absent from it. There is no automated check; the honest position is that the next fixture
  someone imports will do this again, and the measurement (`docker ps` during a run) is the way
  to catch it.
- **Reference:** `backend/tests/conftest.py` (`service_rig`, `rig`, `RIG_MODULES`,
  `RIG_GROUP`), `backend/tests/test_tester_{influx,prometheus,grafana,s3}.py`, phase-5 section
  5, D99 (the module-grouping hook this builds on).

## D118 (2026-08-12): An E5.3 test pinned "no tester exists yet" rather than a behaviour, and
E5.4a invalidated it (rule R0)

- **What changed.** `test_service_testers.py::test_the_endpoint_reports_nothing_while_no_tester_is_registered`
  read the live `testers.REGISTRY`, asserted the endpoint returned `results == []`, and now
  takes the module's empty-stub `registry` fixture instead. No assertion was removed or
  loosened; the same equality is asserted against a registry the test controls.
- **Why it broke.** E5.3 shipped `REGISTRY` deliberately empty and this test read it directly,
  so it was really asserting *"nobody has written a tester yet"* — a fact with a scheduled
  expiry date, which arrived the moment E5.4a registered `mqtt`. The endpoint's behaviour never
  changed: it still invents no verdict for a service with no tester. What the test named and
  what it checked had come apart.
- **Why the fix is the stub and not deleting the test.** The property is real and is worth
  keeping through E5.4b-e — a service with no tester must be absent from the results rather
  than carrying an invented one. Pinning the stub registry tests exactly that, and cannot rot
  again as the remaining four testers land.
- **How it was found, which is the part worth keeping.** E5.4a ran only its own new
  `test_tester_mqtt.py` (24 passed) and not the E5.3 suite it had just changed the inputs of,
  so this failure was invisible at the time it was introduced. It surfaced on the next broader
  run. The E5 epic gates at five checkpoints rather than per task (D107), and the compensating
  discipline that makes that safe is running the *affected* suites per unit, not only the new
  ones. **A unit that registers into a shared structure has to re-run every suite that reads
  it.**
- **It happened twice more, and the C2 gate is what caught both.** E5.4a's own
  `test_the_registry_carries_the_mqtt_tester_and_nothing_it_has_not_built` asserted
  `set(REGISTRY) == {"mqtt"}` — the same "nothing else exists yet" shape — and E5.4b-e expired
  it. And `test_e0_readiness.E0_ROUTES` had not been extended for E5.5's status endpoint, which
  is the same class of omission in the other direction: a deliberate addition that a
  completeness assertion has to be told about. Both were invisible to per-unit runs and both
  turned the full gate red. **Registry completeness now lives in exactly one place**
  (`test_service_testers.py`, pinned against `models.SERVICE_KEYS`) instead of being re-asserted
  by whichever module happened to be last.
- **Reference:** `backend/tests/test_service_testers.py`, `backend/tests/test_tester_mqtt.py`,
  `backend/tests/test_e0_readiness.py`, `app/services/testers/__init__.py`,
  D107 (the checkpoint cadence), D111 (the four tester outcomes).

## D117 (2026-08-12): "Is this service required" is a stored column, not an argument to
`roll_up` (E5.5)

- **The decision.** `deployment_service.required` (boolean, `NOT NULL DEFAULT true`, migration
  `b7d41f0c2e93`) carries whether a service has to reach `verified` for its deployment to.
  `roll_up(rows)` and `recompute(db, deployment_id)` take no `not_required` argument;
  `apply_test_results` writes the flag when a tester answers `not_required`, and sets it back
  to true on any real verdict.
- **Why not a parameter, which is what it was first.** Spec 16.2 makes object storage
  *conditionally* required, and the thing that discovers the condition is E5.4e's tester, which
  answers `not_required` when raw-audio upload is off. That answer exists only during a test
  run. But the rollup is recomputed on two paths that hold no test results at all: `PUT
  /deployments/{id}/services` recomputes after a save, and `test_services_status.py`'s invariant
  helper walks **every** deployment and recomputes from its rows. With the flag as a parameter
  both of those recompute a *different* answer than the test run stored — a deployment with
  object storage switched off reaches `verified` during the test and silently falls back to
  `pending_verification` on the next save.
- **Why that specific failure matters more than it looks.** Phase-5 fixed choice 2 denormalizes
  `services_status` onto `deployment` because E6.4's map and E7.4's Owner fan-out read it per
  deployment. It states that the correctness risk this takes on is answered "by making
  `roll_up` the only writer and asserting the invariant across the suite, not by arguing about
  it". A rollup that cannot be recomputed from the rows alone defeats exactly that guarantee:
  the invariant assertion becomes unsatisfiable, because the fact it would need is gone.
- **The direction of the default is deliberate.** `true`: a service is required until something
  says otherwise. A service wrongly required holds a deployment at `pending_verification` and
  names itself in the status endpoint; a service wrongly not-required lets a deployment reach
  `verified` while a store it depends on is unreachable, which by spec 16.5 also unblocks bundle
  generation.
- **The four in `ALWAYS_REQUIRED` are read from the constant and not from their rows**, so no
  verdict and no stray write can excuse `mqtt`, `influx`, `prometheus` or `grafana`. Spec 16.2
  makes only object storage conditional, and the column exists to express that one case.
- **Provenance.** This entry is written by the session that finished E5.5; the column, the
  model comment citing "D117" and the first draft of `status.py` were written by an earlier
  agent session that terminated mid-refactor, leaving the column with no migration and
  `status.py` still on the parameter form. See `project_planning/e5-progress-ledger.md`.
- **Reference:** `app/services/status.py` (`required_keys`, `roll_up`, `apply_test_results`),
  `alembic/versions/b7d41f0c2e93_service_required_flag.py`, `backend/tests/test_services_status.py`,
  phase-5 fixed choice 2, spec 16.2 and 16.5.

## D116 (2026-08-11): `app/services/clients/` may not import `app/services/testers/`, and the
credential set grew a deployment identity (E5.4a)

- **The cycle.** `testers/__init__` imports every tester so `REGISTRY` is populated at import
  time — which is what makes the endpoint's "read through the module" rule work. A client that
  imported `ServiceCredentials` from `testers/base` therefore closed the loop, and E5.4a hit it
  as a real `ImportError` on the first run rather than as a design worry.
- **The fix is the layering that was wanted anyway.** Clients dial and know nothing about
  verdicts, credential resolution or the tester framework; turning a `ServiceCredentials` into a
  client is the tester's job (`testers/mqtt.py::client_for`). Phase-5 fixed choice 8 promises E7
  will extend these clients, and a client that depends on E5's tester framework is a worse
  inheritance than one that does not. **The import cycle now enforces the rule**, which is
  better than a convention in a docstring: E5.4b-e cannot violate it without the suite failing
  at collection.
- **`ServiceCredentials` gained `deployment_id` and `deployment_slug`.** The MQTT tester's
  target topic is a function of the deployment — the reserved leaf is built through
  `contracts.mqtt.deployment_root` so it lands inside the platform account's single ACL grant —
  and `resolve_credentials` had no way to carry that. They are keyword-only and default to
  `None` because the other four testers dial a URL that contains no deployment identity: a
  required field that four callers pass through untouched is a field that eventually gets passed
  wrong. The MQTT tester rejects their absence explicitly rather than building a wrong topic.
- **Reference:** `app/services/clients/mqtt.py`, `app/services/testers/mqtt.py::client_for`,
  `app/services/testers/base.py::ServiceCredentials`; phase-5 fixed choice 8.

## D115 (2026-08-11): `app/services/dynsec.py` is created by E5.4a, one unit before the phase
document places it (E5.4a)

- **What moved.** The phase document names `app/services/dynsec.py` under **E5.6**. E5.4a needs
  the same thing E5.6 needs — a short-lived client that publishes to
  `$CONTROL/dynamic-security/v1`, correlates the reply on the response topic, and interprets an
  error — so the module is created here and E5.6 adds `mint` and `revoke` to it through the
  `call()` entry point that already exists.
- **Why, and what it is not.** The alternative was a probe living inside the MQTT tester with
  E5.6 unifying later, which guarantees two implementations of one round trip in the tree
  between now and then, and a refactor of working tested code at the moment E5.6 is trying to
  get credential minting right. **Nothing about E5.6's scope moves**: `BrokerCredentialProvider`,
  the `broker_credential` table, the mint and revoke endpoints and `aggregator_acl_grants` are
  all still E5.6's, and this module contains none of them. Decided by the owner when the
  question was put, rather than taken unilaterally.
- **The E5.6 constraints are already honoured here**, so E5.6 inherits them rather than
  re-establishing them: a dedicated short-lived client and never `MqttClientManager` (whose
  subscription set is fixed before `start()` per D64, and which only knows deployments that
  already have a row, while a test runs against candidate coordinates); `broker.py::tls_context`
  reused so D65's pinned-anchor property holds identically.
- **Reference:** `app/services/dynsec.py`; phase-5 section 4, E5.6; D64, D65.

## D114 (2026-08-11): The dynsec probe's three verdicts are decided by the SUBACK, not by the
publish — and the obvious discriminator does not work (E5.4a)

- **The requirement.** Phase-5 fixed choice 4 makes dynsec required for v1, so the probe's
  verdict is part of broker verification, and it has to be three-valued: `absent` (no plugin)
  and `denied` (plugin present, this account is not an administrator) have completely different
  remedies — "enable the plugin" versus "grant this account the admin role" — and are different
  people doing different things. A boolean collapses them.
- **What was measured, before any of it was written.** Three broker shapes, one probe:

  | broker | SUBACK on the response topic | reply |
  | --- | --- | --- |
  | `acl_file`, no plugin, platform account | Granted QoS 1 | none |
  | dynsec, client holding `admin` | Granted QoS 1 | well-formed |
  | dynsec, client without `admin` | Not authorized (0x87) / Unspecified error (0x80) | none |

- **The load-bearing surprise is the first row.** A Mosquitto using `acl_file` **grants** a
  subscription to a topic its file never mentions, and then silently refuses the matching
  publish (PUBACK reason 135, which aiomqtt does not surface). So the intuitive discriminator —
  "was the control publish refused?" — is identical for a broker with no plugin and a broker
  whose account lacks the role, and a probe built on it would report `denied` for the dev broker.
  The phase document requires `absent` there. **Dynsec refuses the SUBSCRIBE**, so the SUBACK is
  the thing that actually carries the information: a refused SUBACK means something is enforcing
  ACLs on the control topics, which is the plugin; a granted SUBACK followed by silence means
  nothing is listening on them at all.
- **A consequence worth having.** "The probe never publishes to `$CONTROL` on a broker it has not
  first confirmed accepts the connection" becomes structural rather than a rule someone has to
  remember — a refused SUBACK returns before the publish line is reached.
- **And a trap for whoever tests this next.** The plugin **consumes** a `$CONTROL` publish and
  never distributes it, so no subscriber — not even an administrator holding
  `subscribePattern $CONTROL/dynamic-security/#` — can witness one. The first version of that
  assertion used a watching subscriber and failed against the *authorized* case, which is how
  this was found. The broker's own log at `log_type all` is the only witness that exists, and
  `Broker.logs()` exists for it.
- **Reference:** `app/services/dynsec.py`; `backend/tests/test_tester_mqtt.py`;
  `backend/tests/conftest.py::dynsec_broker`; phase-5 fixed choice 4; spec 17 item 14.

## D113 (2026-08-11): D112's fix is adopted from the SIM branch rather than written again, and
D112's interim rule is retired

- **What changed.** `backend/tests/conftest.py` and `backend/tests/test_mqtt_manager.py` are
  taken verbatim from the SIM branch's gate-56 commit (`959ff23`, "Let several gate runs share
  one machine, and make one assertion decisive"). That commit implements two of the three
  candidate fixes D112 listed as owed: a machine-wide cross-process lock and port-claim
  registry (`gate_lock`, `free_port`, `GATE_STATE_DIR`), and host-side TCP readiness probes
  (`wait_for_host_port`) replacing the `docker exec` probes that asserted the wrong thing.
- **Why it had to be done here and now.** `e5-batch-1` was cut at `2875063` (SIM.1), which is
  *before* that commit, so the E5 worktree was still running the pre-fix harness while the SIM
  worktree was not. Two sessions were expected to work side by side from here, and only one of
  them was equipped to. E5 has never touched either file — verified with
  `git diff 2875063 HEAD -- backend/tests/conftest.py backend/tests/test_mqtt_manager.py`,
  which is empty — so this is a clean adoption and not a merge, and the two branches now carry
  byte-identical copies.
- **This is E0-owned test infrastructure, exactly as D112 said.** E5 neither designed nor
  modified it; E5 imported it. The rationale for every part of it lives in the SIM branch's own
  decision entries (the ones titled "The suite is safe to run from several processes at once…"
  and "The shutdown assertion classifies the survivor instead of timing it out…"), which arrive
  on `main` with the SIM pull request.
- **D112's interim rule is retired by this.** "Confirm no other session is running tests before
  you start" was a habit standing in for a fix; the fix is now present in this tree. What
  survives from D112 is its other half, which is still binding: a run that comes back with
  container-startup errors is an **invalid measurement** to re-run, never a red gate to record,
  and `docker_retry` is still not to be widened.
- **Numbering hazard for whoever merges.** Both branches appended to this file independently, so
  `D110` and `D111` name different decisions on each: on `e5-batch-1` they are the services PUT
  and the four tester outcomes; on `sim-batch-1` they are the concurrency fix and the shutdown
  assertion. Cross-references above are therefore by TITLE, not by number. Renumbering is the
  merge's job and must not be done inside either branch, because every commit message, ledger
  row and `project-updates.md` entry already written points at the local numbers.
- **Reference:** `docs/DECISIONS.md` D99, D112; `backend/tests/conftest.py`;
  `project_planning/e5-progress-ledger.md`.

## D112 (2026-08-11): The test harness is not safe to run twice at once, and that is a defect
to fix rather than a habit to work around

- **The defect.** Container fixtures publish Docker host ports, and two concurrent runs of the
  suite — two agent sessions, two worktrees, a gate beside a targeted run — make Docker
  Desktop's port forwarder return `/forwards/expose returned unexpected status: 500`.
  `conftest.docker_retry` already retries that exact string five times with a backoff (D99) and
  still exhausts under two sessions, because the retry was tuned for one gate's INTERNAL xdist
  concurrency and not for two gates at once.
- **Why it is worth a decision entry rather than a shrug: the failure is indistinguishable from
  a red gate at a glance, and it is not one.** Measured twice on 2026-08-11 from the E5
  worktree while the SIM session was live: 829 passed / 7 errors, then 831 passed / 5 errors.
  Every error was a container fixture failing to start, **none was a test-logic failure**, and
  the errored modules moved between runs (`test_e0_readiness`; then `test_publish_revision` and
  `test_end_to_end_loop`), each passing on its own immediately afterwards. Under rule R0 a
  session that reports this as a red gate is wrong, and a session that reports it as a pass is
  worse. It is an **invalid measurement** — the run has to be repeated, not interpreted.
- **The interim rule, binding until the fix lands.** Before `make gate` or any Docker-backed
  suite, confirm no other session is running tests in this repository. A run that comes back
  with container-startup errors is re-run with the other session idle; it is neither recorded
  as a gate result nor written into `project-updates.md`.
- **What NOT to do.** Do not widen `docker_retry` to swallow this. It is narrow on purpose
  (D99): a retry loop around every Docker failure turns a genuinely broken image or a bad flag
  into a slow, silent timeout, which is a far more expensive failure than this one. Widening it
  hides the contention instead of removing it.
- **The fix, owed and not yet scheduled.** Make the harness safe to run concurrently rather
  than asking every session to remember not to. The candidate approaches, in the order they
  look worth trying:
  1. **Stop publishing host ports at all** for the container fixtures that do not need a
     stable one — talk to containers on the Docker network by container IP, or run the tests
     themselves in a container on that network. The forwarder is the thing that fails; not
     using it removes the whole class. `free_port()` exists precisely because a few tests need
     a STABLE host port across a restart, so those are the only ones that would still need
     publishing.
  2. **A cross-process lock** around port-publishing container starts (an OS file lock under
     the repo root), so two sessions serialize at the one operation that cannot take
     concurrency, rather than serializing whole gates.
  3. **Name the ports deterministically per worktree** from a hash of the checkout path, so two
     worktrees cannot contend for the same number even when they publish at once.
  Option 1 is the real fix; option 2 is the cheap one that would have prevented both invalid
  runs above. This is E0-owned test infrastructure and does not belong to E5 — E5 recorded it
  because E5 is where it cost two five-minute sweeps to diagnose.
- **Reference:** D99 and the gate-53 commit (which first identified the forwarder fault and
  added the narrow retry); `backend/tests/conftest.py::docker_retry`,
  `ephemeral_postgres`, `ephemeral_broker`; `project_planning/e5-progress-ledger.md`.

## D111 (2026-08-11): A tester says four things, not three, and two of them are not failures
(E5.3)

- **Decision:** `TesterOutcome` is `pass` | `fail` | `not_required` | `not_configured`. It is a
  DIFFERENT vocabulary from spec 16.2's per-service status (`untested` / `verified` /
  `failed`), and E5.5 maps one onto the other rather than the two being one enum.
- **Why not just `pass`/`fail`.** Spec 16.2 makes object storage required *only when raw-audio
  upload is enabled*, and the S5 wizard shows all five services from step one — so a
  deployment that will never use S3, and a deployment whose operator has not reached the
  Grafana step yet, would both render red. Red that an operator is supposed to ignore is worse
  than no signal at all, because it destroys the meaning of the red that matters. E5.4e's
  acceptance already names `not_required` for the first case; `not_configured` is the second,
  and it is the runner's rather than any tester's, because "there is nothing to dial this with"
  is decided before a tester is entered.
- **Why they are not folded into the status vocabulary.** "I did not run" and "I ran and it
  failed" are different facts about the world, and `deployment_service.status` is the record of
  a *verdict*. Collapsing them would make `untested` mean two things, and E5.5's
  `consecutive_failures` counter — which spec 16.5 needs to demote "on repeated failure" — would
  start counting deployments that were never dialled.
- **Containment is per tester, and it is the framework's job.** A timeout, an unexpected
  exception, a result filed under the wrong `service_key`, and a missing credential each become
  that one service's `fail` (or `not_configured`), and the other four verdicts stay real. S5's
  own caption says one failure never blocks reading the other four; that is a property of the
  runner, not something five tester authors have to each remember.
- **A crash reason names the exception TYPE and never its message.** `str(error)` is precisely
  how a credential reaches an API response — `httpx` puts the request URL in its messages and a
  URL can carry a token in a query string. The suite raises an exception with a token in its
  message on purpose and fails if that token appears in the result, in `caplog`, or in the audit
  row.
- **Two budgets, both enforced.** Each tester declares `budget_seconds`; the whole call has
  `WHOLE_CALL_BUDGET_SECONDS` over the top. The second is not redundant: a tester that blocks
  the event loop between awaits, or one that declares a budget longer than a caller will wait,
  is bounded by the endpoint rather than by its own good behaviour. Both are asserted with a
  clock.
- **`POST .../services/test` is `MANAGE_SERVICES`, not a read permission**, because the body
  carries candidate credentials — spec 16.2 validates an entry "before accepting it", so the
  unsaved form is exactly what it tests. **It writes no status**: E5.5 owns that, so re-running
  a test can never itself change a verdict of record.
- **`REGISTRY` ships empty.** E5.4a-e fill it. A tester registered before it exists would make
  the endpoint report a verdict nothing computed, which is a worse answer than "this service has
  no tester yet".
- **Reference:** phase-5 §4 task E5.3, spec 16.2 and 16.5; `backend/tests/test_service_testers.py`;
  `docs/INTERFACES.md` "Owned by E5".

## D110 (2026-08-11): The services PUT is a partial collection of wholesale members, and it
has no delete (E5.2)

- **Decision:** `PUT /deployments/{id}/services` takes an object of up to five services. A
  service **present** in the body is written wholesale — every field the caller omits is
  cleared, the `put_overrides` and E1.7 tags precedent. A service **absent** is left
  completely untouched. There is no way to delete a service row through this endpoint.
- **Why the two halves differ, when "PUT is never a merge" would suggest one rule.** They are
  answers to two different questions. *Within* a service, a merge means a field the operator
  cleared silently keeps its old value, which is the bug the tags precedent exists to prevent.
  *Across* services, wholesale replacement would mean the S5 wizard's per-service step has to
  resubmit the other four services on every save — including four credentials it does not
  hold, which is precisely what the write-only design forbids it from having. So the
  collection is partial and its members are total.
- **No delete, because the `mqtt` row is not optional.** `devbroker.register_services` writes
  one for every deployment and `load_broker_coordinates` reads it; deleting it would strand
  the deployment's control plane with no route back except a migration. E5.1 already
  established the right analogy — service rows are infrastructure attached to the deployment,
  so `DELETE /deployments/{id}` removes them all and nothing removes them one at a time.
  Unsetting a *credential* is fully supported (omit the field); that is a different act.
- **The mqtt password is required at the boundary, not merely by the database.**
  `MqttSettings.password` is typed `str | KeepSecret` with no `None`, because the
  `mqtt_coordinates_required` CHECK makes `password_secret_name` NOT NULL for an mqtt row: an
  omitted password would be an `IntegrityError` the catch-all turns into a 500. Same rule as
  E5.1's — the database is what makes it true, and the boundary is what makes it a 422.
- **The keep sentinel is a Pydantic model, not a dict comparison.** `KeepSecret` with
  `extra="forbid"` means `{"$secret_set": false}` or a stray key is a located 422 rather than
  being quietly treated as a plaintext value that happens to look like an object. The sentinel
  VALUE is still `app.config.validation.KEEP_SENTINEL` — reused, not reinvented, so the
  services form and the config editor round-trip secrets the same way.
- **Reference:** phase-5 §4 task E5.2, D51, D110's own suite
  `backend/tests/test_services_api.py`; `docs/INTERFACES.md` "Owned by E5".

## D109 (2026-08-11): A fourth cross-epic edit, forced rather than chosen: an under-specified
broker row is skipped, not fatal (E5.1)

- **Decision:** `load_broker_coordinates` in `app/controlplane/broker.py` now skips a
  `deployment_service` row missing any of `host`, `port`, `username` or `password_secret_name`,
  logging a warning that names the deployment slug and the missing **column names** and never a
  credential. This sits beside the existing `SecretStoreError` skip and follows the same D64
  rule: one badly provisioned deployment must not deafen the others.
- **Why it is a fourth edit when D108 says three, and why that is not a boundary being quietly
  widened.** D108 authorized three *discretionary* cross-epic edits. This one is **forced**:
  E5.1 makes those four columns nullable so non-`mqtt` service rows can exist at all, which
  turns `Mapped[str]` into `Mapped[str | None]` and makes the existing loader body fail
  `mypy --strict`. Something had to change there. The alternatives were to raise (a single
  malformed row would then take down every deployment's control plane, which is exactly what
  D64 rejected) or to cast the nulls away (which converts a type error into a runtime one at a
  worse moment). Skipping is the answer the module already gives to the neighbouring case.
- **It is unreachable while the schema holds.** The new
  `ck_deployment_service_mqtt_coordinates_required` CHECK makes an `mqtt` row with a null host
  impossible, and non-`mqtt` rows are never loaded by this function. The guard is defence
  against a future migration, not against present data — the code comment says so, and its test
  has to drop the constraint to reach it.
- **The count in the phase document and the ledger was corrected rather than left to drift.**
  Both now say three discretionary plus this one forced, with the distinction stated. A record
  that says "exactly two" while the tree contains four is worse than no record.
- **Reference:** D64, D108; phase-5 §2 and task E5.1; `backend/tests/test_services_model.py`.

## D108 (2026-08-11): Three cross-epic edits are authorized in advance, and a fourth is a
stop-and-ask (E5.0)

- **Decision:** epic E5 may make exactly three edits outside its own surface, named here
  before any of them is written so the boundary is a document and not a judgement call made
  at 2am. Two are E3-owned and both land in task E5.7b, so the whole cross-epic surface is
  one diff a reviewer can read at once. One is E2-owned.
- **`MqttClientManager.refresh()` (E3.2).** `INTERFACES.md` states as a contract that
  "coordinates load once, at `start()`. Adding a deployment's broker row takes a manager
  restart; E3.7 owns that lifecycle." That was an honest limitation while nothing changed
  broker rows at runtime. E5 changes them as a matter of course — Path B writes a new one,
  rotation changes a password, a new deployment gets its first — so it becomes a bug the
  moment E5 ships. `refresh()` re-runs the loader, diffs by `deployment_id`, and starts,
  cancels or restarts tasks; the diff is three lines because `BrokerCoordinates` is a frozen
  dataclass and a rotated password therefore *is* a difference. `start()`'s semantics are
  unchanged. A poll beats `LISTEN`/`NOTIFY` here: a new deployment's broker cannot be dialled
  before the operator has finished configuring it anyway, so a channel and a payload contract
  buy nothing but a second delivery path to keep correct.
- **`service_config_sweep` on `ReconciliationWorker` (E3.7).** Spec 16.4 requires service
  settings to be published "as soon as the device exists in inventory", so a device created
  *after* the services save must still get its retained config. The sweep runner is already a
  generic `(name, interval, callable)`; this is a third entry, not new machinery.
- **`DevicePlan.changed_keys` (E2.6).** It compares raw before/after maps including
  write-restricted keys, while its sibling `snapshot_from_raw` correctly strips them from
  listener snapshots. So one services save marks every Listener changed and mints a revision
  per listener whose snapshot is byte-identical to the previous one — on a SIM fleet, ~600
  pointless revisions and retained publishes per save. Computing `changed_keys` from stripped
  snapshots fixes it at the source and makes preview honest at the same time.
- **What stays untouched:** `app/contracts/mqtt.py`, `app/controlplane/consumer.py`,
  `app/controlplane/revision_state.py`, `app/config/merge.py`, and every assertion in the four
  test-critical suites. A fourth cross-epic edit is a stop-and-ask under rule R2, not a
  judgement the implementing session gets to make.
- **Reference:** spec 16.4; phase-5 §2 and tasks E5.7a/E5.7b; project-changes #24.

## D107 (2026-08-11): E5 gates at five checkpoints rather than per task, and nothing unverified
reaches the remote (E5.0)

- **Decision:** for epic E5 only, the full `make gate` runs at five checkpoints (C1-C5 in the
  phase document) rather than at the end of every numbered unit; each unit ends with its own
  targeted tests, `ruff` and `mypy app` instead. This is a deliberate deviation from rule R0's
  "every numbered task ends with a gate", taken by the owner on wall-clock grounds: E5 is
  eighteen units, and eighteen full gates is roughly ninety minutes of pure gate.
- **The compensating discipline, which is what actually preserves R0's guarantee:** nothing
  reaches the remote without a full green gate. Commits between checkpoints are local and
  unpushed. The push and the tag remain the gated events, a checkpoint is green only on 0
  failed / 0 skipped / 0 xfailed / 0 deselected across the ENTIRE accumulated suite, and a red
  gate is still never committed, never pushed, and never summarized as a pass. Every rule R0
  prohibition on *how* a gate may be run — no skip markers, no `-k`, no `--maxfail`, no
  allowlists, Docker a hard prerequisite — is untouched.
- **Why this is safe here and is not a precedent.** What R0 buys is that no broken state is
  ever built upon and no broken state is ever published. The second half is preserved exactly.
  The first is weakened to a five-unit window, which is bounded by keeping the units within a
  checkpoint tightly related — a checkpoint is a coherent slice (the data model, the testers,
  the delivery path, the stack, the UI), so a failure surfaces among code written for one
  purpose rather than across the epic.
- **Reference:** rule R0; phase-5 §2 "Process choices"; e5-progress-ledger.md.

## D106 (2026-08-11): `MANAGE_SERVICES` and `VIEW_SERVICES` join the permission enum (E5.0)

- **Decision:** E5 adds two permissions rather than reusing `MANAGE_CONFIG`. Manage goes to
  Owner and Deployment Operator; view goes to all four roles. `app/auth/rbac.py`,
  `backend/tests/test_rbac.py` and `frontend/src/lib/rbac.ts` all gain rows.
- **Why not reuse.** `MANAGE_CONFIG` is held by Field Tech's neighbours and by Deployment
  Operator, and a services PUT writes a deployment's Influx admin token, S3 secret key,
  Grafana service-account token and broker password. A Field Tech's role is provisioning
  hardware in the field; handing that role the deployment's keys to everything because the
  endpoint happens to live near config would be an access-control decision made by
  convenience. Separating read from write matters too: status must render for a Viewer,
  because a Viewer looking at a map needs to know the deployment is degraded.
- **Extending a test-critical suite is permitted; weakening it is not.** `test_rbac.py` is one
  of the four spec 14.5 components. Every existing assertion stays; the new permissions are
  additional rows in its table, and the frontend parity test covers them the same way.
  `rbac.py`'s own docstring authorizes exactly this shape: "later epics extend the enum and the
  map deliberately, never ad hoc."
- **Reference:** spec 12.3, 14.5, 16.2; phase-5 §2 fixed choice 9; project-changes #24.

## D105 (2026-08-11): `BrokerCredentialProvider` is defined by E5 and consumed by E4, reversing
the phase-4 ordering (E5.0)

- **Decision:** `phase-4-provisioning.md` §2 fixed choice 1 says E4.6 ships the provider seam
  and "E5.6's entire job is to add a dynsec provider and flip the default". That ordering
  assumed E4 landed first. E4 has not been started and E5 is being built now, so the
  dependency reverses: **E5.6 defines the protocol** (`mint`, `revoke`, `state`) and ships
  `DynsecCredentialProvider` plus `DevBrokerCredentialProvider`.
- **E4.6's job is unchanged in substance.** It still chooses a provider and flips
  `EOE_BOOTSTRAP_CREDENTIALS`; it simply imports the protocol instead of declaring it. Phase 4
  carries addendum PHASE4-2-01 saying so, so a session picking up E4 does not write a second
  interface and then discover it already exists.
- **Why not wait for E4.** E5.6 needs to mint credentials regardless — the generated stack in
  spec 16.3 pre-creates the platform account and the deployment-namespace role, and the whole
  point of dynsec minting is that per-device credentials exist before hardware ships. Building
  it without a named interface, for E4 to later wrap, would be the same work with the seam
  discovered afterwards rather than designed.
- **Reference:** spec 16.4; phase-4 §2 fixed choice 1; phase-5 §2 fixed choice 6;
  project-changes #26.

## D104 (2026-08-11): dynsec is required for v1, closing spec 17 item 14 (E5.0)

- **Decision:** the platform mints per-device broker credentials through Mosquitto's dynamic
  security API, and a broker without the plugin cannot be verified. Spec 16.4's manual-install
  fallback — generate the credential pair, present it to the operator, hold the bundle until
  they confirm — is **not built**. Spec 17 item 14 offered exactly this choice and the owner
  took the requiring branch.
- **What it deletes.** A second `BrokerCredentialProvider` implementation, a
  `pending_manual_install` state and its confirm endpoint, a held-bundle predicate E4 would
  have had to consult, a wizard branch, and the class of deployment that is half-provisioned
  because someone was going to paste an ACL into a broker host and did not. Every one of those
  is a place where a device could end up unable to reach the control plane for a reason nobody
  is watching.
- **What it costs, stated rather than hidden.** An operator running a Mosquitto without
  `dynamic_security.so` must enable it before their deployment can be verified. The MQTT
  tester's failure message names the plugin and what to add to `mosquitto.conf`, and the
  probe distinguishes "plugin absent" from "your platform account is not an admin" because
  those have completely different remedies.
- **The consequence that had to be built, not just written.** The dynsec verdict is part of
  broker verification, so `absent` and `denied` both keep `services_status` off `verified` —
  which by spec 16.5 blocks provisioning-bundle generation, since the bootstrap block embeds
  broker credentials that would not exist.
- **Item 14 is now closed.** Item 13 (Chameleon Cloud VM auto-provisioning) stays open and
  stays out of scope; Path B still ends at a downloadable bundle.
- **Reference:** spec 16.2, 16.4, 16.5, 17 item 14; phase-5 §2 fixed choice 4 and task E5.6;
  project-changes #25; spec addendum SPEC-17-01.

## D103 (2026-08-11): A mock Aggregator announces `offline` when it leaves politely, and the
LWT acceptance kills a real process (SIM.1)

- **Decision:** `MockAggregator.disconnect()` publishes a retained `offline` `StatusMessage`
  before closing, and `__aexit__` calls it. MQTT discards the will on a clean DISCONNECT, so a
  harness that merely closed its socket would leave `online` retained on the status topic
  forever and the platform would go on painting a machine that is not there — for the rest of
  the deployment's life, since the value is retained. SIM.4's "shutdown is clean, publishing an
  explicit offline" is therefore already true of the device rather than bolted onto the runner.
- **Which makes the crash test possible, and that is the point.** A harness whose only exit
  looks like a crash cannot be used to test a crash. So the LWT acceptance runs a whole
  `MockAggregator` in a subprocess and SIGKILLs it: no DISCONNECT packet ever reaches the
  broker, the BROKER composes and publishes the will, and the platform's own consumer picks it
  up off the subscription it already had. Nothing in the test simulates the will, which is the
  same discipline `backend/tests/test_lwt_status.py` applies to `mosquitto_sub`.
- **The subprocess is driven entirely through the environment** (`SIM_HOST`, `SIM_USERNAME`,
  `SIM_PASSWORD`, ...) and prints one line when it is up, so the test waits on the device
  having connected rather than on a sleep. The kill happens in a `finally`: it is the test's
  action and its cleanup at once, and a device left running would outlive the fixture that
  provisioned it and go on publishing into a torn-down broker.
- **Reference:** spec 7.2, 9.3; phase-SIM SIM.1; sim/device.py; sim/tests/test_mock_aggregator.py.

## D102 (2026-08-11): `/sim`'s conftest loads the backend's by path, under a name of its own
(SIM.1)

- **Decision:** `sim/tests/conftest.py` loads `backend/tests/conftest.py` through
  `importlib.util.spec_from_file_location` under the alias `eoe_backend_conftest` and
  re-exports `ephemeral_broker`, `ephemeral_postgres`, `free_port`, `make_kek` and
  `docker_retry`. There is one Mosquitto fixture in this repository and it stays that way
  (phase-SIM section 2); a second one would drift from the first the day one of them learned
  something about Docker Desktop the other did not.
- **Why not `import conftest`.** pytest names a conftest module after its basename, so by the
  time sim's conftest runs, `sys.modules["conftest"]` is *sim's own* — a plain import would
  hand back this half-built module and fail on the first name taken out of it. The explicit
  alias sidesteps the collision instead of depending on `sys.path` order between two
  directories that both contain a file called `conftest.py`.
- **Loading it also loads the derandomized hypothesis profile** the backend registers in that
  module's body, which is what keeps the checksum cross-check green or red for a reason rather
  than by luck. The D99 parallel conventions are copied rather than inherited — the module is
  loaded as a plain module, so its hooks do not run — including the `tryfirst`
  `pytest_collection_modifyitems` that assigns an `xdist_group` per module, for the same reason
  it exists in the backend: every live test here starts its own Postgres and Mosquitto, and a
  module split across workers would start them once per worker.
- **Reference:** D99; phase-SIM section 2; sim/tests/conftest.py.

## D101 (2026-08-11): SIM's checksum is reimplemented from D52's prose, and the harness/suite
import boundary is enforced by tests rather than promised (SIM.1)

- **Decision:** `sim/checksum.py` implements the D52 recipe from its written description and
  never imports `app.config.canonical`. It goes further than the platform's one-line spelling
  on purpose: "keys sorted at every depth" is implemented as a recursive rebuild of every
  mapping in sorted key order rather than as `json.dumps(sort_keys=True)`, so the cross-check
  is comparing two readings of the sentence and not one implementation with itself.
  `tests/test_checksum_agreement.py` asserts byte-for-byte agreement over generated snapshots
  and over a table chosen for where encoders diverge — nested key order, non-ASCII, floats
  versus ints, empty containers, nulls inside `config`, secret markers.
- **Why it matters that this is a reimplementation:** real firmware is given the recipe, not
  the function. A simulator that called the platform's own code would prove only that the code
  is self-consistent, and would stay green on the day the written recipe and the code stopped
  agreeing — which is the day every device in the field starts reporting a checksum that can
  never match and the whole fleet reads as drifted for a reason no operator can see.
- **The boundary is a test, not a convention.** `tests/test_harness_boundaries.py` fails if any
  file under `/sim` spells the topic namespace out by hand (the forbidden prefix is taken from
  the contract's own `ROOT`, so the test cannot pass by agreeing with itself), and if any
  harness module imports anything from the platform other than `app.contracts.mqtt`. The SUITE
  is deliberately exempt from the second rule and only the second: an acceptance test that says
  "against a real platform" has to drive one.
- **Reference:** D52; spec 6.2, 7.3; phase-SIM section 2 and SIM.1; sim/checksum.py.

## D100 (2026-08-11): `/sim` is its own uv project that reaches the platform by path, and its
test group carries the platform's runtime dependencies (SIM.1)

- **Decision:** `sim/pyproject.toml` is a separate uv project. Its RUNTIME dependencies are a
  device's and nothing more — `aiomqtt` and `pydantic` — because the harness stands in for
  firmware, and a runtime dependency here that a real Aggregator would not carry is the first
  step towards a mock that can only be satisfied by a mock. The platform is reached by PATH
  (`pythonpath = [".", "../backend"]` for pytest, a `sys.path` insert in `device.py` for a
  plain `python fleet.py`, `mypy_path = "../backend"` for the type checker), never installed:
  E0 owns the packaging decision and nothing here needs it changed.
- **The dev group carries the backend's whole runtime set, verbatim.** SIM.1's acceptance runs
  a REAL platform in the test process — its API, its publisher, its reconciliation worker, its
  consumer — because "a published revision reaches `applied`" is a claim about the platform and
  cannot be made against a stub. That needs SQLAlchemy, psycopg, alembic, FastAPI and the rest
  importable from sim's venv. `tests/test_harness_boundaries.py` fails if the two lists drift,
  because the alternative is discovering a missing package weeks later as an ImportError naming
  something nobody remembers deciding to need.
- **mypy covers the harness modules, not the suite** (`files = ["checksum.py", "device.py"]`,
  so a bare `uv run mypy` is the whole check). The backend excludes its own tests for the same
  reason: the shared fixtures they call are deliberately untyped, and strict mode would report
  every call to them as an error about the fixture rather than about the test. Type-checking
  the harness against the real `app.contracts.mqtt` is the part that earns its keep — a payload
  field renamed on the platform side now fails at the contract's first outside caller.
- **Ruff and mypy settings are the backend's, asserted equal rather than copied by hand**, so a
  `/sim` on different rules cannot fail CI for formatting nobody chose (rule R2).
- **Reference:** phase-SIM section 2 "Fixed choices", SIM.1; sim/pyproject.toml.

## D99 (2026-08-11): The backend suite runs in parallel, grouped by module (SIM.0)

- **Decision:** `-n 6 --dist loadgroup` with a `tryfirst` `pytest_collection_modifyitems` hook
  in `tests/conftest.py` that marks every test with an `xdist_group` named after its module.
  **Backend gate time: 541s → 260s.** On the owner's instruction, after a run of gates where
  the waiting was the dominant cost of doing the work.
- **Grouped by MODULE, and it has to be.** Nearly every suite here hangs a module-scoped
  Postgres or Mosquitto off a fixture, and many are deliberately order-dependent within their
  file — `test_uniqueness` creates `sensor`, then asserts the next create becomes `sensor-2`.
  Per-test distribution scatters those across six workers, each paying for its own container
  and each seeing none of the others' state. Observed exactly that: six tests, six workers,
  four failures.
- **`tryfirst` is not decoration.** xdist reads the `xdist_group` mark inside its OWN
  `pytest_collection_modifyitems` and bakes the group into the nodeid there. A mark added after
  that hook runs is never seen, and everything scatters as if unmarked — silently, since the
  suite still runs. That cost a red gate to find, which is why the hook says so in its
  docstring.
- **The fixed-port modules share one group.** `test_compose_stack` and `test_verify_tool` both
  bring the real deploy stack up on the `FIXED_PORTS` pins, and there is exactly one host port
  15173. A shared group is xdist's own guarantee of "same worker", therefore never concurrent.
- **`docker_retry` for the forwarder, and only the forwarder.** Docker Desktop returns
  `/forwards/expose returned unexpected status: 500` when several containers publish ports at
  once; it also did so serially, twice, during this task. The helper retries exactly three
  known-transient strings and passes anything else straight back on the first attempt, because
  a blanket retry would turn a genuinely broken image into a slow silent timeout. `docker run
  --name` retries remove the half-created container first, or the retry reports "name already
  in use" and hides the real fault.
- **R0 is untouched.** The whole suite still runs, unfiltered, in one invocation; the gate
  guard's counts are unaffected. Parallelism changes how long the truth takes to arrive, not
  what counts as true.

## D98 (2026-08-11): The suite carries a 300-second per-test deadman switch (SIM.0)

- **Decision:** `addopts` gains `--timeout=300 --durations=10` (pytest-timeout, a dev
  dependency). Any single test still running after five minutes is killed and NAMED. The five
  tests that legitimately build container images carry `@pytest.mark.timeout(1200)`, because a
  cold `docker build` of three images is honest work, not a hang.
- **Why, on the owner's instruction:** a wedged Docker socket, a broker container that never
  accepts, or a lost port forward used to hold the entire gate hostage with no output — you
  learn nothing for ten minutes and then learn nothing at all. A timeout converts that into a
  named failure at a bounded cost, which is the difference between a slow gate and an
  unusable one.
- **What this does NOT do, stated plainly:** it does not make the gate faster. The backend
  suite takes ~8.5 minutes because it is 766 tests, most of the wall clock being real
  Postgres and Mosquitto containers starting for integration tests — not because anything
  hangs. A cap below that would fail the gate by construction rather than speed it up.
  `--durations=10` prints the ten slowest tests every run, so the cost is visible and
  attackable with evidence rather than guesswork.
- **R0 is untouched.** A timeout kill is a FAILURE, not a skip, so the gate guard's
  skipped/xfailed/deselected counts are unaffected and no test can quietly opt out by being
  slow.

## D97 (2026-08-11): "No tasks outlived stop()" is asserted with a settle window, not an
instantaneous snapshot (SIM.0, fixing an E3.2 test)

- **The failure.** SIM.0's gate went red on
  `test_mqtt_manager.py::test_shutdown_leaves_no_running_tasks` —
  `tasks outlived stop(): ['Task-1395']`, the task being aiomqtt's own
  `Client._misc_loop()`. The same test passed in a standalone `sh gate.sh backend-tests` run
  twenty minutes earlier. A test that passes and fails on identical code is a flake, and a
  flake in a gate is worse than a red: it makes rule R0 a coin toss.
- **The cause, at the library boundary.** aiomqtt cancels `_misc_loop` from paho's socket-close
  callback with `self._loop.call_soon_threadsafe(self._misc_task.cancel)` (client.py:716-717),
  and its `__aexit__` (client.py:798) never awaits that task. The cancel is therefore SCHEDULED
  on the event loop, not performed, by the time `stack.aclose()` returns. A task cancelled but
  not yet reaped reads as `not done()`. Under full-gate load — other stages' containers
  competing for the host — that window widens, which is precisely why it was invisible in the
  quiet standalone run.
- **The test was wrong, not the manager.** `stop()` cannot make a third party's
  `call_soon_threadsafe` synchronous without reaching into private attributes of aiomqtt, which
  would be a worse defect than the flake. What `stop()` can honestly promise is that nothing
  survives it — and that is what is now asserted, by `_tasks_outliving()` polling for up to 5
  seconds.
- **The assertion keeps its teeth.** The leak D94 was written for — a `_connection_loop` task
  that survived cancellation with a live socket under it — is still running when the deadline
  expires, and still fails. Only cancellation already in flight is tolerated, and only for as
  long as it takes the loop to run one scheduled callback. This is not a retry-until-green
  loop: the failure mode it was built to catch is permanent by nature.
- **Fixed under R0 rather than re-rolled.** The rule's escape hatch for a wrong test is to fix
  it and record why, not to run the gate again and take the green. `_close_client` and D94's
  shielded-teardown machinery are unchanged; the production path was already correct.

## D96 (2026-08-11): Apply publishes AFTER it commits, and one broker's outage does not
fail the rest (E3.13)

- **Decision:** `POST /config/apply` writes the overrides and the draft revisions in one
  transaction, COMMITS, and only then publishes. An operator's config edit is durable the
  moment they apply it; a broker that is down costs them a publish, not their work.
- **Decision:** a revision that could not go out stays `draft` and is REPORTED as such, per
  revision. `POST /revisions/{id}/publish` retries it — the same route drift repair uses
  (D82), so there is one publish path and one set of refusals rather than a second one grown
  here.
- **Decision:** one failure does not abort the rest. A fleet-wide apply spans deployments
  whose brokers are independent, and failing the whole call over one unreachable broker
  would leave the operator unable to tell which devices were told.
- **`ApplyOut.state` gains `partial`.** With several deployments in one apply, "some devices
  were told and some were not" is a real outcome and neither `draft` nor `pending` describes
  it honestly. `revisions[].state` carries the per-device truth; the top-level value is a
  summary.
- **`EOE_PUBLISH_ENABLED` now defaults ON** (D61, as task E3.13 specifies). Publication still
  only reaches a deployment that HAS a `deployment_service` broker row, and the flag remains
  settable per environment for anyone who wants to stage config without touching devices.
  Two E2 assertions that pinned the flag as off were rewritten rather than deleted, and now
  assert the honest consequence: the flag being on is not enough — a process holding no
  outbound connection (D86) still reports `draft`.
- **The end-to-end test is the epic's definition of done**, and deliberately the only test
  that spans every task: preview, apply, retained desired message read by the device on its
  own credential, ack, `applied`, timeline, websocket. A red there with green everywhere else
  means the pieces do not fit rather than that a piece is broken.

## D95 (2026-08-10): Live updates are invalidation signals, and `unknown` is a status
(E3.12)

- **Decision (the bus):** `publish()` issues `pg_notify` INSIDE the caller's transaction.
  Postgres delivers it only on commit, so a browser cannot be told about a transition that
  was rolled back — no outbox, no ordering to arrange, no window of a UI showing something
  that did not happen. `test_an_event_reaches_a_listener_only_after_the_transaction_commits`
  pins both halves.
- **Decision (the client):** events are INVALIDATION SIGNALS and never data. The hook
  refetches; it never patches a cache from an event body, and on every (re)connect it
  invalidates everything. `NOTIFY` is best-effort — a browser that reconnects has missed
  whatever happened while it was away — so a patched cache would be a confidently stale
  screen, which is worse than a slightly late one.
- **Decision (scoping):** applied on the server, per event, per connection. A websocket is a
  long-lived read of everything happening in the platform; filtering in the browser would be
  no filtering at all. A client may narrow its channels and can never widen its scope, which
  is why the inbound reader needs no authorization of its own.
- **Decision (D40 lifted, and rewritten):** status is real now, derived in ONE place
  (`device_status.py`) from LWT, spec 6.5 liveness and revision state. The guard test is not
  deleted: it now asserts that a chip renders only where the API reported a status, that
  `unknown` never renders as one of the six, and that config routes still show none.
- **`unknown` is a first-class status**, and this is the part most likely to be "simplified"
  later. A device entered in inventory but never heard from has no status; defaulting it to
  healthy would report a deployment that has never come online as working. It renders as a
  muted dash, visibly not a chip.
- **Reachability outranks reconciliation** in the roll-up: an offline device shows `offline`
  even when its revision has drifted, because the drift cannot be repaired until the device
  is back and `drifted` would send an operator to fix a config on an unplugged box.
- **A slow browser is dropped from, not waited on.** The hub's per-subscriber queue drops
  when full: losing live updates for one stalled client beats stalling the bus for everyone.

## D94 (2026-08-10): The broker client is closed OUTSIDE its own cancellation (E3.2 defect,
found by a gate flake)

- **What went wrong:** `test_shutdown_leaves_no_running_tasks` failed twice across this batch
  with `tasks outlived stop(): [...Client._misc_loop...]`, and passed every time it was run
  alone. A test that only fails under load is a latent gate flake, and re-running until green
  would have been the wrong answer.
- **The cause, and it was a real bug:** `_connection_loop` held the client in an
  `async with`, with `except asyncio.CancelledError: raise`. aiomqtt's `__aexit__` AWAITS —
  it sends DISCONNECT, then cancels its own internal `_misc_loop`. Re-raising there runs
  that teardown inside a task whose cancellation is already pending, so its first await
  raises again, the cleanup is abandoned half-done, and `_misc_loop` is left running with a
  live socket under it. Under load the window is wide enough to hit.
- **Decision:** the client lives in an `AsyncExitStack`, and `_close_client` closes it in its
  OWN task, shielded, then awaits that task to completion even after the shield re-raises.
  The teardown therefore always finishes before `stop()` returns.
- **Why it matters beyond a green gate:** every reconnect takes this path. A leaked
  `_misc_loop` per reconnect is a slow leak of tasks and sockets in a process designed to
  run for months across brokers that come and go — the flake was the symptom, not the
  disease.
- **Verified** by running the suite three times clean and the shutdown test again under
  deliberate CPU contention, which is the condition that used to reproduce it.

## D93 (2026-08-10): The timeline row is written inside `transition()`, and the org-wide
half of spec 6.3 stays on the audit log (E3.11)

- **Decision:** `reconciliation_event` is written by `revision_state.transition` rather than
  by its call sites. Spec 6.3 asks for "every transition" recorded, and that function is
  already the only writer of `config_revision.state` — so the timeline is complete BY
  CONSTRUCTION instead of by four call sites each remembering to log. The same argument put
  `published_at` there (D84). `test_no_transition_can_happen_without_a_timeline_row` walks
  the entire spec 6.2 table to hold it.
- **Decision:** spec 6.3's other half — "an Organization-wide and per-Deployment audit log
  renders the same events filtered by scope" — is **E0.8's `GET /audit`**, not a second
  surface over this table. E3.4, E3.5 and E3.7 have been writing `revision.publish`,
  `revision.report`, `revision.timeout` and `revision.drift` rows with the deployment in
  `scope` since they landed. Two org-wide logs would be two answers to one question, and the
  one nobody was looking at would rot.
- **The two tables are not redundant.** `audit_log` answers "who did what across this
  organization" and holds config edits and user administration too; `reconciliation_event`
  answers "what happened to this device" and is the only one guaranteed complete per
  transition.
- **`diff` and `detail` split by PROVENANCE.** `diff` comes from revision snapshots, which
  hold spec 5.4 markers rather than plaintext, so storing values there cannot leak a secret —
  and an operator seeing "before 48000, after 22050" is the entire point of spec 6.3's
  "before/after effective config diff". `detail` is whatever a DEVICE or the worker said, and
  device values are of unknown provenance, so it carries key NAMES only. Mixing them would
  either strip the diff of its usefulness or put untrusted values on a trusted screen.
- **The diff is recorded only on entry to `pending`**, the edge where what the platform is
  asking for changes. Repeating it on `applied`, `drifted` and `failed` would read as four
  separate config changes on the timeline.
- **The first revision for a device has no diff.** It is not a change from anything, and
  rendering the whole config as "added" buries the one key an operator edited.
- **UI:** entries carry `data-revision-state`, deliberately not `data-status`. A revision
  state (spec 6.2) and a device status (spec 9.3) are different vocabularies, and D40's guard
  forbids `[data-status]` on inventory routes until E3.12 has real status to put there —
  borrowing the attribute would defeat a guard that exists to stop plausible-looking
  placeholders.

## D92 (2026-08-10): The worker gets a REAL healthcheck, because `disable: true` is red in
CI and green locally (E3.7 defect, found in CI)

- **What went wrong:** CI failed the two container tests with
  `container eoe-verify-test-worker-1 has no healthcheck configured`. E3.7 gave the `worker`
  service `healthcheck: disable: true` — deliberately, with a good argument: the shared image
  probes the API's HTTP port, which the worker does not have, and "a green tick that proves
  nothing is worse than no tick". The local Compose (v5.3.1) accepts a disabled healthcheck
  under `compose up --wait`; the runner's rejects it. **Green here, red there**, on already
  pushed and tagged commits.
- **Decision:** the worker writes a liveness stamp and the compose healthcheck reads its age.
  `EOE_WORKER_HEARTBEAT_PATH`, default `/tmp/eoe-worker.heartbeat`, written every 5s by a
  loop that **only writes while both sweep tasks are alive**.
- **Why this keeps E3.7's argument rather than abandoning it:** the check is real. The
  failure a worker can actually suffer is a sweep task dying while the process stays up
  holding its broker connection — from outside indistinguishable from a healthy fleet, which
  is exactly why `_sweep_loop` swallows exceptions in the first place. A tick that only
  proved the process had not segfaulted would report that as healthy forever.
  `test_a_dead_sweep_lets_the_heartbeat_go_stale` is the guard.
- **Why a file and not a port:** the worker serves nothing. An HTTP port opened purely to
  answer a probe means a socket, a framework and a route added to a process whose entire job
  is to talk to Postgres and a broker.
- **Only the standalone process writes one.** Under `EOE_WORKER_IN_API` the API's own
  healthcheck already covers the process, and the suite runs the worker hundreds of times
  without wanting a file each — `test_no_heartbeat_file_is_written_unless_one_is_asked_for`.
- **Guarded against recurrence:** `test_no_compose_service_disables_its_healthcheck` bans
  `disable: true` outright, since that is precisely what CI rejects and the only part
  decidable from the compose file. This is the third defect this batch that was invisible to
  one side of the CI/local split (D87, D90, D92); the pattern is that the gate and CI do not
  run in identical environments, and each such difference is worth a guard rather than a
  memory.

## D91 (2026-08-10): A missed-wake EVENT flips liveness immediately, and the platform
still computes nothing (E3.9)

- **Decision:** `listener_missed_wake_window` sets `device_state.liveness_state = 'offline'`
  for the named MAC when the event arrives, rather than waiting for the `lst/{mac}/reported`
  publish that spec 6.5 says will follow. `expected_wake_at` is cleared with it: the promise
  is spent once it has been missed.
- **Why not wait for the report:** spec 6.5 has the Aggregator do both — raise the event AND
  report the Listener offline next time it publishes — and the event is the first news. A
  Listener that keeps reading `sleeping` until the next reported publish is a device the
  operator is being told is fine while its own Aggregator has already said it is not.
- **Why this is not the platform computing liveness,** which the phase document forbids in
  as many words: the Aggregator knows the wake time the Listener declared over the local
  link, applies `listener.wake_grace_seconds` itself, and raising the event IS the decision.
  The platform records an announcement. **Nothing up here reads a wake time or a grace
  period**, and `test_the_platform_never_computes_a_wake_window` is the guard: an
  `expected_wake_at` an hour in the past with no event behind it changes nothing at all.
  `wake_grace_seconds` remains a device setting that rides the config down.
- **Ordering:** the event and the reports come from the same Aggregator over the same
  ordered session, so the report that follows confirms rather than contradicts. Reports stay
  authoritative for liveness and keep E3.5's staleness rule; the event only supplies the
  immediate flip.
- **A missed-wake for a Listener that has never reported stores the event and no state
  row.** Inventing a `device_state` row from an event would put a device in the reported
  table that has never reported.
- **Verified by mutation:** removing the flip turns two tests red, including the acceptance.

## D90 (2026-08-10): The QA stack's compose project name is pinned in every document
(E3.8, found by the gate)

- **What went wrong:** gate 46 went red on the two container tests with
  `Bind for 0.0.0.0:15173 failed: port is already allocated`, and the holder was a full
  stack under the compose project **`deploy`** — not the `eoe-qa` one the walkthrough talks
  to. `qa-stack.ps1` passes `-p eoe-qa`, but the guide's POSIX §0 path and the README both
  omitted `-p`, so Compose fell back to naming the project after the directory.
- **Why it is worse than a cosmetic mismatch:** a POSIX reader who follows §0 gets a stack
  called `deploy`, and then every later command in the walkthrough — all of which pass
  `-p eoe-qa` — silently addresses a stack that does not exist, while the one they are
  actually running keeps holding the fixed host ports. The D44 pre-gate warning names
  `.\qa-stack.ps1 down`, which does not touch it either. The failure surfaces as an
  unexplained port collision with no documented command to clear it.
- **Decision:** `-p eoe-qa` is pinned in the guide's POSIX path and in the README's dev
  setup, with a note saying it is not optional; the pre-gate warning now gives the POSIX
  teardown alongside the PowerShell one and says the gate collides with ANY running stack,
  not only a `qa-stack.ps1` one.
- **Not fixable in the test harness**, unlike D87: `FIXED_PORTS` is a deliberate contract
  (the walkthrough tells you to open `localhost:15173`), so the container tests bind those
  exact ports by design and no amount of pinning inside `compose_env` lets a gate coexist
  with a running stack. The fix belongs in the documents that bring the stack up.

## D89 (2026-08-10): Per-task project-updates entries are batched to the end of E3
(owner instruction)

- **Decision:** E3.8 through E3.13 do not each get a dated `docs/project-updates.md` entry.
  One consolidated entry covering the batch lands when E3.13 closes the epic. Every other
  part of R0/R1 is unchanged: each task still ends in its own FULL green gate, its own
  commit, its own push and its own `gate-{N}` tag, and every deviation still gets a
  DECISIONS entry as it happens.
- **Why:** the owner asked for it on 2026-08-10, having watched the per-task entries grow
  into the largest artifact of each task.
- **Recorded rather than silently followed** because R1 says "dated entry after each gate
  PASSES", and a rule the project binds itself to is not one a session may quietly drop. The
  consolidated entry is the compromise: the batch is not allowed to land with no dated
  record at all, which is the thing R1 exists to prevent.
- **Consequence to watch:** a red gate mid-batch has nowhere to be recorded until the end.
  Where one happens, it goes in the commit message of the task that fixed it, the way
  gate 45's three red runs did.

## D88 (2026-08-10): LWT online state is its own table, and receipt order — not the
payload clock — decides it (E3.8)

- **Decision:** `aggregator_status` (migration `d3b1a7f45e92`), one row per Aggregator:
  `online`, `declared_at`, `changed_at`, `received_at`, a cascading FK to `aggregator.id`.
  E3.5's `DeviceState` docstring anticipated these as columns on `device_state`; they are
  not, and that docstring is amended.
- **Why not `device_state`:** three reasons, the third decisive. (1) `reported_at`,
  `checksum` and `config` are NOT NULL there, and a device publishes `online` before it has
  ever reported a config — a status-only row would need three of E3.5's columns made
  nullable, dissolving the invariant that a row there IS a report. (2) LWT is
  Aggregator-only: Listeners hold no MQTT session (spec 6.4/9.3), and E3.9 stores their
  liveness on the report where it arrives. (3) An `offline` LWT is published by the BROKER
  on the device's behalf. `device_state` is defined as "the last state the device sent", and
  a will is precisely the state the device did not send.
- **Decision (ordering), and the trap it avoids:** status carries NO staleness comparison.
  A device composes its will when it CONNECTS, so the broker holds those bytes — with that
  connect-time `at` — until the session dies; every `online` heartbeat published afterwards
  carries a later timestamp. Applying spec 7.4's rule here, correct as it is for reports,
  would reject the LWT as stale and **leave a dead device reading online forever**, which is
  the exact failure spec 9.3 makes MQTT authoritative in order to prevent. Receipt order is
  the truth: one broker, QoS 1, one ordered session per device, and a retained replay always
  carries the current value. `declared_at` is stored because the device said it and is read
  by nothing that decides anything. Pinned by
  `test_an_lwt_whose_timestamp_predates_the_last_heartbeat_still_wins`.
- **`changed_at` moves only on a real change.** The broker replays the retained status on
  every platform reconnect; rewriting it there would reset the whole fleet's "offline since"
  to the moment the platform restarted, telling an operator the outage began when their own
  service did.
- **No `unknown` third state.** A device that has never spoken has no row, which is a
  different question from one the platform has heard call itself offline.
- **Verified by mutation:** removing the SIGKILL from the acceptance test leaves the device
  online and turns it red, so the flip is genuinely the broker's will and not a side effect.

## D87 (2026-08-10): Opening broker connections may never kill its host, and the
container tests pin every compose variable (E3.7, found by the gate)

- **What went wrong:** the gate 45 run went red on `test_compose_stack` and `test_verify_tool`
  with `container eoe-gate-test-api-1 exited (3)` — uvicorn's code for a lifespan that raised.
  With `EOE_PUBLISH_ENABLED` on, the D86 lifespan awaited a bare `MqttClientManager.start()`,
  which reads the `deployment_service` rows ONCE. The API comes up beside Postgres in compose,
  won the race against the migrations that create that table, and died of
  `UndefinedTable` — taking every route that has nothing to do with publishing with it.
- **Decision (the defect):** the retry moves INTO the manager as
  `MqttClientManager.start_or_retry()`, returning True when the connections are open and False
  when a background retry is running; `stop()` cancels that retry. Both hosts of a manager now
  get it, and E3.7's `ReconciliationWorker._connect_with_retry` — which already had exactly
  this guard privately — is deleted in favour of it. A second copy in `main.py` was the
  alternative, and the reason there is one copy is that the worker having the guard while the
  API did not is precisely what shipped the bug.
- **Decision (why CI could not have caught it):** Docker Compose interpolates `${VAR}` from the
  process environment first and from `deploy/.env` second. `compose_env()` built a fresh env
  dict but left five variables unpinned, so the container tests read them from a developer's
  own scratch `.env` — which the E3 walkthrough §7 instructs you to fill with
  `EOE_PUBLISH_ENABLED=true` and 5-second sweeps. The gate therefore tested a *different stack*
  on a machine that had run the walkthrough than in CI, where no `.env` exists. Every
  interpolated variable is now pinned, guarded by
  `test_compose_env_pins_every_variable_the_compose_file_interpolates`, which caught three more
  on its first run — including `EOE_CORS_ORIGINS`, whose walkthrough value still names the
  pre-PHASE0-2-02 port.
- **Verified by mutation:** restoring the bare `start()` turns
  `test_the_api_starts_even_when_the_broker_rows_cannot_be_read` red.
- **The honest reading:** this is a defect in E3.7 as written, caught before its gate passed
  and before it was committed, which is what the gate is for. It also means the flag flip at
  E3.13 would have broken every `compose up` for anyone whose migrations had not already run.

## D86 (2026-08-10): The API holds its own publish-only broker connection (E3.7)

- **Decision:** `create_app` gained a lifespan. When `EOE_PUBLISH_ENABLED` is on it starts
  an `MqttClientManager` with NO subscriptions registered and parks it on
  `app.state.mqtt`; the publish route and (at E3.13) E2's apply publish through it. With
  the flag off it starts nothing and `app.state.mqtt` is None, which is what every existing
  test sees — a `TestClient` used without its context manager never runs the lifespan at
  all.
- **Why:** publishing is an HTTP action and the worker is a different process (D59). The
  alternatives were worse: routing publishes through the worker would need a request/reply
  bus this phase does not have, and a shared connection across a process boundary is not a
  thing that exists. Two managers on one broker are fine — each carries its own instance
  suffix in its client id (D64), which is the same property that lets two API replicas
  coexist.
- **Consequence:** the API subscribes to nothing, so it can never consume a message the
  worker is also consuming. A single-process deployment (`EOE_WORKER_IN_API`) runs both
  managers in one process, and they still do not overlap.

## D85 (2026-08-10): The drift sweep compares BOTH directions, and only one of them is a
transition (E3.7, owner-approved)

- **Decision:** each pass over the `applied` revisions asks two questions. (1) Does the
  device's stored `device_state` match the revision it applied? A mismatch is
  `applied -> drifted(report_diverged)`, the spec 6.2 edge. (2) Does the platform's
  RECOMPUTED effective config still match that revision (through E2's merge engine and
  `snapshot_from_raw`)? A mismatch is reported as `desired_changed` and moves nothing.
- **Why the second one is not a transition:** spec 6.2 has no state for "desired moved on",
  and it would not be true of the device if it had one — the device is doing exactly what
  it was told. Creating the revision that closes the gap is E2's apply, which is out of
  scope for this phase (phase-3 §3), so the worker would have nowhere to go with it. It is
  surfaced because an operator seeing "applied" on a device whose config has been edited
  since is being told something misleading by omission.
- **Why direction (1) needs a sweep at all**, given E3.5 already drives that edge on
  report: a device that quietly reverts and then reports with no `applied_revision_id`, or
  names a pruned revision, moves nothing on arrival — E3.5 stores the state and
  deliberately takes no edge (D79's neighbour). The sweep is the only thing that reads
  those rows, which is exactly spec 6.4 item 5's "even without a device-initiated report".
- **One composition rule, not two:** `plan.revision_snapshot` was split into
  `snapshot_from_raw(target_type, raw)` so the sweep builds its comparison the way E2 built
  the revision. A second copy would report every Listener carrying a write-restricted
  service key as drifted — the drift detector drifting, the one defect a drift detector
  cannot have. Pinned by `test_the_detector_uses_e2s_own_snapshot_composition`.

## D84 (2026-08-10): `config_revision.published_at`, and the timeout window is measured
from it (E3.7)

- **Decision:** a nullable `published_at` column, written by
  `revision_state.transition` on EVERY edge into `pending` and by nothing else. The spec
  6.4 item 4 window is measured from it. A `pending` row with no `published_at` is reported
  and left alone, never timed out.
- **Why not `created_at`:** E2 writes that when an operator saved a draft. Spec 6.2 reaches
  `pending` three ways, and two of them are re-entries — an operator retrying a `failed`
  revision, or re-publishing over `drifted`. Measured from `created_at`, a revision retried
  an hour after it was drafted fails again on the next sweep without the device having been
  given a moment to answer, and the timeline then says `failed(timeout)`, which under D70
  means the device stayed silent.
- **Why in the state machine rather than in the publisher:** every path into `pending` goes
  through `transition()`. Stamping it at the call site would make the window a property of
  which caller moved the revision.
- **Why a column and not worker memory:** the phase acceptance is that a restarted worker
  loses nothing (spec 14.3). This is the only piece of the loop that was not already
  durable. Verified by mutation: removing the stamp turns both acceptance tests red.
- **Extends an E2-owned table** (`config_revision`, D55). Flagged rather than assumed: E2
  owns the row up to `draft` and E3 owns every state after it, so lifecycle columns are
  E3's to add. `docs/INTERFACES.md` records it under E3.

## D83 (2026-08-10): `service_unavailable` joins the D8 error vocabulary (E3.7)

- **Decision:** a seventh envelope code, at HTTP 503, raised when a dependency the platform
  needs is down and the request itself was fine. Its first use is a broker outage during
  `POST /revisions/{id}/publish`.
- **Why:** D8 fixed the vocabulary as stable and EXTENSIBLE for exactly this. The existing
  codes both lie here: a 4xx blames the caller for a broker that is down, and
  `internal_error` sends an operator looking for a platform defect. The publish rides
  inside the database transaction (D74), so the revision is untouched and the request is
  honestly retryable — which is what 503 means and what neither alternative says.
- **Consequence:** `test_api_skeleton.py::D8_VOCABULARY` gained the code. That is an
  extension of an E0 guard, not a weakening: the assertion is still exact equality.

## D82 (2026-08-10): `POST /revisions/{id}/publish` — drift repair is an operator action
(E3.7, owner-approved)

- **Decision:** a new route on the E2.6 revisions router, `manage_config` within the
  revision's deployment, CSRF-protected, calling the same `publish_revision` E3.13 wires
  E2's apply to. Scoping is two-step: a revision the caller cannot SEE answers 404 (the D35
  existence-oracle rule), one they can see but may not publish answers 403 naming the
  permission. Refusals map as: publication disabled → 409, stale or superseded → 409, device
  gone from inventory → 409, broker down → 503 (D83), revision vanished → 404.
- **Why it exists in this phase:** the phase forbids auto-republish (`auto_reconcile` is
  inert, D81) and makes re-publish "an operator action" — with no route, drift could not be
  repaired through the platform at all, and the phase acceptance's own re-publish step
  would exist only inside a test.
- **Not temporary, and not superseded by E3.13:** E3.13 wires BULK apply to publication.
  This is the single-revision action, and the two share one code path with one set of
  refusals.

## D81 (2026-08-10): The reconciliation policy lives on the `deployment` row, and
`auto_reconcile` is stored INERT (E3.7, owner-approved)

- **Decision:** two columns on `deployment` — `pending_timeout_seconds` (default 300, the
  phase-3 fixed choice, CHECK > 0) and `auto_reconcile` (default false). Not a
  `deployment_policy` table: one row per deployment already exists, the sweep reads it in
  the same query it scans revisions with, and E5/E7 can move it later behind the accessor.
- **Why on the deployment and not in the E2 settings catalog:** it is a platform setting,
  not a device setting (phase-3 fixed choice). A catalog key would be merged into effective
  config and published to devices, which would put the platform's own scheduling on a
  device's desired topic.
- **`auto_reconcile` is stored and inert.** Spec 6.2 names an "auto-reconcile policy" as
  the second driver of `drifted -> pending` and spec 17 item 3 has not decided what that
  policy is. The worker READS the flag only to log that it read it, and counts it in the
  sweep report; nothing in the codebase turns it into an action.
  `test_auto_reconcile_is_stored_and_does_nothing` is the guard, and the phase that
  implements the policy is the phase that may delete it.
- **A revision whose deployment row is gone falls back to the 300s default** rather than
  sitting `pending` forever: `config_revision.deployment_id` is un-FK'd by design (D33).

## D80 (2026-08-10): The worker's shape — two sweeps, one transaction per revision, and no
state of its own (E3.7)

- **Decision:** `app/controlplane/runner.py` holds `ReconciliationWorker` plus two pure
  sweep functions (`pending_timeout_sweep`, `drift_sweep`) that take a session factory and
  return a report. The worker owns an `MqttClientManager` with E3.5's consumer registered
  and schedules the two sweeps on independent cadences (`EOE_TIMEOUT_SWEEP_SECONDS` 30,
  `EOE_DRIFT_SWEEP_SECONDS` 300). Two entrypoints, one module (D59):
  `python -m app.controlplane.runner` for the compose `worker` service, and the API
  lifespan under `EOE_WORKER_IN_API`.
- **The sweeps are functions, not methods,** so the suite drives them with no event loop,
  no broker and no worker — a red test there means the comparison is wrong rather than a
  container being slow.
- **One transaction per revision.** A sweep that committed once at the end would hold row
  locks across the whole scan; one that failed whole on a single bad row would leave the
  rest of the fleet unreconciled.
- **The scan is lockless and the transition is not.** Each candidate is re-read under
  `load_for_transition` and re-checked, so a device's ack that lands mid-sweep wins over the
  clock rather than losing to whichever write happened to be second
  (`test_an_ack_that_lands_first_wins_the_race`).
- **A failing sweep is logged and retried, never fatal.** A worker whose sweep task died
  would keep its broker connection and silently stop timing anything out, which from the
  outside is indistinguishable from a healthy fleet.
- **`broker.MessageHandler` widened to `Awaitable[object]`**: E3.5's `handle` returns a
  `ReportOutcome` the worker counts, and the dispatcher ignores return values anyway. Every
  existing handler still satisfies it.

## D79 (2026-08-10): A device's identity comes from the TOPIC, and reports that fail an
identity check are not stored either (E3.5)

- **Decision:** the reported consumer takes `{agg}` and `{mac}` from the topic a message
  arrived on and never from a payload field. `contracts.mqtt.parse_topic` is the one place
  that takes a topic apart, validating identifiers on the way IN with the same functions
  that validate them on the way out.
- **Why the topic is trustworthy and a payload field is not:** spec 7.1 cuts each device's
  broker ACL to its own subtree, so the segments of a topic a message arrived on were
  authenticated by the broker before the platform saw them. A payload field is a
  self-declaration; trusting it would let any device with a valid credential report on
  behalf of any other, and the E1.5 MAC-conflict machinery would never fire, because the
  conflicting device would simply claim to be the right one. None of the spec 7.3 inbound
  models carries an identity field today and none should grow one.
- **A quarantined or misrouted report writes NO `device_state` row and moves NO revision.**
  Spec 4.3 item 2 stops the platform overwriting *inventory*; this goes one step further,
  because a report the platform does not believe must not become the device's reported
  configuration either. Storing it would launder a rejected claim into the record E3.7's
  drift sweep reads. The acceptance test asserts all three: the Listener row is unchanged
  field for field, there is no `device_state` row, and the revision is still `pending`.
- **Two further refusals, both warnings rather than quarantines.** A known device reporting
  on a deployment it does not live in (checked against the CONNECTION's deployment, which
  the platform dialled and therefore knows) means a credential valid for a namespace the
  device does not belong to — a broker problem, not an inventory one. A report naming a
  revision that belongs to another device is the same class. Neither is evidence about a
  device's identity, so neither belongs in `quarantined_report`.

## D78 (2026-08-10): Reported-state persistence — `device_state` now, extended by E3.8 and
E3.9 (E3.5, owner-approved)

- **Decision:** E3.5 creates `device_state`, one row per device holding spec 6.1's "last
  state the device sent", and `device_event` for the spec 7.3 event stream. The phase
  document's E3.5 text names only "persist device events"; the owner approved the state
  table at plan approval for the reason below.
- **Why it is not optional:** spec 7.4's ordering rule needs a per-device memory. Deciding
  staleness from revision timestamps alone — which is literally what spec 7.4 says — misses
  the case that actually breaks things: a delayed report for the SAME revision carrying
  diverging config would drive a healthy `applied` device to `drifted` on ten-second-old
  news. `reported_at` compared against the stored row is what refuses it. E3.7's periodic
  drift re-compare needs the same row, and spec 6.1 asks for it by name.
- **Strictly older is stale; equal is not.** A byte-identical replay shares its
  `reported_at`, and letting it run the full comparison is what makes idempotency a property
  of `applied_revision_id` plus checksum, as spec 7.4 words it, rather than of a timestamp
  shortcut that would hide a broken comparison behind an early return. Test-pinned in both
  directions: flipping `<` to `<=` turns the replay test red.
- **`ReportOutcome.STALE` therefore means late delivery and nothing else** — the
  one-meaning-per-value discipline D70 applied to `Trigger.TIMEOUT`. A report naming a
  `superseded` revision is not stale delivery: it is current news that the device has not
  caught up, so it is STORED and moves nothing.
- **E3.8 and E3.9 extend this table** (the `deployment_service` pattern, D62's neighbour):
  E3.8 adds the LWT-driven online state spec 9.3 makes authoritative, E3.9 the spec 6.5
  liveness block. E3.5 stores neither, deliberately — a column here that is a
  half-implementation of a task that has not run is worse than no column.
- **Identifiers follow `config_revision` exactly** (`entity_type` + `entity_id`, aggregators
  by platform UUID and listeners by MAC), so a device's revisions and its reported state
  join without crossing between spec 4.2's three identifiers a second time (D75).
- **It is current state, not evidence, so it is deleted with its device.**
  `delete_device_state_for` is wired into the E1 aggregator and listener DELETE endpoints,
  the `delete_overrides_for` precedent (D51). A Listener re-added under a MAC that once
  belonged to another physical device would otherwise inherit its predecessor's reported
  config and read as reconciled before it had said a word. `device_event` rows are evidence
  and deliberately survive (D33).
- **`E0_TABLES` in `test_e0_readiness.py` gains both names**, which is that guard's
  documented extension mechanism rather than a weakening of it: the schema-drift check
  exists so a neighbouring phase's table cannot appear early unnoticed, and this is the
  phase that owns these two.

## D77 (2026-08-10): Device events dedupe on (emitter, instant, code) (E3.5, owner-approved)

- **Decision:** `device_event` carries a unique index on `(deployment_id, aggregator_uuid,
  listener_mac, at, code)` and the consumer checks before inserting, so a QoS 1 redelivery
  is a no-op rather than a second row.
- **Why not append-only**, which is the `quarantined_report` precedent: a quarantine row
  answers "how many conflicting reports arrived", where every delivery genuinely is evidence.
  A device event answers "what happened on the device", and a duplicated row is a lie about
  how often it happened — one an operator reads straight off the E3.11 timeline. Events
  carry no device-supplied id to dedupe on, so identity has to be structural; two distinct
  events with one code from one device in the same instant are indistinguishable on the wire
  anyway.
- **`NULLS NOT DISTINCT` is load-bearing.** Without it Postgres treats every Aggregator-level
  event (`listener_mac` NULL) as unique, and the index would dedupe only Listener events —
  so exactly the lifecycle events an Aggregator emits about itself would double. Requires
  Postgres 15+; the stack is on 16. A test covers the NULL case specifically.
- **Dedupe must not swallow a recurring fault:** the same code at a different instant is a
  different event, also test-pinned. A stream gap every minute is a minute-by-minute story.
- **The unique index backstops the check against races**, exactly as `inventory_alert`'s
  partial index backstops E1.5's alert dedupe.

## D76 (2026-08-10): An unregistered Listener's report is quarantined, with no alert (E3.5,
owner-approved)

- **The gap:** E1.5 returns `UNKNOWN_MAC` (known reporter, MAC in no inventory row) with
  zero side effects and says "E3 decides what an unregistered device means per channel"
  (D37). The reported channel is the first channel to have to decide.
- **Decision:** quarantine the report with reason `unknown_mac`, open no alert, write no
  inventory row and no `device_state` row.
- **Why quarantine:** a Listener wired up before anyone entered it in inventory is a real
  and ordinary situation, and dropping the report with a log line leaves an operator no
  trace to find it by. The quarantine table is already the place a report the platform will
  not act on goes, and the row carries the MAC, the reporting Aggregator and the payload —
  enough to adopt the device.
- **Why no alert:** spec 4.3 item 2's `duplicate_identity` is for conflicts, and nothing
  here disagrees with anything; spec 4.3 item 3's `provisioning_required` is explicitly about
  `aggregator_uuid` membership on the metrics, analysis and object ingest paths. Reusing
  either would widen an E1-owned vocabulary to mean something it does not.
- **Consequence for E1.5:** `_quarantine` became the public `quarantine_report(db, report,
  reason)` so the row shape stays in one place rather than being reassembled by each channel
  that needs it. Behaviour, signature and outcomes are unchanged; `quarantined_report.reason`
  gains a third value, noted on the column.

## D75 (2026-08-10): An Aggregator revision's `target_id` is the PLATFORM UUID; the topic
segment is the `aggregator_uuid` (E3.4)

- **The fact:** E2's apply writes `str(aggregator.id)` — the row primary key — into
  `config_revision.target_id`. The spec 7.2 `{agg}` topic segment and the spec 7.3
  `target.id` field are the `aggregator_uuid`. Spec 4.2 keeps an Aggregator's three
  identifiers distinct and E1 never conflates them (INTERFACES, entity schema); this is the
  one place in the codebase that has to cross between two of them, so `resolve_desired_route`
  does the lookup and `DesiredRoute.device_id` carries the result.
- **Why it matters:** using `target_id` as the topic segment builds
  `eoe/redwood-coast/agg/<a uuid>/desired` — a well-formed topic that no device subscribes to
  and no broker ACL grants. It fails silently: the publish succeeds, the revision goes
  `pending`, and the device times out to `failed` 300 seconds later looking like a hardware
  fault. Nothing upstream catches it, because every identifier involved is a valid string.
- **And the payload's `target.id` is the `aggregator_uuid` too.** An Aggregator receiving the
  platform's private row key could not recognize itself in it. Listeners have one identifier,
  the MAC, so the distinction does not arise there.
- **How it was found, and the test lesson.** E3.4's first implementation looked up by
  `aggregator_uuid` and its suite built fixtures the same way, so the tests agreed with the bug
  and passed. The manual verification pass caught it on the first real revision, because E2's
  own apply response shows the real `target_id`. The fixture now derives the id through
  `platform_uuid_of()` from live inventory, and
  `test_an_aggregator_target_id_that_is_not_a_platform_uuid_is_refused` pins the refusal —
  a fixture that invents its own id shape can only prove the implementation agrees with the
  fixture.

## D74 (2026-08-10): The publish happens INSIDE the database transaction (E3.4)

- **Decision:** `publish_revision` opens its own session, stages the transition, the supersede
  sweep and the audit row, publishes the retained message, and commits only if the publish
  returned. A `BrokerUnavailable` rolls the whole thing back, so the revision stays in `draft`
  with nothing published — which is what `broker.py` already promised its callers.
- **Why not commit first:** a committed `pending` revision that no device was ever told about
  resolves 300 seconds later as a spec 6.2 `failed(timeout)`. Under D70 that message means
  "the device never answered", so the platform would be blaming a device for its own broker
  outage, and the operator would go looking at the link, the power and the firmware.
- **The residual window is one-sided and smaller:** publish succeeds, commit fails. That leaves
  a retained message with the revision still `draft` — recoverable by republishing, and it
  never produces a false accusation against a device.
- **The row lock is deliberately held across the publish.** `load_for_transition` takes
  `FOR UPDATE`, so two operators publishing the same revision at once serialize: the second
  re-reads the state the first committed and takes the idempotent path (D72) instead of
  transitioning twice. The cost is a database transaction held open across one QoS 1 round
  trip, which is bounded by the client's own timeout.
- **`publish_revision` owns its transaction rather than joining the caller's**, because a
  revision can only be published once it is committed. E3.13's apply therefore commits its
  overrides, revisions and audit rows first, then calls this.

## D73 (2026-08-10): Only the newest revision for a device may be published (E3.4)

- **Decision:** `publish_revision` refuses a revision that is not the latest for its
  `(target_type, target_id)`, ordered by `(created_at, id)`, and refuses a `superseded` one
  outright as spec 6.2's only terminal state.
- **Why:** this is the other half of the pair D69 records. `supersede_open_revisions` closes
  every other open revision unconditionally, with no timestamp comparison; publishing an older
  revision would therefore supersede a NEWER draft an operator was still editing. Removing
  either rule alone is a data-loss bug. Both docstrings say so, and
  `test_an_older_revision_is_refused_so_a_newer_draft_survives` pins it.
- **`(created_at, id)`, not `created_at` alone.** The column defaults to `now()`, which
  Postgres holds constant for a whole transaction, so revisions written together tie exactly.
  The same total order `open_revisions_for_target` sorts by, so "newest" and "oldest first"
  cannot disagree about which row is which.
- **The check ignores state on purpose.** A newer revision that is itself already superseded
  still blocks an older one: the rule is "publish the latest", which is simple enough for an
  operator to hold in their head, rather than a state-dependent search for the best candidate.

## D72 (2026-08-10): Re-publishing a `pending` or `applied` revision re-sends the bytes and
moves no state (E3.4)

- **Decision:** the E3.4 acceptance criterion "republish of the same revision is idempotent" is
  implemented as: send the byte-identical retained payload again, perform NO transition, write
  NO second audit row. `PublishOutcome.transitioned` reports which path ran.
- **Why re-send rather than return early:** a broker that lost its retained store (restarted
  without persistence, reprovisioned) leaves devices that reconnect with no desired config at
  all. Re-publishing is the operator's repair for exactly that, and it is safe because the
  payload is derived entirely from the immutable revision row — a device that already holds it
  computes a matching checksum and does nothing.
- **Why no transition:** `pending -> pending` is not a spec 6.2 edge and the state machine
  refuses self-transitions by design ("callers that mean 'already there' check the state
  first"), so this module checks first. Moving `applied` back to `pending` for a re-send would
  be worse than illegal: it would make a healthy, reconciled device read as unreconciled when
  nothing about it changed.
- **Why no audit row:** the audit trail answers "who changed this revision's state". A re-send
  changed nothing, and a row per retry would bury the transitions that matter under repair
  noise. The publish is still logged at INFO.

## D71 (2026-08-10): The desired topic is resolved from LIVE inventory, and the flag is
enforced inside `publish_revision` (E3.4)

- **Topic addressing:** the deployment slug and aggregator UUID come from the device's current
  inventory rows, never from `config_revision.deployment_id`. That column is historical
  evidence (the D33 un-FK'd precedent) recording where the device lived when the revision was
  cut; a device that has since moved deployments must publish to its current home's broker and
  subtree or not at all. Listener revisions resolve their deployment through their Aggregator's
  pod for the same reason — the spec 7.2 Listener subtopic hangs off the Aggregator's subtree,
  so it must name the deployment whose ACL grants that subtree.
- **A revision whose device is gone raises `UnknownPublishTarget`,** which is expected rather
  than exceptional: revision history outlives the devices it describes by design, and a
  revision for a decommissioned Aggregator is evidence with no topic to go to.
- **`EOE_PUBLISH_ENABLED` is enforced inside `publish_revision`,** as a REQUIRED keyword-only
  `publish_enabled` argument, not as a check each call site remembers. The value still comes
  from `Settings.publish_enabled` (D61) because settings do not belong in the control-plane
  core, but the refusal lives in one place, so no future caller can reach a device by
  forgetting the flag. Required rather than defaulted: a default would decide the safety
  question for callers who never thought about it.
- **The spec 6.2 trigger is chosen from the revision's current state,** not fixed at `publish`:
  `draft` publishes, `drifted` republishes, `failed` retries. Three edges reach `pending` and
  they are three different events on E3.11's timeline. A hardcoded trigger would be rejected by
  the state machine rather than silently mislabelled, which is what validating triples buys.

## D70 (2026-08-10): `failed(timeout)` means silence and nothing else; a contradictory ack
fails fast (E3.6, implemented at E3.5)

- **Decision:** a reported state that names a pending revision but carries config that is not
  that revision's is a DEFINITE negative answer, and fails the revision on the first report —
  `pending -> failed` under `report_error`, with a detail naming the differing KEY NAMES (not
  values: snapshots hold secret markers, rule R2). It never waits for the window to elapse.
  `Trigger.TIMEOUT` therefore attaches to exactly one transition and carries exactly one
  meaning: no valid report arrived at all. A suite test pins that one-to-one.
- **And the ambiguity is removed rather than adjudicated.** The report carries `config` AND
  `checksum`, so two different failures were being collapsed into one. They are now separate:
  1. **Internally inconsistent** — `checksum` != `config_checksum(config)`. The device
     contradicts *itself*; the message is malformed, not a reconciliation outcome. Rejected at
     the boundary alongside schema violations, with a message pointing at the firmware's
     checksum implementation, and **no state transition** — an unparseable report is not
     evidence about whether the config was applied, so the revision is left for the timeout,
     whose message is then still true.
  2. **Internally consistent, disagrees with the revision** — the device coherently reports
     config that is not the revision's. Nothing to wait for. Fails immediately, per above.
- **Why not the conservative "stay pending and let the timeout decide":** it waits 300 seconds
  to report a *timeout* for a device that answered in two seconds, which is an inaccurate error
  message for a condition the platform already knew for certain. It also makes
  `failed(timeout)` cover two stories that call for opposite operator responses — "the device
  never answered" (check the link, the broker, the power) and "the device answered wrong"
  (check the config and the firmware).
- **Consequence, and it is deliberate:** `applied_revision_id` is not authoritative on its own.
  The platform recomputes the checksum from the reported config rather than trusting a naked
  checksum field, which is the only thing that makes D52/D55's "device-echoed checksums match
  by construction" a property rather than a hope. A firmware that cannot reproduce the D52
  recipe is caught by case 1, precisely, instead of looking like a config disagreement.

## D69 (2026-08-10): Spec 6.2's diagram beats its table on the superseded edge (E3.6)

- **The contradiction:** spec 6.2's transition TABLE lists only `pending -> superseded` and
  `applied -> superseded`. Its DIAGRAM, four lines below, draws
  `(any non-terminal) --new revision--> superseded`, which also reaches `draft`, `drifted` and
  `failed`. Both are section 6.2; they cannot both be complete.
- **Decision (owner-approved at plan approval):** implement the union — the table's nine rows
  plus the diagram's three. `superseded` is the only terminal state.
- **Why the diagram:** under the table alone a revision that failed can never be closed out.
  The operator fixes the config and publishes a new revision; the old row sits at `failed`
  forever beside an `applied` one, and nothing in the state column says which is live. The same
  goes for a superseded-in-fact `draft` and for a `drifted` revision abandoned in favour of a
  new one. The table is best read as listing the interesting transitions, not as an exhaustive
  enumeration — which is exactly what the diagram's parenthetical says.
- **How the suite keeps both honest:** `test_revision_state.py` transcribes the table verbatim
  as `SPEC_6_2_TABLE` (trigger text included) and the diagram's edge separately as
  `SPEC_6_2_DIAGRAM_EXTRA`, so each spec statement is named and attributable rather than merged
  into one undifferentiated list. Feed this back into the next spec revision by adding the three
  rows to the table.
- **A transition is a TRIPLE, not a pair.** Legality depends on the trigger: `pending -> failed`
  is legal as an apply error or a timeout and illegal as "operator retries", which is
  `failed -> pending` read backwards. Validating `(source, target)` alone would accept that.
- **The paired rule that makes the sweep safe.** `supersede_open_revisions` closes every other
  open revision for the device unconditionally, with no timestamp comparison. That is only safe
  because E3.4 refuses to publish a revision that is not the newest for its device; without that
  guard the sweep would quietly discard a newer draft an operator was still working on. The two
  rules are a pair — removing either alone is a data-loss bug, and both say so in their
  docstrings.

## D68 (2026-08-10): `PayloadError` is safe to log; Pydantic's rendering is not (E3.3)

- **Decision:** `contracts.mqtt.decode` builds its message from
  `ValidationError.errors(include_url=False, include_input=False, include_context=False)` —
  the model name and which fields failed, never the values.
- **Why:** `str(ValidationError)` echoes the offending input back, and for a `missing` error
  the "input" is the WHOLE body. A reported-state payload's `config` carries secret markers
  and an event's `detail` is device-supplied text of unknown provenance, and E3.5 will log
  every decode failure it hits. Verified, not assumed: the test feeds a body whose `config`
  holds a `secret:` marker and omits `checksum`, and asserts the marker does not appear.
- **Consequence:** a later edit that swaps the message back for Pydantic's nicer one puts
  secret markers in the log. The comment on `decode` says so; keep it.

## D67 (2026-08-10): The spec 7.3 payload models — strictness by direction, and the
vocabularies the spec left open (E3.3)

- **Direction decides strictness.** Models the platform PUBLISHES (`DesiredConfig`,
  `Command`) set `extra="forbid"`: an unexpected key there is a bug on this side about to
  reach every device in a deployment. Models it RECEIVES (`ReportedAggregatorState`,
  `ReportedListenerState`, `StatusMessage`, `DeviceEvent`) set `extra="ignore"`: firmware
  that adds a field must not be able to make the platform stop reading its reports.
- **`schema_version` is top-level only, and absent means 1.** There has never been another
  version, so a device that omits it can only have meant this one; a payload claiming any
  other version is rejected rather than guessed at. Nested blocks (`target`, `health`,
  `liveness`) carry none, matching every spec 7.3 example.
- **Timestamps are timezone-aware only**, normalized to UTC on the way in and serialized as
  `...Z` on the way out. A naive instant cannot be ordered against another device's report,
  and spec 7.4 drops stale reports by comparing timestamps — guessing UTC would make that
  silently wrong instead of loudly broken. `Z` rather than `+00:00` because that is what
  every spec example prints and firmware may well compare the strings.
- **`encode()` omits absent optionals rather than sending null**, which is what spec 7.3
  means by `expected_wake_at` being "present only while sleeping". It does NOT reach inside
  `config`: a null there is data, and stripping it would change the D52 checksum.
- **`expected_wake_at` is present exactly while sleeping**, enforced in both directions. The
  platform never recomputes a wake schedule (spec 6.5), so a `sleeping` report without one
  leaves nothing to tell healthy sleep from silence, and one left on a `streaming` report is
  a stale promise E3.9 might act on.
- **Vocabularies the spec leaves open**, chosen here and open to revision before firmware
  ships: event `level` is `debug|info|warn|error` (the spec shows `warn`); event `code` is an
  OPEN vocabulary — firmware will invent codes — but constrained to identifier shape
  (`^[a-z][a-z0-9_]{0,63}$`) because codes end up in queries, alert rules and UI copy;
  `health.coarse` is deliberately FREE TEXT, since inventing a vocabulary firmware has not
  agreed to would reject real reports for a field the platform does not even chart (spec
  10.1); `detail` is capped at 2000 characters so firmware authors read the budget off the
  contract rather than discovering it from a truncated row.
- **`applied_revision_id` is optional** on both reported states: a device that has applied
  nothing yet still reports its state.
- **`command_id` defaults to a fresh UUID**, so two submissions of one logical command carry
  distinct ids structurally rather than by a caller's discipline — that is exactly what lets
  a device deduplicate its own retries without swallowing an operator's second attempt.
- **The D52 checksum recipe is NOT imported here.** This module is published to firmware
  authors who implement the recipe rather than call it, so the contract states the field's
  SHAPE (`^sha256:[0-9a-f]{64}$`) and `app.config.canonical` keeps the recipe. The suite
  bridges them: a snapshot round-tripped through `encode`/`decode` must produce identical
  canonical bytes, which is the property that makes device-echoed checksums match.

## D66 (2026-08-10): Async tests ride anyio's pytest plugin, not pytest-asyncio (E3.2)

- **Decision:** the `anyio_backend` fixture in `backend/tests/conftest.py` pins the single
  backend `"asyncio"`, and async tests carry `pytest.mark.anyio`. No new dev dependency.
- **Why:** anyio is already installed — Starlette depends on it — and its plugin does
  everything pytest-asyncio would here. Pinning one backend also keeps one test per test:
  anyio parametrizes over trio by default, which would double the async suite for a runtime
  the app never uses, and every one of those duplicates counts against the gate's clock.
- **The one new RUNTIME dependency at E3.2 is `aiomqtt`** (plus its `paho-mqtt`), which the
  phase document fixes as the client choice. Nothing else was added.

## D65 (2026-08-10): A pinned broker CA REPLACES the public trust store (E3.2)

- **Decision:** when a `deployment_service` row carries `ca_cert_pem`, `tls_context()` builds
  a context trusting that CA and nothing else. Only a row with no stored PEM falls back to
  the system trust store (the E5 path, for a broker with a publicly-issued certificate).
- **Why:** spec 7.1 identifies a deployment's broker by its own CA, and `ssl` offers both
  shapes — `create_default_context()` then `load_verify_locations` ADDS an anchor, which
  reads like hardening and is the opposite. With the public roots still loaded, any
  certificate any public CA would issue for the broker's hostname also verifies, so the
  stored PEM stops being a constraint and becomes decoration.
- **What stays true in both branches:** `check_hostname` on, `CERT_REQUIRED`, minimum TLS
  1.2. aiomqtt's `tls_insecure` is not used anywhere and should never be.
- **Consequence:** re-running `app.devbroker` rotates the CA, so a manager holding older
  coordinates fails to verify until it reloads them. That is the correct failure — it is a
  different broker identity — and the reload happens on the manager restart E3.7 owns.

## D64 (2026-08-10): The client manager's connection model (E3.2)

Four rulings the phase document leaves open, all chosen so that **a broker outage is not an
event message-handling code ever sees** — the property E3.2's acceptance criterion states.

- **Subscriptions are registered before `start()` and are fixed after it.** A registration
  accepted mid-flight would reach a connection that happens to be down only after it next
  reconnected, so some deployments would deliver to the new handler and others would not —
  a bug that surfaces as missing messages days later. Registering late raises.
- **Clean sessions; every connect resubscribes.** The platform does not ask the broker to
  remember its session: several API replicas may hold the same deployment, and a shared
  persistent session id would have them evict each other. Delivery guarantees come from QoS 1
  and the retained desired topics (spec 6.4), which is where the spec puts them.
- **A handler that raises is logged, and the loop keeps reading.** One device's malformed
  payload must not cost a whole deployment its control plane. This is why `InboundMessage`
  carries RAW bytes: a payload the platform cannot parse is still a payload E3.5 has to see
  and decide about, so parsing does not belong in the transport.
- **An unreadable broker secret skips that deployment, with a warning naming the secret and
  never its value.** The alternative — raising out of the loader — lets one badly provisioned
  deployment deafen every other one at startup.
- **Also settled:** coordinates load once at `start()` (adding a broker row takes the manager
  restart E3.7 owns); `publish()` lives here but stays a bare primitive, because WHICH topic
  and WHICH retain flag are E3.4's and E3.10's decisions; publishing with no live connection
  raises `BrokerUnavailable` rather than returning quietly, so E3.4 can never move a revision
  to `pending` on a publish that did not happen.

## D63 (2026-08-10): Dev host ports move, container ports do not (owner-directed)

- **Decision:** the compose stack publishes 18000/15173/15432/16379/18883 on the host; every
  container keeps listening on 8000/5173/5432/6379/8883 internally.
- **Why split it that way:** the collision is with the HOST's port space only. Moving the
  container side too would touch both Dockerfiles, the uvicorn and vite arguments, the
  Mosquitto listener and every in-network URL, for no benefit — nothing inside a compose
  network collides with anything. Keeping the standard ports internally also keeps the
  images honest as artifacts: the API image still serves 8000 wherever it is run.
- **Consequence for `vite.config.ts`:** the host-run dev server moves to 15173 as well, so
  the guides can name one address for both paths. The container is unaffected because the
  Dockerfile's CMD passes `--port 5173` explicitly.
- **Why it mattered enough to change a phase-0 fixed choice:** rule R0 forbids skipping, so
  a port a developer's other services already hold is not an inconvenience — it is a gate
  that cannot go green. Recorded as project-changes #21, addendum PHASE0-2-02.

## D62 (2026-08-10): The topic builders land with E3.1, not E3.3

- **Decision:** `app/contracts/mqtt.py` is created by E3.1 carrying the spec 7.2 topic
  builders; E3.3 adds the spec 7.3 payload models to the same module and completes its
  suite. The phase document assigns the whole module to E3.3.
- **Why:** E3.1's broker ACL grants ARE topic strings. Building them from literals for two
  tasks and refactoring at E3.3 would mean the namespace existed in two places, which is
  precisely the drift the "single contracts module" fixed choice exists to prevent. Moving
  a deliverable earlier inside one epic and one batch is a sequencing call the handbook
  (section 2) allows; nothing outside E3 is touched.
- **Consequence:** `backend/tests/test_mqtt_contracts.py` exists from gate 39 and E3.3
  extends it rather than creating it. Recorded in project-changes #20.

## D61 (2026-08-10): `EOE_PUBLISH_ENABLED` default flips on at E3.13 (owner-approved)

- **Decision:** E3.13 flips `Settings.publish_enabled` to default `True`, as task E3.13
  specifies, rather than deferring the flip to a separate readiness flight.
- **Why:** E2 is merged, so the condition the phase document attaches to the flip is met.
  The CI end-to-end test is the safety net, the flag remains settable per environment, and
  publication only reaches a broker a deployment actually has a `deployment_service` row
  for. Epic E3's definition of done requires the flip.
- **Owner decision, 2026-08-10.** The alternative offered was an E3-R flight on the E0-R
  precedent; the owner chose the flip at E3.13.

## D60 (2026-08-10): D40 is lifted by E3, on real reported state only (owner-approved)

- **Decision:** E3.12 replaces D40's zero-`[data-status]` guard with real device status on
  the inventory tables, the aggregator card and the Overview roll-up, all driven by
  `device_state` rows fed from LWT and reported messages, plus a Timeline tab. The guard
  stays where it still applies (config routes; import outcomes are not device states).
- **Why:** D40 exists to stop invented status, not status. E3 is the epic that supplies the
  real thing, and it named itself as the lifter. The Map (E6) and alerts (E7) stay
  untouched.
- **Consequence:** the gate check that asserts zero `[data-status]` on inventory routes is
  rewritten in the same batch, not deleted — it becomes an assertion that status renders
  where real state exists and nowhere else.

## D59 (2026-08-10): Control-plane topology — one worker module, two entrypoints, and a
Postgres LISTEN/NOTIFY bus (owner-approved)

- **Decision (worker):** `app/controlplane/runner.py` runs either as an asyncio task in the
  FastAPI lifespan or as a standalone `worker` container, from one module. Spec 3.1 draws
  Workers as a separate box, and production runs it that way; forcing a second container
  into every integration test buys nothing but minutes.
- **Decision (websocket fan-out):** reconciliation transitions happen in the worker while
  websockets are held by the API, so the two need a bus. It is Postgres `LISTEN`/`NOTIFY`:
  the worker NOTIFYs after commit, each API process LISTENs and fans out to its sockets.
- **Why not Redis**, which spec 3.2 names for websocket fan-out: spec 3.2 also calls Redis
  optional and spec 15.1's simplest self-hosted deploy omits it, and E0 wrote that promise
  into a readiness test. A bus that must work without Redis cannot be Redis. Redis stays a
  recorded future accelerator for E7/E8 behind the same seam.
- **Consequence:** no new required dependency — the bus rides the psycopg connection the
  app already holds. `test_redis_stays_optional_until_e3` is updated in E3.12 to say Redis
  stayed optional THROUGH E3, which is now a stronger statement than when E0 wrote it.

## D58 (2026-08-04): Bulk edit UI rulings (E2.8) — gating, honest slots, folded affordances

- **Commit gating is the acceptance**: Commit stays disabled until the CURRENT form
  deep-equals the payload the server last previewed; ANY change re-disables it until
  re-preview (test-pinned). The preview pane says which state it is in.
- **The modal is a size/structure MODIFIER** (`.modal-wide`, two panes) on the one
  modal vocabulary — never a second vocabulary.
- **E3 slots stay honest**: the impact grid's "Offline now" figure and the preview
  table's Status column render "—" with copy naming E3; zero [data-status] (D40).
  S4's "Publish immediately" checkbox is REPLACED by one line of copy naming E3 +
  EOE_PUBLISH_ENABLED — a disabled checkbox would imply a nearly-ready control.
- **Selections**: checkbox multiselect on the pod listeners table (spec 5.2's simple
  path; a leading display column behind manage_config) opens the modal with the
  explicit `{ids}` predicate; saved selections reopen BY REFERENCE
  (`{selection_id}`) so the server re-evaluates membership at use (D54). No
  delete/rename affordance — the API is GET/POST only. Secret keys are excluded from
  the bulk key picker in v1 (single-entity editors carry secrets; recorded).
- **Deferred with the owner**: searchable key picker (native select ships), preview
  CSV download, purpose-built schedule editor (shared with D57).
- **The gate-38 browser walk caught a real defect** the component suites cannot see:
  the secret cell's write-only Replace control overflowed its fixed-width cell and
  the neighboring cell swallowed its clicks. Fixed (flex-wrap in the value cell),
  full gate re-run green — the walkthrough exists precisely to catch this class.
- **Reference:** phase-2 E2.8; S4; spec 5.2; bulk-edit.test.tsx;
  guide/e2-verification.md.

## D57 (2026-08-04): The config editor's frontend rulings (E2.7)

- **Desktop-only** (owner decision 2026-08-04, closing DES open question 3): the
  246px-rail + provenance-table + 290px-rail layout targets 1440px; no tablet layout is
  built or promised. Field techs — the tablet role — have config read-only anyway.
- **Tab set folded**: S3's five-tab strip (Settings/Network/Secrets/Tags/Revisions)
  ships as THREE — Network is a settings group, secrets are rows inside Settings (the
  SECRET chip + bullets + write-only Replace), Tags remounts the E1.7 TagEditor,
  Revisions lists per-device drafts. First real consumer of ContextBar's tab slot;
  tab state rides `?tab=` for deep links.
- **Interactive-is-ink**: the new ToggleSwitch fills its track with ACTION ink, not the
  mockup's green (green is a status color). S3's "Show provenance" toggle is dropped —
  provenance is the screen's point, always on. "Only overridden" ships.
- **Editors**: all catalog-driven (the spec 5.3 acceptance is a fixture-only
  `test.demo_knob` growing a working editor with zero src/ references, gate-pinned);
  `capture.schedule` edits as validated raw JSON in v1 (purpose-built schedule editor
  deferred); unknown value types fall back to the JSON editor, keeping the acceptance
  true for future types. Client validation stays soft; the server's folded 422 lands
  per-key on the named rows.
- **Provenance chips** are a NON-status vocabulary (`data-provenance`, the .outcome-*
  precedent — no glyphs, never data-status; the D40 zero-[data-status] guard is
  asserted on config routes). "edited" rides the warning alias, "set here" the accent —
  flagged for DES.8 review. The revert control is text U+00D7 (no ↺ in the vendored
  fonts, D27); the diff arrow is CSS-drawn.
- **Additive changes to E1 surfaces**: HierarchyTree gains optional
  testId/ariaLabel/footer props (defaults preserve E1); InventoryLayout repointed onto
  the extracted `useHierarchyTree` hook (identical query keys — behavior-neutral,
  regression-netted by the E1 suites); ListenerDetail gains the V2·S2 effective-config
  card with an Edit deep-link, and its footer promise drops the now-delivered E2 line.
  lib/http.ts extracted from lib/inventory.ts (ApiError re-exported).
- **Reference:** phase-2 E2.7; S3/V2·S2; owner decisions 2026-08-04; the E2 design
  checklist; config-editor/config-secrets/config-rbac/config-lib test suites.

## D56 (2026-08-04): Bulk apply — one plan builder, write-at-level, draft-only, per-deployment audit

- **Decision:** preview and apply share ONE body (`{selection: inline | {selection_id},
  changes, level}`) and ONE plan builder (`app/config/plan.py`) — identical inputs
  through identical code makes "preview matches what apply then produces" structural.
  `level="target"` writes the change map onto each matched entity; a named level writes
  ONCE at the single common ancestor (a split is a 422 naming the candidates;
  organization level demands an org-wide MANAGE_CONFIG grant). The affected set is the
  honest blast radius: every aggregator/listener whose chain includes a write target,
  matched or not. Devices whose effective config would not change are `no_op` in
  preview and receive NO revision — notably, replacing a secret's value keeps the same
  marker, so it is a storage+SecretStore update with no new revision (rotation reaches
  devices via E3's §8.7 rewrap path, not desired-config). The plan models secret
  changes AS STORAGE HOLDS THEM (markers, keep-sentinel resolution) so plaintext can
  never leak into snapshots — the gate-36 suite caught exactly that defect in the
  first implementation, and the fix went into the engine. Apply is ONE transaction:
  merged override writes + draft revisions + one `config.apply` audit row PER affected
  deployment (detail: changed key names, revision ids, target counts, level — never
  values). Preview is paginated (spec 14.4's streaming deferred to E8.2, a recorded
  scale seam); preview carries no CSRF (mutates nothing) while apply does; BOTH
  evaluate through MANAGE_CONFIG visibility, which is what makes preview==apply hold
  per actor. `EOE_PUBLISH_ENABLED` joins Settings + .env.example, default off; apply
  stops at draft unconditionally and only reports the flag.
- **Reference:** spec 5.2, 14.4; phase-2 E2.6 (addendum PHASE2-4-02, project-changes
  #18); test_config_apply.py.

## D55 (2026-08-04): config_revision — per-device, un-FK'd, marker snapshots; read routes assigned to E2.6

- **Decision:** revisions target DEVICES only (`aggregator`/`listener` — the spec 7.2
  desired topics' addressees); pods and organizations never carry revisions. Shape
  (published verbatim in INTERFACES for E3): id, target_type, target_id String(100),
  deployment_id (both deliberately un-FK'd — the D33 immutable-evidence precedent:
  history outlives devices and deployments), snapshot JSONB (flat dotted keys, secret
  MARKERS never plaintext; listener snapshots exclude write-restricted service keys per
  spec 5.4 and include inventory keys; aggregator snapshots include service keys),
  schema_version=1 (spec 7.3), checksum via the D52 recipe (the snapshot IS the
  publishable payload body, so device-echoed checksums match by construction), state as
  a STRING from the spec 6.2 vocabulary (E2 writes 'draft' only; no enum migration when
  E3 uses the rest), created_by SET NULL, created_at; state is indexed (pre-pays E3's
  pending scan). The spec-13 revisions read routes no phase-2 task claimed —
  GET /aggregators/{id}/revisions, GET /listeners/{mac}/revisions, GET /revisions/{id}
  — are assigned to E2.6 because E2.8's acceptance needs them; list items omit the
  snapshot, the item carries it; D35 identical-404 discipline throughout.
- **Reference:** spec 6.1, 6.2, 7.3, 13, 5.4; phase-2 "Coordination with E3";
  app/models.py::ConfigRevision; app/api/revisions.py.

## D54 (2026-08-04): Selection grammar and evaluation — re-evaluate at use, re-filter per actor

- **Decision:** the spec-5.2 selection mechanism is the phase-doc's structured JSON with
  four predicate forms — `{tag}` (E1.7 containment parity), `{key, op: eq|ne|in, value}`
  (compares EFFECTIVE values through inheritance), `{key, op: exists}` (true iff an
  override exists at the entity or any ancestor — provenance names one of the five
  levels; inventory keys always answer false), and `{ids: [...]}` (explicit-identity
  membership, added for the spec-5.2 checkbox path; listener ids normalize as MACs) —
  under `all`/`any` nesting capped at depth 5 / 50 predicates. Value queries on secret
  keys are rejected (`exists` allowed — set-ness is not a value). Saved selections store
  the validated query VERBATIM and re-evaluate at every use, never a materialized id
  list; every evaluation re-filters through the caller's `visible_deployments`, so a
  stale grant never leaks a device. Key-predicate evaluation loads ancestor tables once
  and all override rows in one tuple-IN query (a handful of queries regardless of fleet
  size); the phase doc sanctions in-Python evaluation at v1 scale. Spec 13 ships
  GET/POST only — no PATCH, no DELETE on selections, deliberately. POST requires
  MANAGE_CONFIG in at least one deployment (a local check, not a new rbac primitive);
  preview rides VIEW_STATUS (a browse tool).
- **Reference:** spec 5.2, 13; phase-2 fixed choices + E2.5; app/config/selection.py;
  test_selections.py.

## D53 (2026-08-04): Merge semantics — the cascade IS the deep merge; three accessors by audience

- **Decision:** spec 5.1's "deep merge" is implemented as the level cascade over flat
  dotted keys: per key, the deepest chain level that sets it wins, else the catalog
  default, and the winning value replaces WHOLESALE — capture.schedule objects included
  (the E1.7 replace-never-merge precedent). There is no field-level merging of object
  values, deliberately. Effective config covers every catalog key at every level (an
  ancestor's value is what descendants inherit) except inventory keys, which materialize
  only at listener level from listener columns; chain overrides of inventory keys and
  unknown keys are ignored on read (storage validates writes; the merge is defensive).
  Container values are copied on the way out — mutating a result never reaches the
  chain or catalog. DB access is three explicit accessors by audience
  (`app/config/service.py`): `effective_for` (REDACTED — the only router path),
  `effective_raw` (markers verbatim — E2.6 snapshots), `effective_resolved` (plaintext —
  INTERNAL ONLY, E3 publisher/E4 bundles, never over HTTP). The suite runs
  property-based cases via hypothesis (new dev-only dependency, owner-approved
  2026-08-04) under a registered derandomized profile so gates stay deterministic.
- **Reference:** spec 5.1, 14.5; phase-2 E2.3; test_config_merge.py (the locked suite).

## D52 (2026-08-04): The canonical-JSON checksum recipe — a frozen wire contract

- **Decision:** revision checksums are `"sha256:" + hex(sha256(canonical_bytes))` where
  canonical bytes are `json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
  ensure_ascii=False).encode("utf-8")` — keys sorted at every depth, compact, non-ASCII
  preserved, no trailing newline. Checksums cover snapshots WITH secret markers in place:
  secrets never transit desired topics (spec 5.4, 8), so the snapshot is the publishable
  payload body and device-echoed checksums match by construction. The locked suite pins
  three golden hex digests (defaults-only listener snapshot, non-ASCII strings, float
  representations) plus a JSONB store/reload/recompute case; if any golden ever changes,
  that is a wire-protocol break for E3's ack matching, never a routine test fix.
- **Reference:** spec 6.2, 7.3; app/config/canonical.py; test_config_merge.py goldens.

## D51 (2026-08-04): Config secrets — the marker, the config: namespace, and the commit-ordering rules

- **Decision:** a secret-flagged override key never stores plaintext. The row holds the
  marker `{"$secret": "config:{entity_type}:{entity_id}:{key}"}` and the plaintext lives
  in SecretStore under that name — the new `config:` namespace beside
  `totp:`/`deployment:`/`bundle:` (flagged additive edit to the E0-owned SecretStore
  contract; the readiness round-trip test covers the new shapes, including the MAC-keyed
  listener form whose name contains colons). Wire semantics: a redacted read renders a
  set secret as the keep sentinel `{"$secret_set": true}`; PUT accepts a plaintext
  string (set/replace), the sentinel (keep — 422 if nothing is stored), or omission
  (unset). Commit ordering: SecretStore commits through its own sessions, so plaintext
  puts land immediately (an aborted caller transaction strands an unreachable secret,
  harmless — its marker never landed) and deletions are returned to the caller as
  `secret_names_to_delete` to run AFTER its commit, because deleting first would lose
  the value on rollback.
- **Reference:** spec 5.3, 12.4; phase-2 E2.2; app/config/overrides.py;
  test_entity_overrides.py.

## D50 (2026-08-04): The override level rule — at-or-above lowest level (spec over phase doc)

- **Decision:** `validate_override_map` enforces spec 5.3's direction: a key may be
  overridden at its lowest level or at any ancestor level, never below. The phase doc's
  "below is permitted" sentence loses (project-changes #17, addendum PHASE2-4-01).
  `lowest_level='any'` behaves as listener — settable everywhere. Errors are returned
  per key, all at once, sorted, each naming the key (folded into one D8
  `validation_error` 422 by the API layer in E2.4). Additional validator rules the
  documents left open: null is never a value (remove the key to unset), `object` values
  are capped at 2 KiB (opaque is not unbounded; capture.schedule's internal schema is
  firmware/E4 territory), int values reject booleans (Python bool-is-int trap).
- **Reference:** spec 5.3, 5.1; phase-2 E2.2; owner decision 2026-08-04;
  app/config/validation.py.

## D49 (2026-08-04): Inventory resolution extends to identity.name and identity.mac

- **Decision:** the catalog marks four keys `resolution='inventory'`: `location.gps_lat`
  and `location.gps_lon` (mandated by E1's INTERFACES contract) plus `identity.name` and
  `identity.mac`. All four read from listener columns (D31/D32 own those fields) and
  reject override writes with a 422 naming the key and pointing at
  `PATCH /listeners/{mac}` (arrives with E2.2's validator).
- **Rationale:** identity.* has exactly the character the E1 contract fixed for
  location.*: the listener row is the source of truth (MAC is the immutable key, name is
  DB-unique per deployment). A config override shadowing either would fork the identity
  model E1.5's services depend on.
- **Reference:** spec 5.3, 4.2; E1 INTERFACES "hierarchy schema"; phase-2 E2.2.

## D48 (2026-08-04): Service-key write block — the E5 stub (owner-directed)

- **Decision:** all eight `telemetry.*` keys plus `upload.s3_bucket`, `upload.s3_endpoint`,
  `upload.s3_access_key`, `upload.s3_secret_key` carry
  `write_restricted='service_onboarding'`: their catalog rows exist and their defaults
  merge into effective config (spec 16.4 needs that), but the generic override PUT
  rejects them with a message naming E5's onboarding flow — a documented R2 stub, not a
  missing feature. **`upload.s3_prefix` is deliberately outside the block** (owner ruling
  2026-08-04): spec 5.1 names it an aggregator-level setting the operator sets, while
  spec 16's flow writes the deployment-level endpoints/credentials.
- **Rationale:** spec 5.3's closing paragraph — "the deployment services onboarding flow
  writes them rather than the operator editing them key by key" — plus E2's out-of-scope
  list deferring that flow to E5. Blocking now avoids two writers when E5 lands.
- **Reference:** spec 5.3, 5.1, 16; phase-2 "Out of scope"; owner decisions 2026-08-04.

## D47 (2026-08-04): Catalog storage — singular table, in-migration convergent seed, schema-document endpoint

- **Decision:** the spec-5.3 catalog is a `settings_catalog` table (singular per D30)
  seeded in-migration from the `app/config/catalog.py::CATALOG` constant — the single
  source, gate-pinned against a hardcoded spec key list AND against the seeded rows
  field for field. `seed_catalog()` is an upsert-plus-prune, so replays converge on the
  current constant (neutralizing the import-app-code-in-a-migration hazard); catalog
  evolution = constant edit + sync migration + `CATALOG_VERSION` bump in one batch.
  `GET /config/catalog` returns `{version, items}` sorted by key — a schema document,
  deliberately NOT a D7 list envelope (it is rendered wholesale, never paginated).
  `lowest_level='any'` (logging.verbosity) behaves as `listener` for the level rule.
  The DB column for the default is `default_value` (SQL keyword avoidance); the wire
  field stays `default`. Bounds added beyond the spec table: confidence_threshold 0-1
  (definitionally), noted in the row.
- **Reference:** spec 5.3; phase-2 E2.1 and "Catalog storage"; test_settings_catalog.py.

## D46 (2026-08-04): Gate-30 commit message reworded — one-time deviation from R3's never-amend clause (owner-approved)

- **What happened:** the original gate-30 commit message named the repository's
  instructions file by filename. That filename contains an R3 forbidden substring, and
  `test_git_hygiene.py` scans every commit message in history case-insensitively — so CI
  on PR #13 went red (`backend-tests` → `ci-green`), and every future gate and CI run on
  any branch containing that commit would fail forever. A follow-up commit cannot remove
  a string from history.
- **Decision:** reword the commit message only (tree byte-identical; the gate-30 content
  and its green result are untouched), move the `gate-30` tag to the reworded commit, and
  force-push branch + tag. This deviates, once, from R3's "never amend or force-push a
  tagged gate commit" — the two R3 clauses were in genuine conflict, and the owner chose
  clean history over tag immutability (the alternative was whitelisting the filename in
  the scanner, weakening the bright line). Owner-approved 2026-08-04.
- **Why the gate could not catch it:** the gate runs BEFORE the commit exists; a commit
  message defect is only ever caught by CI or the NEXT gate, after the push. This is a
  structural blind spot of the gate-then-commit sequence, not a broken test.
- **Prevention (binding on future sessions):** never write the instructions file's
  filename — or any other R3 forbidden substring — in git-visible text: commit messages,
  PR titles/bodies, issues, tags, release notes. Refer to it as "the project instructions
  file". The PR #13 body was edited to comply in the same batch.
- **Reference:** rule R3 (attribution + push protocol); `backend/tests/test_git_hygiene.py`;
  PR #13 CI runs of 2026-08-04.

## D45 (2026-08-04): Rules 1.1.0 — walkthrough currency joins R1 (owner-directed)

- **Decision:** `.claude/rules/project-rules.json` gains `R1_record_keeping.verification_walkthrough`
  (version 1.0.0 → 1.1.0; CLAUDE.md restates it): every epic ships its own
  `guide/e{N}-verification.md` (indexed in guide/README.md) before its final gate, and an
  epic that invalidates a prior walkthrough's assertions amends them **in the same batch
  that invalidates them** — the clause that catches E3 flipping E1's no-status
  expectations. Walkthrough changes ride gated batches like everything else.
- **Why a rule and not convention:** the split is mechanical vs prose. `qa-stack.ps1`
  tracks the product automatically (current images, current migrations, the test-pinned
  demo fixture); the walkthrough is prose that nothing compels forward — exactly the
  drift class already observed twice (frontend-guide outside the record loop, hygiene F6;
  the stale DES handoff statements). Owner directed the amendment 2026-08-04 before E2
  planning so E2 becomes the rule's first subject.
- **Governance mechanics:** the new key sits BESIDE `logs`/`addendum_convention`
  (test_governance pins `logs` as an exact set); rules-JSON structure re-validated by the
  gate.
- **Reference:** rule R1; guide/e1-verification.md; DECISIONS D44; hygiene finding F6
  (project-updates 2026-08-01).

## D44 (2026-08-02): qa-stack.ps1 — the manual-QA stack and the ports rule

- **Decision:** `qa-stack.ps1` (repo root) is the one-command manual-QA entry point:
  `up` builds and starts the documented compose stack under the dedicated project name
  **`eoe-qa`**, generates `deploy/.env` with fresh local secrets when missing (never
  printing values; `.env` is gitignored), seeds `app.seed --demo` host-side, health-
  probes both ends, and prints the site URL + owner credentials. The seed's refusal on
  re-run is treated as the idempotent "already seeded" path, not a failure. `down`
  keeps the `eoe-qa_postgres_data` volume (QA data survives restarts); `reset` wipes
  it; `status` reports. `guide/e1-verification.md` is the walkthrough it exists for.
- **The ports rule, recorded as the fix for the gate-15-class incident:** the gate's
  compose suites (`eoe-gate-test`, `eoe-verify-test`) bind the SAME host ports
  (8000/5173/5432/6379), so a running QA stack reds the gate. The script prints a boxed
  tear-down-before-gate warning on every `up`, and the walkthrough repeats it twice.
  Distinct project names isolate containers/volumes, not ports — the warning is the
  remedy, not a workaround.
- **Scope note:** tooling + operator documentation only; no production code, no test
  changes, no planning-document impact (hence no project-changes entry). Delivered
  through the full rhythm (branch, green gate, PR) — the frontend-guide record-skipping
  mistake (hygiene finding F6) deliberately not repeated. `guide/README.md`'s TOC also
  gains its missing `bulk-import.md` row.
- **Reference:** guide/e1-verification.md; guide/getting-started.md;
  backend/tests/test_compose_stack.py; project-updates 2026-07-30 (the gate-15
  collision); D43.

## D43 (2026-08-02): The demo fixture — seed --demo semantics and the verifier's E1 walk

- **Decision:** `uv run python -m app.seed --demo` seeds the canonical hierarchy in the
  same one command (fresh DB: owner + hierarchy, password still printed exactly once;
  existing owner: hierarchy only, nothing re-printed; existing demo org: refuse, exit 1).
  The **no-flag path is byte-identical to E0.12's** — `test_seed.py` runs unchanged. The
  fixture is fully deterministic (no randomness): org "Earth Echoes Demo"; Redwood Coast
  (`redwood-coast`) and High Desert (`high-desert`); three named pods each; aggregators
  `demo-agg-rc-01..03`/`demo-agg-hd-01..03`; 28 listeners at 8/5/3 + 6/4/2 with
  locally-administered MACs (`02:EE:0E:…`), even-index GPS, first-listener pod tags —
  documented by name in INTERFACES so E2/E6 reference rows without re-deriving them, and
  mirrored exactly by `frontend/tests/inventory-fixture.ts`. One system audit row
  (`inventory.seed_demo`) summarizes counts.
- **Verifier:** `verify.py` gains an 11-step E1 walk over real HTTP (create deployment →
  pod-with-aggregator in one call → listener by MAC → E1.4 reject/suffix pair → E1.7 tag
  replace → D35 scoped-visibility and 404-oracle checks → 409-with-blockers → leaf-up
  teardown); cleanup's safety net now removes hierarchy rows under any `verify-%`
  deployment children-first, so a run that dies mid-walk still leaves nothing.
- **Reference:** phase-1 E1.9; DECISIONS D20 precedent (verifier DB-side bootstrap);
  backend/tests/test_seed_demo.py; guide/seed-script.md.

## D42 (2026-08-02): .admin-table generalizes to .data-table; gate-27 test retitles

- **Decision:** `.admin-table` (E0.9, UsersAdmin-only) becomes the shared **`.data-table`**
  vocabulary with the v2 header treatment (raised mono uppercase band) — one table
  language for E1.8's four tables and everything later (INTERFACES: "no second
  vocabulary"); UsersAdmin repointed in the same commit, its `users-table` testid and
  suite untouched. E1.8 also ships the first reusable `.form`/button vocabulary
  (`.auth-form` stays login-specific), including `.btn-danger` as surface-fill +
  alerting ink + tinted border — never a filled red button.
- **Test changes at this gate (R0):** `shell.test.tsx`'s route table gains four inventory
  rows and its `/` heading row follows the Overview retitle to "Organization overview"
  (project-changes #16); `auth.test.tsx`'s two post-login assertions follow the same
  retitle. Both are consequences of a recorded plan change, not weakenings; every other
  assertion is untouched and the suite grew 44 → 60.
- **Reference:** INTERFACES "Frontend composition"; project-changes #16; DECISIONS D25.

## D41 (2026-08-02): ContextBar crumbs become real links

- **Decision:** `Crumb.to` now renders a router `<Link>` (the DES.7 component rendered
  `to` as a styled span — declared but never wired); the final crumb carries
  `aria-current="page"`. Additive change to a DES-owned component, flagged here per
  INTERFACES' rule: E1.8 is the ContextBar's first real consumer and the breadcrumb is
  its reason to exist (D25). `to`-less usage (the Map page) is unaffected. Noted for
  DES.8's review.
- **Reference:** INTERFACES "Frontend composition" (ContextBar contract); D25.

## D40 (2026-08-02): No fabricated status anywhere in E1's UI

- **Decision (owner-directed, 2026-08-02):** E1.8 builds the tree, tables, and detail
  surfaces to the design geometry with status slots designed in, but renders **no device
  state anywhere** — no StatusChip rows, no rollup dots, no distribution bars, no
  "devices online" hero — because no reported state exists until E3 wires MQTT, and the
  project rule is no mock data, ever. The honesty is gate-enforced:
  `inventory-tree.test.tsx` and `overview.test.tsx` assert **zero `[data-status]`
  elements** on every inventory route and the Overview. Where the mockups draw status,
  E1 shows structure/counts/identity and names the owning epic in visible copy ("Device
  status arrives with E3 · services with E5"). E3 removes the guard deliberately when
  real state lands. Flagged for DES.8's review (the mockups draw dots the product
  intentionally omits).
- **Reference:** epic plan owner decisions; S7's own copy ("Postgres-owned data stays
  live"); DECISIONS D25.

## D39 (2026-08-02): @tanstack/react-table — E1's one new frontend dependency

- **Decision:** `@tanstack/react-table` ^8.21 (the phase doc's fixed choice for E1.8's
  device tables; the frontend guide records installation as an explicit decision, made
  here). Used strictly headless: all rendering through the shared `.data-table` classes;
  `manualSorting`/`manualPagination` because the D7 envelope makes the server the source
  of truth — the page serializes TanStack state to the wire grammar
  (`sort=name|-name`, `limit`/`offset`). No form, validation, or CSV libraries were
  added; the server is the parser and validator (D38).
- **Reference:** phase-1 E1.8; docs/frontend-guide.md "Starting E1"; D7.

## D38 (2026-08-02): Bulk import — 200-with-report, all-or-nothing default, savepoint rows

- **Decision:** `POST /listeners/import` and `POST /aggregators/import` accept
  `application/json` (`{"rows": [...]}`) or raw `text/csv`; options ride the query string
  (`?partial=`, `?auto_suffix=` — listeners only) because a CSV body cannot carry them.
  Limits: 1000 rows, 1 MiB. A well-formed request always answers **200 with a job
  report** `{committed, created, failed, rows: [{row, status, entity_id, name, error}]}`
  — row results are data, not an error envelope, and row `error.code` strings reuse the
  D8 vocabulary as data without extending the wire codes. All-or-nothing is the default:
  any failed row rolls back every row AND the audit record (suite-proven), and the
  committed=false report doubles as the dry run the E1.8 UI shows before an explicit
  partial accept. Rows execute under per-row SAVEPOINTs so constraint violations become
  row errors, and flushed rows are visible to later rows' collision checks — in-file and
  DB duplicates share one code path. Scope is enforced **per row** (cross-scope rows are
  row-level `forbidden`), so the endpoints need only session + CSRF. Audit: one
  `<entity>.import` row per request with counts, flags, and created ids.
- **CSV documentation split:** the column format is normative in `docs/INTERFACES.md`
  (phase-doc requirement); `guide/bulk-import.md` shows operator examples and defers to
  it (PHASE0-2-01 routes operator material to /guide) — both, no conflict.
- **Reference:** phase-1 E1.6; spec 13; DECISIONS D8, D35; backend/tests/test_bulk_import.py.

## D37 (2026-08-02): Report-time identity — return-based services, append-only quarantine, deduped alerts

- **Decision:** E1.5 ships as `app/inventory/identity.py` — services plus two tables, no
  HTTP surface, no UI (E3.5 wires MQTT and must not reimplement the logic). The API is
  **return-based**: `handle_reported_identity(db, ReportedIdentity) ->
  IdentityResolution{outcome, listener, quarantined, alert}` with outcomes
  MATCHED / NAME_CONFLICT / MAC_CONFLICT / PROVISIONING_REQUIRED / UNKNOWN_MAC — friendlier
  for E3's consumer loop than exception control flow; `require_known_aggregator` provides
  the raising variant (`ProvisioningRequiredError`) for ingest paths.
- **Semantics fixed here:** conflicts NEVER touch inventory rows (suite proves
  byte-identical reload); `quarantined_report` **appends** — every conflicting report is
  evidence — and carries **no FK to listener** (must survive deletion and describe devices
  inventory never held); `inventory_alert` **dedupes on the open alert** per
  (alert_type, entity_type, entity_key) via a partial unique index
  (`WHERE resolved_at IS NULL`), app-checked first so a repeat conflict returns the
  existing alert; a resolved alert permits a fresh one. `alert.deployment_id` is scope
  for filtering, deliberately un-FK'd (same reasoning as audit scope, D33).
  `duplicate_identity`/`provisioning_required` are **alert types, not wire error codes** —
  the closed D8 vocabulary is not extended. Services stage rows and never commit; audit
  rows (`inventory.quarantine`, `inventory.alert`) are system-originated (actor NULL).
- **Reference:** spec 4.3 items 2-3, spec 17 item 9; phase-1 E1.5; project-changes #15
  (PHASE1-4-03); migration `05c4858bfab5`; backend/tests/test_identity_service.py.

## D36 (2026-08-02): The deployment slug freezes at the first pod

- **Decision:** the concrete rule behind the phase doc's "editable before first use":
  `slug` may be set at create (else generated: NFKD-strip to ASCII, lowercase, squash
  non-alphanumerics to hyphens, trim, cap 63, collision suffix `-2`, `-3`, …) and changed
  via PATCH **only while the deployment has zero pods**; afterwards a differing slug is
  409 `conflict`. "First use" means "first child pod" because the `{dep}` MQTT namespace
  (spec 7.2) only matters once devices can exist under it. E3 may tighten this rule
  (e.g. freeze permanently once a broker is live), never loosen it. Known edge, accepted:
  a deployment that had pods, deleted them all, may change its slug again pre-E3 —
  recorded in INTERFACES so E3 re-examines it.
- **Reference:** phase-1 §2; spec 7.2; test_hierarchy_crud.py slug lifecycle tests.

## D35 (2026-08-02): Scoped visibility — the filter, the permission map, and the 403/404 asymmetry

- **Decision:** `app/scoping.py` is the single source for result-set visibility:
  `visible_deployments(assignments, permission)` -> `"all" | set[ids]`,
  `scope_filter(...)` for lists (deployments on id, pods on deployment_id, aggregators
  via the pod join, listeners on the D32 stamp), `require_any_assignment` for surfaces
  every role may read. Permission map: reads = VIEW_STATUS (org reads = any assignment);
  child writes = MANAGE_DEVICES in the target deployment + CSRF; org writes and
  POST /deployments = org-level MANAGE_DEVICES. **No change to rbac.py anywhere in E1** —
  the locked matrix and the frontend parity test are untouched; this suite lives in the
  new `test_scoping.py`, not in the test-critical file.
- **The asymmetry:** `/deployments/{deployment_id}` routes keep E0.7's 403-before-lookup
  pattern (safe: the check precedes any existence lookup). Child items answer **404 for
  out-of-scope and missing alike** — MACs are enumerable (OUI + counter), so a
  403-on-existing would be an existence oracle; the suite asserts the two 404 bodies are
  byte-identical. POSTs answer 403: the client supplied the parent scope, denial confirms
  nothing.
- **Reference:** spec 12.3, 13; DECISIONS D32; backend/tests/test_scoping.py.

## D34 (2026-08-02): Organization surface — no DELETE, single-org POST clamp

- **Decision:** spec 13 lists no DELETE for `/organizations` and wins over E1.2's "all
  five entities" wording (owner-confirmed 2026-08-02; project-changes #13, addendum
  PHASE1-4-01). POST /organizations 409s while an organization exists (spec 12.1
  single-org v1; project-changes #14, PHASE1-4-02). Cross-reference D32: the clamp is
  what makes global `aggregator_uuid` uniqueness equal the spec's within-org rule; a
  future multi-org change relaxes both together. Org reads are gated by
  `require_any_assignment` (a deployment-scoped operator still needs the org name for
  the tree); org writes need org-level MANAGE_DEVICES.
- **Reference:** spec 13, 12.1; phase-1 §4 E1.2; DECISIONS D32.

## D33 (2026-08-02): E1.1 flips the role_assignment FK; audit scope is never one

- **Decision:** `role_assignment.deployment_id` gains its real foreign key (the seam phase-0
  E0.7 fixed explicitly), plain NO ACTION; `audit_log.scope` is **deliberately never FK'd**,
  permanently — D3 immutability means audit rows outlive the deployments they reference.
- **Consequences, recorded before the gate (rule R0):** (1) readiness test
  `test_scope_columns_are_uuid_nullable_and_not_yet_foreign_keys` is replaced by two tests —
  the role_assignment half inverted as the test was designed to be, the audit half made
  permanent with a test asserting NO FK ever appears. (2) `test_rbac.py` (test-critical)
  receives an **additive fixture change only**: the module fixture inserts an organization
  and real deployment rows for DEPLOYMENT_A/B because scoped grants now reference real rows;
  no assertion or matrix row changed. (3) `test_users_admin.py` likewise creates a real
  deployment; a new test pins the 422. (4) `/users` assignment bodies now pre-validate
  deployment existence (422 `validation_error`) so an FK violation is never miscaught by the
  email-conflict IntegrityError handler. (5) Migration `53181716569c` DELETES orphan scoped
  grants rather than NULLing them — NULL means org-wide, so NULLing would silently escalate
  a scoped grant; deleted orphans referenced deployments that never existed and are accepted
  as unrestorable. (6) `verify.py` bootstraps a real `verify-dep-{tag}` deployment (and org
  if none exists) for its scoped-operator step and removes both in cleanup.
- **Reference:** phase-0 E0.7; phase-1 E1.1; docs/INTERFACES.md role_assignment section;
  DECISIONS D3.

## D32 (2026-08-02): Listener carries a set-once deployment_id stamp; aggregator_uuid unique globally

- **Decision:** `listener.deployment_id` is a denormalized, **set-once** FK: parent fields
  (`organization_id`/`deployment_id`/`pod_id`/`aggregator_id`) are create-only across the
  whole hierarchy — no re-parenting in v1, PATCH models reject them (`extra="forbid"`) — so
  the stamp is computed server-side at create (aggregator→pod→deployment walk) and cannot
  drift. It exists because spec 4.3's "listener name unique within its Deployment" must be a
  real constraint (phase-1 §2: "constraint plus application check") and a unique constraint
  cannot span a 3-hop join. `aggregator_uuid` gets a plain **global** UNIQUE: v1 is
  single-organization (spec 12.1), so global uniqueness implies the within-org rule with no
  denormalized org column anywhere.
- **Spec 12.1 reconciliation:** 12.1 forbids stamping the *tenant* id on every table. One
  deployment id on exactly one table is not a tenant stamp; no `organization_id` is
  denormalized anywhere. Multi-org later relaxes the aggregator_uuid constraint to a
  composite in one migration (cross-reference D34's single-org clamp — both must move
  together).
- **Rejected:** triggers (invisible to autogenerate, unnamed by the convention); app-level
  checks alone (phase doc demands a constraint); composite-FK chains (redundant once parents
  are immutable).
- **Reference:** phase-1 §2 fixed choices; spec 4.2/4.3, 12.1; test_hierarchy_schema.py.

## D31 (2026-08-02): MAC is the listener primary key, literally

- **Decision:** `listener.mac String(17)` is the PRIMARY KEY, CHECK-constrained to uppercase
  colon-separated form; the API normalizes case/separators at the boundary. No surrogate
  UUID. Spec 4.2 calls MAC "the immutable primary identity for a Listener across the whole
  platform"; the phase doc's fixed choice is "Listeners key by MAC"; `session.id` is the
  in-repo natural-PK precedent; `audit_log.entity_id` was sized for a MAC from E0.8.
  Rename-safety is a non-issue: MAC is immutable by spec — PATCH never accepts it, and a
  typo'd MAC is a different physical device, fixed by delete + recreate.
- **Reference:** spec 4.2; phase-1 §2; readiness test `test_audit_entity_id_fits_a_mac_address`.

## D30 (2026-08-02): Hierarchy tables are singular, matching E0; routes stay plural per spec

- **Decision:** `organization`, `deployment`, `pod`, `aggregator`, `listener` — singular,
  like every E0 table — although phase-1 E1.1's task text spells them plural. The naming
  convention templates bake table names into constraint names, so consistency is
  load-bearing; URL collections stay plural exactly as spec 13 writes them (`/organizations`
  …), the split `user` table / `/users` route already established. Table names land verbatim
  in the readiness `E0_TABLES` lock.
- **Reference:** phase-1 §2 E1.1; app/db.py NAMING_CONVENTION; spec 13.

## D29 (2026-08-01): Test changes in the records-hygiene batch — two strengthenings

- **Decision:** two tests change in this batch, both at a green gate and both adding
  assertions rather than removing them. (1) `backend/tests/test_governance.py`
  (`test_planning_documents_unmodified_except_appended_addenda`) gains a non-empty guard on
  the `planning-baseline` file list: D23's rescope iterates `git ls-tree` output, and if the
  tag were renamed or the path misspelled, `ls-tree` exits 0 with empty stdout — the loop
  would run zero times and the invariant would pass having verified nothing. The guard
  (`assert len(baseline_names) >= 7`) mirrors `test_planning_documents_tracked_by_git` and
  makes that failure loud. (2) `frontend/tests/users-admin.test.tsx`: the test named "hides
  the sidebar link from a viewer and shows it to an owner" never asserted the viewer half
  (pre-existing — verified identical at `23eff5d`), and D25 deliberately made the Users link
  visible to every role, so the name documented an invariant the product had abandoned while
  passing vacuously. It is replaced by two tests asserting D25's actual intent: the link is
  visible to a viewer AND to an owner; the viewer's content gating stays covered by the
  existing "denies the page to a viewer" test.
- **Reference:** rule R0 (record test changes); DECISIONS D23, D25; project-changes #12;
  review of `23eff5d..f93f061` (2026-08-01).

## D28 (2026-08-01): Late record — Gate 16 changed a fifth test, and what it means for E0.4's acceptance proof

- **Decision (record-only, no code change):** D26 presents four test fixes at the DES.7 gate
  as the complete inventory ("All four are corrections"). A fifth change shipped in the same
  commit (`5347eeb`) unlisted: `frontend/e2e/theme-swap.spec.ts` was rewritten from injecting
  the alt sheet via `page.addStyleTag(ALT_SHEET)` to driving the real theme toggle, because
  D24 scoped the night sheets to `:root[data-theme="dark"]` and stylesheet injection went
  inert. The rewrite is a net strengthening — 2 e2e tests became 4 (persistence across
  reload; the status palette relit on `/map`) and the swap is now exercised through the path
  a user takes — but it was explained only in the spec file's header comment, and it changed
  what proves E0.4's acceptance criterion: "swapping its values visibly restyles the shell"
  is now demonstrated via `lib/theme.ts` flipping `data-theme`, not via a bare sheet swap
  with zero code changes. Recorded so D26 does not stand as complete and the criterion's
  changed proof is written down (see PHASE0-4-06).
- **Reference:** rule R0 (record test changes); DECISIONS D24, D26; commit `5347eeb`
  (Gate 16); project-changes #12.

## D27 (2026-07-31): Fonts vendored, and the status glyphs get their own 568-byte subset

- **Decision:** the three typefaces the token sheets name ship as latin-subset woff2 files in
  `frontend/public/fonts/`, declared in the new `frontend/src/styles/fonts.css` — IBM Plex
  Sans 400/500/600, IBM Plex Mono 400/600, Source Serif 4 600. Only weights the CSS uses are
  vendored. A **seventh** file, `eoe-status-glyphs.woff2`, carries the six status glyphs, and
  a new additive token `--eoe-font-family-glyph` (D21 terms) points `.status-glyph::before`
  at it.
- **Why the seventh file — the finding that forced it:** the six status glyphs are Geometric
  Shapes and Dingbats codepoints (`●` U+25CF, `◐` U+25D0, `▲` U+25B2, `■` U+25A0, `✕` U+2715,
  `◆` U+25C6), and **none of them exists in IBM Plex Sans, IBM Plex Mono, or Source Serif 4**
  — verified against the *complete* families with fontTools, not merely against these
  subsets. So vendoring the text faces alone would have left every status shape to whatever
  the host happens to have installed. That is the failure the Gate 16 entry saw as a hairline
  `◐` in headless Chromium, and on a minimal air-gapped host (spec §15.1) it degrades to tofu
  — which silently deletes one of the three channels the status vocabulary is built on
  (`docs/INTERFACES.md`, "Status vocabulary"). Shapes are load-bearing, so they are vendored
  like everything else: Noto Sans Symbols 2 (OFL 1.1) subsetted to exactly those six
  codepoints, 568 bytes.
- **Alternatives considered:** (a) swap to glyphs the text families do cover — Plex offers
  `◊`, `✓` and arrows, not six shapes that stay distinct at 10px, so the vocabulary would
  have shrunk to fit the font; (b) draw the shapes in CSS with `clip-path` — no font
  dependency, but it replaces one token per status with a rule per status and breaks the
  `content: var(--…-glyph)` design the sheets already encode. Both were rejected as worse
  than 568 bytes.
- **Gate enforcement:** `frontend/tests/fonts.test.ts` — every `@font-face` src resolves to a
  committed file; no `url(https:…)` or `@import` in any sheet (vendored means vendored); every
  first-choice family in a `--eoe-font-family*` token has an `@font-face`; **the glyph
  subset's `unicode-range` covers every status glyph token**, so a seventh status added later
  without re-cutting the subset fails the gate instead of shipping as tofu; and
  `.status-glyph::before` still names the glyph family.
- **Licensing:** all three families are OFL 1.1; each license text ships beside the fonts as
  the OFL requires (`LICENSE-ibm-plex.txt`, `LICENSE-source-serif-4.txt`,
  `LICENSE-noto-sans-symbols-2.txt`). Whole set ≈160 KB.
- **Reference:** `project_planning/DES-track-handoff.md` "The three rules" item 3; spec §15.1;
  project-changes #10.

## D26 (2026-07-30): Test fixes at Gate (DES.7 batch)

Rule R0 requires recording tests changed at a red gate. All four are corrections, not
weakenings.

- **`tokens.test.ts` check 1** matched named colors anywhere in a declaration, so
  `white-space: nowrap` failed. Now scans the value only; `color: white` still fails.
- **`tokens.test.ts` check 5** forbids components *importing* a night sheet, but matched the
  filename in prose too, tripping on `lib/theme.ts`'s own header comment. Now matches
  `import "…tokens.alt.css"`.
- **`tests/setup.ts` stubs `window.matchMedia`** — jsdom has no media queries, so
  `lib/theme.ts`'s `prefers-color-scheme` probe threw. Reports "not dark". No coverage lost:
  real resolution, override, and persistence are checked in `e2e/theme-swap.spec.ts`.
- **`auth.test.tsx`** asserts the account by accessible name, not text: D25's top bar shows
  an initials avatar and the email is now `aria-label`/`title`. Same invariant, stronger
  form — it checks what a screen reader announces.
- **Reference:** rule R0 on_failure; `frontend-tests` gate run, DES.7 batch.

## D25 (2026-07-30): DES.7 shell restructure — dark top bar, and primary nav lists every destination

- **Decision:** `Shell.tsx` becomes V2·S1's dark top bar with horizontal nav over an optional
  context band, replacing E0.4's left sidebar.
  `project_planning/DES-track-handoff.md` item 4 names this DES.7's one structural change:
  the map needs full viewport width and the breadcrumb needs a permanent home. `shell-sidebar`
  → `shell-topbar`; regions, `aria-label="Primary"`, and routes otherwise unchanged. New
  shared components: `ContextBar`, `PageHeader`, `StatusChip`/`StatusLegend`, `EmptyState`,
  `ThemeToggle`.
- **The consequential half: primary nav lists every destination for every role,** rather than
  hiding entries behind `<Can>` as the E0.7 sidebar did. Hiding a section teaches a wrong map
  of the product and makes a permissions problem look like a missing feature. Pages gate their
  own contents instead (`UsersAdmin` already did), and backend RBAC remains the authority.
  An affordance change, not a security one — no endpoint's protection depended on a hidden link.
- **The four new skeleton pages carry no gate,** deliberately: they display no data, only which
  epic brings the surface. Each gets its gate in that epic.
- **Rejected:** rendering unpermitted entries visibly disabled (the handoff's read of spec
  §12.3). Right once roles are routinely exercised; during the skeleton phase every entry would
  render disabled for a signed-out reviewer. Revisit at DES.8.

## D24 (2026-07-30): Night theme ships — D21's dark-palette gap closed, selector-scoped

- **Decision:** D21 left one gap open — nothing carried dark values for the extension keys, so
  a dark marker, badge, or table cell rendered a near-black status color on a near-black
  surface. `frontend/src/styles/tokens.ext.alt.css` closes it. `tokens.alt.css` stops being a
  test fixture: `main.tsx` imports both night sheets unconditionally and `lib/theme.ts` sets
  `document.documentElement.dataset.theme`.
- **Selector, not import order:** both night sheets are scoped to `:root[data-theme="dark"]`,
  outranking the light sheets' plain `:root`. Reordering imports cannot change which theme
  wins, and nothing is injected or disabled at runtime. Check 10 fails the gate on a bare
  `:root` in either night sheet.
- **Resolution:** a stored choice wins and pins the theme; otherwise `prefers-color-scheme`
  decides and keeps deciding. The manual override is not optional — field staff read this
  outdoors in daylight, where the OS setting is wrong.
- **Color keys only.** Glyphs, spacing, type, density, motion, and border widths are
  theme-independent. Check 9 fails if the night sheet defines a key the light extension does
  not (it would resolve in dark, be undefined in light); check 8 mirrors check 7 so
  `danger`/`success`/`warning` cannot drift from their status aliases in either theme. Every
  status color was contrast-verified per pair against its tint, `surface`, and `bg`; lowest in
  the set is 4.8:1.
- **New keys in `tokens.ext.css`, same D21 terms (nothing renamed or repointed):**
  `--eoe-color-action-contrast-muted`, `-action-raised`, `-accent-on-action`, `-brand-mark` —
  the chrome is `--eoe-color-action` in *both* themes, so anything sitting on it needs an
  on-dark pair; `--eoe-radius-pill`/`-round` (shape constants, not ramp points);
  `--eoe-height-topbar`/`-contextbar` (new `--eoe-height-*` namespace for fixed app furniture,
  which is not a control height).
- **Rejected:** toggling `<link disabled>` at runtime (flash of wrong theme, not statically
  analyzable); a `prefers-color-scheme` media block (no manual override, the requirement that
  matters most here).

## D23 (2026-07-30): Test fix at Gate (DES batch), planning-doc governance check scoped to the actual baseline set

- **Decision:** `backend/tests/test_governance.py::test_planning_documents_unmodified_except_appended_addenda`
  iterated every `*.md` file currently present in `project_planning/` and required each to
  have an identical counterpart in the `planning-baseline` git tag, crashing (not failing
  cleanly) on any file that didn't exist at baseline. This batch adds
  `project_planning/DES.4-handoff.md` and `project_planning/DES-track-handoff.md` — DES-track
  handoff/rationale material, not the fixed spec/plan/handbook/phase documents the baseline
  tag actually pins (implementation-handbook.md section 1's authority order names exactly
  those five kinds of document as "binding"). The test now walks
  `git ls-tree --name-only planning-baseline project_planning/` instead of the live directory
  listing, so it diffs only the documents that were actually part of the frozen baseline.
  New, non-baseline files in `project_planning/` are simply outside what this invariant
  covers — there is nothing in the baseline tree to diff a new file against.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. Not a weakening:
  the seven originally-baselined documents are exactly as protected as before (still diffed
  byte-for-byte outside appended addenda); the test's old behavior of hard-crashing on any
  new sibling file was an artifact of nothing having been added to the directory since E0.0,
  not a deliberate invariant that new files are forbidden.
- **Owner directive:** the project owner asked directly for DES-track handoff/rationale docs
  to live in `project_planning/`, not `docs/` — they are project-planning material, not
  engineering-internal logs. `docs/DES.4-handoff.md` moves to
  `project_planning/DES.4-handoff.md`; `docs/HANDOFF.md` moves to and is renamed
  `project_planning/DES-track-handoff.md` (its content spans DES.1–DES.8, so the generic name
  no longer fit next to a track-scoped one).
- **Reference:** rule R0 on_failure; `backend-tests` gate run during the D21 (DES-4-01) batch;
  `test_planning_documents_tracked_by_git` (unaffected, still a `>= 7` lower bound).

## D22 (2026-07-30): Test fix at Gate (DES batch), theme-swap assertion no longer checks font/spacing

- **Decision:** `frontend/e2e/theme-swap.spec.ts` asserted that `fontFamily` and the
  sidebar's computed `padding` change when `tokens.alt.css` is swapped in. Both assertions
  now fail: the DES.4 v2 night theme deliberately keeps the same type family and the same
  `--eoe-space-*` scale as the light sheet ("relit rather than inverted" — only color and
  shadow values change; see `tokens.alt.css`'s own header comment). The assertions checked
  an artifact of the old *synthetic* alt sheet (E0.4-era: an arbitrary Georgia/mono/zero-radius
  fixture designed so every property category visibly differed), not an actual product
  requirement — nothing in spec 3.2 or the DES track's direction calls for the night theme to
  use a different typeface or rhythm. Replaced with `sidebarBackground`
  (`--eoe-color-surface`) and `sidebarBorderColor` (`--eoe-color-border`), which do differ
  between the two real themes and still prove the swap mechanism (loading the alternate sheet
  changes computed styles with zero code changes) end to end.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. This is a test
  correction, not a weakening: the invariant under test — "swapping token values visibly
  restyles the shell" (E0.4 acceptance criterion) — still holds and is still checked on real
  computed styles; only the specific CSS properties asserted changed, because two of the four
  original properties are no longer expected to differ by design.
- **Reference:** rule R0 on_failure; `frontend-e2e` gate run during the D21 (DES-4-01) batch;
  docs/INTERFACES.md "Design tokens".

## D21 (2026-07-30): DES-4-01 accepted — additive status/border/density token namespaces

- **Decision:** `docs/INTERFACES.md` "Design tokens" fixed five namespaces and DES.4's brief
  was a replacement *value set* for the existing property names only. The six device states
  spec §9.3/§6.2 requires (`streaming/healthy`, `sleeping`, `degraded`, `offline`,
  `alerting`, `drifted`) cannot be built inside the locked `danger`/`success`/`warning` set
  without collapsing distinct states (`sleeping` into `offline`, `drifted` into `failed`),
  which spec §6.5/§6.2 treat as meaningfully different. **Accepted as proposed, additive
  only:** `frontend/src/styles/tokens.ext.css` extends `--eoe-color-*`, `--eoe-space-*`, and
  `--eoe-font-*` with new keys, and introduces new namespaces `--eoe-border-width-*`,
  `--eoe-row-height-*`, `--eoe-control-height-*`, `--eoe-duration-*`, `--eoe-ease`. No
  existing key is renamed, removed, or repointed; `danger`/`success`/`warning` keep their
  names and are aliased to `status-alerting`/`status-healthy`/`status-degraded` so the two
  vocabularies cannot drift apart. Each status carries a color, a tint, and a glyph
  (`--eoe-color-status-{name}`, `-tint`, `-glyph`) — color is never the only channel spec
  §9.3 badges/markers/chips rely on, and the six-value status vocabulary is now closed.
  `frontend/src/main.tsx` imports the sheet; `frontend/tests/tokens.test.ts` treats it as a
  third application-owned sheet (alongside `tokens.css`/`tokens.alt.css`), not a literal
  leak, and check 7 asserts the `danger`/`success`/`warning` values stay byte-equal to their
  status aliases (a real cross-sheet `var()` reference isn't possible without coupling
  `tokens.css` to the extension, so the sync is gate-enforced instead).
- **Rejected alternatives:** reusing `danger`/`success`/`warning` for six states (loses the
  `sleeping`/`offline` and `drifted`/`failed` distinctions spec §6.2/§6.5 require); literals
  in a separate `status.css` module (defeats the DES.7 theme-swap guarantee — a dark theme
  would leave status colors behind); encoding status in a data attribute and resolving color
  in JS (moves theme values into TS, the same gate problem one layer removed).
- **Separable bug fix, included in the same change:** `frontend/src/styles/app.css` wrote
  `border: var(--eoe-space-1) solid …` / `outline: var(--eoe-space-1) solid …` in four places
  for want of a width token, rendering the sidebar border, `.card` border, and both
  focus-visible outlines at **4px** instead of a hairline. All four now use the new
  `--eoe-border-width-hairline: 1px`.
- **Known gap, deliberately deferred:** `tokens.alt.css` (the night theme) does **not** yet
  mirror the keys `tokens.ext.css` adds. `tests/tokens.test.ts` check 6 still only compares
  `tokens.css` against `tokens.alt.css`, so this is not gate-enforced yet either. Producing
  correct dark-mode status colors requires per-pair contrast verification the way the three
  existing status-aliased colors got (spec'd, not just scaled) — that is real design work, not
  a mechanical follow of this decision, and is out of scope for this batch. Do not assume the
  night theme has a status palette until a follow-up decision closes this gap.
- **Rationale:** Rule R2 requires flagging a change to an E0-owned interface before applying
  it; DES.4-handoff.md was that flag, raised by the DES track. The project owner accepted it
  in this session as part of finishing the DES.4 delivery — additive-only, so every current
  E0 consumer of the five locked namespaces is unaffected and the E0.4 acceptance criteria
  keep holding.
- **Reference:** project-changes #8; project_planning/DES.4-handoff.md; docs/INTERFACES.md "Design
  tokens"; spec sections 9.3, 6.2, 6.5; phase-0-foundations.md section 2 (E0.4).

## D20 (2026-07-24): Verifier cleanup semantics and httpx promotion

- **Decision:** The deployment verifier (`app/verify.py`) deletes the temporary accounts it
  creates via direct database operations (the API deliberately has no user-delete surface,
  spec 13), in FK order: sessions, role assignments, the `totp:{id}` secret row, then the
  user. **Audit rows are never deleted**: the `ondelete=SET NULL` actor FK clears their
  actor reference and the verification trail remains permanently — immutability outranks
  tidiness, and the guide documents this as an implication. `httpx` moves from the dev
  group to main dependencies (the shipped verifier needs it).
- **Rationale:** "Delete the specific account we create" (owner directive) is satisfied at
  the account level while preserving the audit invariant every other part of the platform
  enforces.
- **Reference:** project-changes #7; guide/verify-deployment.md; spec sections 13, 14.1.

## D19 (2026-07-24): Pre-E8 hardening pulled forward by the readiness flight

- **Decision:** Two production-posture fixes land with the E0-R readiness flight rather
  than waiting for E8.7: the API image runs as a fixed non-root user (UID 10001), and the
  compose frontend service now receives `VITE_API_BASE_URL` (default
  `http://localhost:8000`, overridable via `EOE_FRONTEND_API_URL`).
- **Rationale:** Owner directive to verify a production-poised platform. The missing
  frontend env var was a genuine defect: inside the compose stack the browser app could
  never reach the API (only the Playwright config set the variable, out-of-band). Root
  containers are a needless posture risk with a two-line fix.
- **Reference:** project-changes #6; E8.7 still performs the full security review.

## D18 (2026-07-24): Secret scan covers untracked files; fixture credentials are generated

- **Decision:** Two changes after CI (correctly) went red on the E0.6 push while the local
  gate had passed. (1) Test fixture credentials are generated per run
  (`PASSWORD = f"pw-{uuid4().hex}"`), never committed as literals; the scanner's flag on
  `correct-horse-battery` was upheld, not allowlisted. (2) The secret scan now walks
  `git ls-files --cached --others --exclude-standard`, so untracked files are covered at
  the gate that introduces them instead of only after their close-out commit.
- **Rationale:** Rule R0 requires recording test changes at a red gate; both changes
  strengthen the check. The local/CI divergence existed because gates run before the
  close-out commit while CI runs after it: new files were invisible to a tracked-only scan
  locally.
- **Reference:** rule R2 (secrets never in fixtures); CI run on `e0-batch-3` at gate-6;
  backend/tests/test_repo_layout.py, backend/tests/test_auth.py.

## D17 (2026-07-24): Branch protection pending repository-owner action — RESOLVED 2026-07-30

- **Decision:** The API attempt to require the `ci-green` status check on `main` returned
  404 (GitHub's masking of missing admin rights; the working account has WRITE). The
  pipeline is fully functional without it; hard merge-blocking waits on the repo owner.
- **Verified empirically (same day, after E0.12):** `main` reports `protected: false`; the
  working account's permissions are `admin: false, maintain: false, push: true`; and a
  scratch draft PR (#4, since closed, branch deleted) with deliberately red checks —
  `backend-quality`, `backend-tests`, and `ci-green` all FAILURE — reported
  `mergeStateStatus: UNSTABLE, mergeable: MERGEABLE`. **GitHub would currently allow a red
  PR to merge.** Detection works end to end; enforcement is the single missing piece and it
  is exactly the one-checkbox owner action below. Until it is applied, merge discipline is
  procedural (rule R3: never merge a red PR).
- **Action for the repository owner (HudsonReynolds2):** Settings → Branches → Add branch
  protection rule → branch pattern `main` → enable "Require status checks to pass before
  merging" → select **`ci-green`** (only this one; it fans in every stage, so newly added
  stages block automatically without touching settings again). Optionally also enable
  "Require a pull request before merging".
- **Resolved (2026-07-30; recorded 2026-08-01):** The repository owner applied a
  "protect-main" ruleset requiring the `ci-green` check. Verification PR #5
  (`test/ci-gate-verification`, closed, branch deleted) confirmed enforcement in both
  directions — merge blocked while `ci-green` was red, unblocked once green — after one
  correction: the ruleset initially named the check `CI / ci-green` (the
  workflow-qualified display name), which never matches; it was corrected to plain
  `ci-green`. Re-verified 2026-08-01 from this machine: `main` reports `protected: true`,
  and PRs #6 and #7 merged through the required check. GitHub-side merge-blocking is
  active; rule R3's procedural discipline is no longer the only guard. (The protection
  API still returns 404 for the working account — reading ruleset config needs admin.)
- **Reference:** phase-0-foundations.md section 4 (E0.5 acceptance, "a failing test blocks
  merge"); docs/INTERFACES.md "CI pipeline"; PR #5 closing comment.

## D16 (2026-07-24): Line endings pinned to LF via .gitattributes

- **Decision:** `.gitattributes` pins every text file to LF in the repository and the
  working tree on all platforms (`* text=auto eol=lf`), with CRLF only for `*.ps1`/`*.bat`
  and binary patterns exempted. History renormalized with `git add --renormalize`.
- **Rationale:** Gate 5 went red when a branch switch on Windows (core.autocrlf=true)
  smudged CRLF into the working tree and Prettier correctly flagged every file. Without the
  pin, formatting checks disagree between Windows checkouts and the LF-native CI runners,
  making the pipeline flaky by construction.
- **Reference:** rule R0 on_failure; Gate 5 first run log; task E0.5.

## D15 (2026-07-24): CI shape, single workflow over a stage registry with a fan-in check

- **Decision:** One workflow (`.github/workflows/ci.yml`) whose jobs each invoke a single
  stage from the canonical registry in `gate.sh`, ending in a `ci-green` fan-in job that is
  the sole required status check. Everything runs on every push with a per-ref concurrency
  cancel; no path filters. Docker layer caching for the containers job and path filtering
  are recorded future optimizations, deliberately not built now.
- **Rationale:** The registry gives zero drift between CI and the local gate (same shell
  functions execute in both), which is what keeps the pipeline honest as later epics add
  suites (sim-protocol, controlplane-integration). The fan-in gives branch protection one
  stable check name so adding a stage never requires touching repository settings. Full runs
  on every push favor correctness over minutes at the current scale.
- **Reference:** phase-0-foundations.md section 4 (E0.5); docs/INTERFACES.md "CI pipeline";
  closes D9's deferral (the literal alembic reversibility commands now run in CI as the
  `migrations` job).

## D14 (2026-07-24): Test fix at Gate 3, prefix discipline asserted through the public surface

- **Decision:** The prefix-discipline test reads the OpenAPI schema (every documented path
  starts with `/api/v1`, health present) and behaviorally proves nothing serves outside the
  prefix (`/` and `/health` return 404). It does not walk router internals. Invariant
  unchanged.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. Two attempts at
  walking `app.routes` failed against current FastAPI, which represents included routers as
  lazy pathless containers and applies prefixes at match time, leaving route objects with
  unprefixed paths. The public surface (schema plus observable behavior) is the stable,
  version-proof thing to assert.
- **Reference:** rule R0 on_failure; Gate 3 first and second run logs.

## D13 (2026-07-23): Ephemeral test Postgres via direct docker run, not the testcontainers library

- **Decision:** The migration suite starts its ephemeral Postgres with a direct `docker run`
  through the already-proven `docker_cli()`/`docker_env()` helpers (port 54329, random
  password, forced removal on teardown) instead of the `testcontainers` library D6 named.
- **Rationale:** testcontainers-python reaches the daemon through docker-py, whose Windows
  named-pipe transport adds a pywin32 dependency and a second connection path to debug. The
  direct approach reuses one code path for all Docker interaction and gives the same
  guarantee: a real, disposable Postgres per test module.
- **Reference:** amends D6; docs/migration-conventions.md; backend/tests/test_migrations.py.

## D12 (2026-07-23): Test fix at Gate 1, docker CLI directory appended to subprocess PATH

- **Decision:** Integration-test helpers append the docker CLI's own directory to the
  subprocess PATH (`docker_env()` in `backend/tests/test_repo_layout.py`). Assertion logic
  unchanged.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. Docker Desktop's
  `credsStore` invokes `docker-credential-desktop` from PATH; a shell environment captured
  before the install cannot resolve it, failing every image pull with "error getting
  credentials" even though the daemon runs fine.
- **Reference:** rule R0 on_failure; Gate 1 first run log.

## D11 (2026-07-23): Gate enforcement lives in a runner wrapper, not the conftest hook

- **Decision:** The R0 hard-failure on skipped/xfailed/deselected tests is enforced by
  `backend/tests/gate_runner.py`, which the gate scripts invoke; it runs `pytest.main()` with
  no filter arguments, reads the counts through a plugin object, and controls the process exit
  code itself, failing closed if the counts cannot be read. The conftest hook keeps a loud
  advisory print for plain `pytest` runs.
- **Rationale:** Manual verification at Gate 0 showed pytest 9.1.1 ignores mutation of
  `session.exitstatus` inside `pytest_sessionfinish`: the violation printed but the run exited
  0, which would have made the R0 guard silently decorative.
- **Reference:** rule R0; plan section A3; Gate 0 manual verification.

## D10 (2026-07-23): Test fix at Gate 0, explicit UTF-8 subprocess decoding

- **Decision:** The Gate 0 test helper running git subprocesses decodes output with
  `encoding="utf-8"` explicitly (shared `run_git` in `backend/tests/conftest.py`) instead of
  `text=True`, which on Windows decodes with the ANSI code page (cp1252) and crashed on the
  spec's UTF-8 architecture diagram. Assertion logic unchanged.
- **Rationale:** Rule R0 requires recording any test fix made at a red gate. This was a
  platform-dependent defect in test infrastructure, not a weakening: the affected test now
  actually executes its comparison on all platforms.
- **Reference:** rule R0 on_failure; Gate 0 run log (project-updates entry for E0.0).

## D9 (2026-07-23): "In CI" acceptance criteria deferred to E0.5

- **Decision:** E0.2's `upgrade head` / `downgrade -1` checks and the container builds are
  verified locally at the gates; wiring the identical commands into GitHub Actions is E0.5's
  job and E0.5 is outside the current batch (E0.0 through E0.4).
- **Rationale:** The batch boundary was set by the project owner. Recording the deferral keeps
  it visible rather than looking like a missed acceptance criterion.
- **Reference:** phase-0-foundations.md section 4, E0.2 and E0.5.

## D8 (2026-07-23): Error code vocabulary

- **Decision:** Error envelope `code` values are snake_case, stable, and never renamed:
  `validation_error`, `unauthorized`, `forbidden`, `not_found`, `method_not_allowed`,
  `conflict`, `internal_error`. The vocabulary may extend; existing codes never change.
- **Rationale:** The phase document fixes the envelope shape but not the `code` vocabulary.
  Fixing it now prevents drift across seven later epics.
- **Reference:** phase-0-foundations.md section 2 (API conventions); spec section 13.

## D7 (2026-07-23): List response envelope and sort grammar

- **Decision:** All list endpoints respond with
  `{"items": [...], "total": int, "limit": int, "offset": int}`. Sort syntax: `sort=name`
  ascending, `sort=-created_at` descending, comma-separated for multi-key. Binding on E1
  through E7.
- **Rationale:** The phase document fixes the request params (`limit`, `offset`, `sort`) but
  not the response shape or sort grammar.
- **Reference:** phase-0-foundations.md section 2 (API conventions); spec section 13.

## D6 (2026-07-23): Toolchain

- **Decision:** Backend: `uv` with `pyproject.toml`, `ruff` (lint and format), `mypy`,
  `pytest`, `testcontainers` for a real ephemeral Postgres. Frontend: `vitest` with React
  Testing Library and `msw`, ESLint and Prettier, `tsc --noEmit`, and a small `playwright`
  suite for real-browser checks.
- **Rationale:** Playwright is required, not optional: E0.4's acceptance criterion (swapping
  token values visibly restyles the shell) needs real computed styles, which jsdom cannot
  resolve for CSS custom properties.
- **Reference:** phase-0-foundations.md sections 2 and 4 (E0.4).

## D5 (2026-07-23): Config file format is TOML

- **Decision:** The optional settings file (spec section 15.3) is TOML, read with stdlib
  `tomllib`. Environment variables override file values; file values override defaults.
- **Rationale:** Zero added dependency on Python 3.12; the precedence rule is the phase
  document's own acceptance criterion.
- **Reference:** phase-0-foundations.md section 4 (E0.3); spec section 15.3.

## D4 (2026-07-23): CSRF via double-submit token

- **Decision:** Sessions ride an `HttpOnly; SameSite=Lax` cookie (Secure except on plain-HTTP
  localhost) plus a double-submit CSRF token. The middleware hook point and `EOE_CORS_ORIGINS`
  setting land in E0.3; token issuance and validation land with E0.6.
- **Rationale:** D2 makes every browser request cross-origin, and cookie auth cross-origin
  requires CSRF protection. No planning document mentions CSRF anywhere.
- **Reference:** phase-0-foundations.md section 4 (E0.6); spec section 14.1.

## D3 (2026-07-23): Audit immutability enforced at app level plus DB grants

- **Decision:** No ORM or endpoint update/delete path for `audit_log`, plus a reversible
  migration that REVOKEs UPDATE and DELETE on the table from the application role.
  Implemented in E0.8 (not the current batch).
- **Rationale:** Meets the phase document's stated bar and survives a future session that
  forgets the invariant. An intentional strengthening, recorded here.
- **Reference:** phase-0-foundations.md section 4 (E0.8); spec sections 14.1 and 13.

## D2 (2026-07-23): Fully decoupled frontend

- **Decision:** The frontend is its own container with its own Dockerfile, tests, lint, and
  typecheck. The API never serves frontend assets in any environment. Production frontend is
  an nginx-served static build (CDN-ready for E8). No Vite dev proxy. The API base URL comes
  from `VITE_API_BASE_URL`. MSW mocks the API for frontend dev and tests. The only coupling
  is the OpenAPI contract.
- **Rationale:** Project owner requirement: the frontend must be as decoupled as possible for
  parallel development and management at scale. No dev proxy means cross-origin behavior is
  exercised from day one instead of appearing first in production.
- **Reference:** phase-0-foundations.md section 4 (E0.1, E0.4); spec sections 15.1 and 3.2.

## D1 (2026-07-23): DB-backed session rows

- **Decision:** Sessions are rows in a `session` table (id, user_id, created_at, expires_at,
  revoked_at, user_agent, ip); the cookie carries a signed opaque session id. Implemented in
  E0.6 (not the current batch); recorded now so E0.2's baseline and INTERFACES.md anticipate it.
- **Rationale:** Makes `POST /auth/logout` actually revoke, and lets E0.9 invalidate a
  deactivated user's sessions immediately. Still satisfies "signed expiring session tokens".
- **Reference:** phase-0-foundations.md section 4 (E0.6); spec section 12.2.
