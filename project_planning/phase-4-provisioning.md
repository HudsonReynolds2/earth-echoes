# Phase 4 Document: Provisioning Tool (Epic E4)

**Companion documents:** Technical Specification v1.1 (authoritative), Project Development Plan v1.0
**Spec sections implemented:** 8, and 16.4's bootstrap block only
**Depends on:** E0, E1, E2 and E3 complete and merged (gates 0-51). E4.6 has a cross-epic
dependency on E5.6 which this document resolves with a flag and a provider seam (section 2).

---

## 1. Scope

Build the provisioning tool: the bundle and record data model, the versioned on-card config
file format, per-pod and per-listener generation with mixed mode, the device-facing envelope
encryption that lets one firmware binary serve an entire Organization, the Aggregator's
`settings.yaml` with its bootstrap block, the manifest and README, the export archive and its
download, the registration matching that moves a record from generated to confirmed, the
wizard and tracking board, and the fill-in-later flow for name and GPS.

When this phase ends, an operator picks a Pod, picks a mode, generates a bundle, and downloads
an archive a field tech can write to a stack of microSD cards. As those cards go into devices
and the devices reach the broker, the tracking board fills in by itself: `generated` →
`flashed` → `registered` → `confirmed`, or `mismatch` when a device disagrees with what was
prepared for it.

**The manifest is one of the four test-critical components** (spec 14.5, alongside the merge
engine, the reconciliation state machine, and the RBAC checks). From the moment E4.7 lands, its
suite is locked documentation under rule R0: later sessions extend it and never weaken it. The
reason is worth stating, because it is not obvious — the manifest is the only artifact that
says what SHOULD be on a card, and a bundle whose manifest disagrees with its own contents is
undetectable in the field, where the card is already written and the device is already in a
tree.

**This phase writes files, never cards** (spec 8.1). The browser does not touch hardware; a
person loads the exported files. Every design question that starts "could the platform flash…"
is answered no by the spec, not by this document.

## 2. Prerequisites and inherited interfaces

Read `docs/INTERFACES.md` first. What this phase consumes, and does not redefine:

**`effective_resolved(db, entity_type, entity_id, secret_store)`** (`app/config/service.py`,
E2.3) — the plaintext accessor. `INTERFACES.md` already names this phase as one of its two
legitimate callers: "INTERNAL ONLY: E3's publisher and **E4's bundle generator**; wiring it
into an HTTP response is a security defect." This is how the generator obtains
`network.wifi_password` and `network.stream_key`. The other two accessors are wrong here:
`effective_for` returns redacted markers and `effective_raw` returns markers verbatim. A
generated card carrying the string `__secret__` instead of a PSK is a bundle that boots
nothing, so the choice of accessor is load-bearing and belongs in a code comment.

**`SecretStore`** (`app/secrets.py`, E0.11). The `bundle:{id}:{key}` namespace is already
reserved for this phase. **The two encryption schemes nest, they do not compete:** SecretStore
protects a secret at rest inside Postgres under the platform KEK; the spec 8.4 firmware
envelope protects the same secret in transit to a device under the firmware KEK. A value
leaves SecretStore as plaintext for exactly as long as it takes to re-encrypt it under the
DEK, in memory, inside the generator.

**The settings catalog** (`app/config/catalog.py`, E2.1) — the source of every key a generated
file may contain. The three flags this phase reads:

- `secret=True` on `network.wifi_password` and `network.stream_key` — the two device-facing
  secrets the firmware envelope covers. The other four secret keys (`upload.s3_*`,
  `telemetry.influx_token`, `telemetry.prom_remote_write_password`) are Aggregator-side and
  reach devices only as retained desired config after first connect (spec 16.4). **They never
  enter a card file.**
- `resolution="inventory"` on `location.gps_lat`, `location.gps_lon`, `identity.name`,
  `identity.mac` — read from listener columns, never overridable. This is exactly why spec
  8.2's "unique per-listener data is left blank on the card and filled in later in the cloud"
  needs no new machinery: the blanks are a catalog property this phase reads, not a list this
  phase maintains.
- `lowest_level="pod"` on the `network.*` keys — the reason a per-pod file is coherent at all.

**`config_revision`** (E2.6) — the revision a bundle embeds, and its D52 `checksum`, which is
what `confirmed` compares against. **`state` belongs to E3's machine; this phase never writes
it** and never creates revisions of its own: a bundle records the revision that was current
when it was generated.

**`device_state`, `device_event`, `quarantined_report`, `inventory_alert`** (E3.5, E1.5) —
E4.9's evidence. Read, never written. `device_state` holds one row per device replaced in
place, keyed `(entity_type, entity_id)` with `entity_id` being the aggregator platform UUID or
the listener MAC — the same convention `config_revision` uses (D75).

**`aggregator.aggregator_uuid`** (E1.1) — **already platform-generated, at inventory create
time.** `INTERFACES.md` on bulk import: "blank `aggregator_uuid` is platform-generated (spec
4.2)". The project plan's E4.6 line reads "`aggregator_uuid` generation and inventory
recording", and that half of it is already done. E4.6 **embeds the existing value and records
it on the bundle**. Do not build a second minting path; an Aggregator whose UUID changed
between inventory creation and bundle generation would break the join key spec 4.2 exists to
protect.

**`deployment_service`** (E3.1) — the broker row: `host`, `port`, `tls_enabled`, `ca_cert_pem`,
`username`, `password_secret_name`, `service_key` currently CHECK-constrained to `('mqtt')`.
This phase reads it for the bootstrap block's endpoint and TLS expectation. **E5 owns extending
this table**; this phase adds no column to it.

**RBAC** (E0.7) — `Permission.MANAGE_PROVISIONING` and `Permission.VIEW_PROVISIONING` already
exist in `app/auth/rbac.py` and are mirrored in `frontend/src/lib/rbac.ts`. Owner, deployment
operator and field tech hold manage; viewer holds view. **This phase is the first consumer of
verbs E0.7 defined for it and changes neither map.** A field tech's whole role is this epic.

**Frontend** (E0.4, E1.8, DES.7) — `frontend/src/pages/Provisioning.tsx` is the deliberately
empty `EmptyState` placeholder (project-changes #9), and replacing it is E4.10's job.
`.data-table`, `.form`/`.form-field`, `.btn-*`, `.modal*`, `.outcome-*`, `Can`/`useCan` are
E1's vocabulary; extend them rather than starting a second one. Layout for the tracking board
comes from **S6 in `Screens.dc.html`** ("Provisioning — bundle tracking board (Field Tech)",
labelled E4.10 · spec §8.5); the generation wizard is not drawn, so it is assembled from
DES.5's wizard-step components. v1 holds the layout, the current token sheets hold the values —
**v2 wins on every value** (DES handoff).

**The demo fixture** (E1.9) and the SIM fleet (SIM.1-SIM.4) are what this phase demonstrates
against. E4.9's end-to-end acceptance drives a SIM mock Aggregator; if SIM has not landed when
E4.9 does, use E3's in-test mock fixtures and say so in the ledger rather than growing a third
mock.

### Fixed choices for this phase

These four were decided by the owner at plan approval. They resolve three open spec items and
one cross-epic dependency. **They are not to be relitigated mid-epic**; a task that appears to
need one reopened is a stop-and-ask.

1. **E4.6 ships behind a flag, on a provider seam** (project plan section 3's own contingency,
   handbook section 3's named case). All tasks ship in this phase. `settings.yaml` is generated
   structurally complete — `aggregator_uuid`, broker host and port, TLS expectation, and the
   device's own broker username and password — with the credential pair coming from a
   `BrokerCredentialProvider` interface. This phase implements exactly one provider, reading
   E3's devbroker dev accounts (`app/devbroker.py`: `plan_accounts`, `device_username`,
   `load_manifest`). `EOE_BOOTSTRAP_CREDENTIALS` (Settings field `bootstrap_credentials`)
   **defaults off**: with it off the block is written with the credential fields absent and
   both the README and the manifest say the device cannot reach the broker until they are
   filled; with it on, the provider's credentials are embedded. **E5.6's entire job is to add a
   dynsec provider and flip the default** — not to restructure this task.
   Spec 16.5 gates generation on a verified broker; verification status is E5.5's and does not
   exist yet, so the gate degrades to **"a `deployment_service` row with `service_key='mqtt'`
   exists for this deployment"**, 422 otherwise, at one call site carrying an explicit
   `# E5.5 replaces this predicate` marker.

   > **Addendum PHASE4-2-01 (2026-08-11, ref project-changes #24):** This choice assumed E4
   > would land before E5. It did not — E5 was built first, so **the direction of this
   > dependency has reversed and three sentences above are now stale.** `BrokerCredentialProvider`
   > is **defined by E5.6**, in `backend/app/services/credentials.py`, together with
   > `DynsecCredentialProvider` and `DevBrokerCredentialProvider`. E4.6 therefore **imports the
   > protocol rather than declaring it**, and does not write a dev provider of its own —
   > `DevBrokerCredentialProvider` is exactly the one this task described. E4.6's remaining work
   > is what this choice always said it would leave E5: choose a provider and flip
   > `EOE_BOOTSTRAP_CREDENTIALS`. Two further consequences: **dynsec is required for v1** (spec
   > 17 item 14, closed — DECISIONS D104), so there is no manual-install path and no held-bundle
   > state for E4 to consult; and the degraded verified-broker predicate above is **no longer
   > needed**, because E5.5 shipped `deployment.services_status` — E4.6 gates on
   > `services_status == 'verified'` directly and the `# E5.5 replaces this predicate` marker
   > should never be written. See DECISIONS D105.

2. **E4.2 authors `device_config` v1 as YAML** (spec 17 item 1). No firmware-agreed schema
   exists, so this phase writes the draft: a versioned template carrying `schema_version: 1`,
   built from the spec 5.3 keys that spec 5.4 permits on a card. It is marked **DRAFT pending
   firmware sign-off** in three places that a reader cannot miss — a comment in the generated
   file itself, the `DECISIONS.md` entry, and the bundle's README. YAML because the Aggregator's
   `settings.yaml` is YAML by spec 8.3 and a field tech should read one format, not two. The
   template is versioned precisely so that the firmware-agreed schema arrives as an additive
   v2 renderer rather than a rewrite of everything downstream of it.

3. **E4.5 uses an env-provided firmware KEK and AES-256-GCM throughout** (spec 17 item 7).
   `EOE_FIRMWARE_KEK`, base64 of exactly 32 bytes, validated fail-loud at app construction
   exactly as `EOE_KEK` is. A fresh per-Pod DEK per export; the DEK wrapped with the KEK under
   AES-256-GCM; the PSK and stream key encrypted with the DEK under AES-256-GCM. Wrapped DEK
   and ciphertexts are base64 in the pod file with a `key_version` field. One algorithm, which
   ESP32 does in hardware through mbedTLS, and which is the scheme E0.11 already proved in this
   repository. **KEK custody remains open** — spec 17 item 7 gates production use, not dev
   implementation, and this document says so rather than implying the question is closed.
   Losing this key strands the ability to provision new Pods across the whole Organization;
   that risk is recorded, not solved, here.

4. **E4.8 ships a plain `.zip`.** Every secret inside is already firmware ciphertext (choice 3),
   so archive-level encryption would reintroduce exactly the operator-carried passphrase that
   spec 17 item 7 deleted when it replaced bundle key escrow with firmware-side envelope
   encryption. Spec section 13's "(encrypted archive)" wording predates that change. E4.0 files
   the project-changes entry and appends the spec addendum, which records archive-level
   encryption as **deferred — revisit only if a future requirement asks for it.** Zip rather
   than tar.gz because cards get written on Windows field laptops.

Plus two structural choices, which are this document's rather than the owner's:

- **Location.** `backend/app/provisioning/` holds the generator, the templates, the firmware
  envelope, and the record reconciler; routes live at `backend/app/api/provisioning.py`. The
  frontend client module is `frontend/src/lib/provisioning.ts`, following
  `src/lib/inventory.ts`'s shape (typed `ApiError`, one function per call, flat query keys).
- **E4.9 reads E3's tables; it does not edit `consumer.py`.** The reported consumer is a
  published contract (D76-D79) with a locked suite, and its identity handling is deliberate to
  the point of being delicate. A provisioning reconciler that re-derives record state from
  `device_state`, `config_revision` and `quarantined_report` gets the same answer, is
  restart-safe and idempotent by construction, and costs E3 nothing. **A task that appears to
  need a `consumer.py` edit is a stop-and-ask** (rule R2).

## 3. Out of scope

Per-device broker credential minting and the Mosquitto dynsec path, including the manual-install
fallback and its held-bundle state (**E5.6**; this phase ships the provider interface and one
dev implementation). Service connection tests, the testers, `services_status` and its lifecycle,
the generated stack, and the onboarding wizard (**E5**). Extending `deployment_service` with
non-MQTT rows (**E5.1**). Secret rotation over the local link (**spec 8.7**; this phase ships
the per-Pod DEK structure that makes rotation possible and no push mechanism — the Aggregator
pushes, and nothing in this epic talks to a Listener). Firmware KEK custody, backup, and
org-wide KEK rotation (**spec 17 items 7 and 12**; E8.1 owns the secret-manager backend behind
the same interface). Map rendering of provisioning state (**E6**). Alert surfacing beyond the
`inventory_alert` rows E1.5 already opens (**E7**). Telemetry of any kind (**E7**). Anything
that writes to a card, and any browser-side hardware access (**spec 8.1** — a person loads the
files). Live-GPS ingestion (**spec 17 item 6**; spec 8.6 scopes v1 to the manual fill path
only). Any edit to `app/contracts/mqtt.py`, `app/controlplane/consumer.py`, or the four
test-critical suites (merge engine, revision state machine, RBAC, and — once E4.7 lands — the
manifest).

## 4. Task list

**E4.0 Phase document and records.** This document, `project_planning/e4-progress-ledger.md`,
the `docs/project-changes.md` entry covering both plan deltas (E4.0 itself, absent from the
project plan's eleven-task E4 list, and the four fixed choices), the matching addendum on
project plan section 3, and the spec addendum recording section 13's "(encrypted archive)"
wording as superseded and archive encryption as deferred. Docs only; the gate is a regression
check. *Acceptance:* every fixed choice above appears in `DECISIONS.md` or is scheduled onto
the task that implements it, and the ledger's twelve rows exist.

**E4.1 Bundle and record data model.** `provisioning_bundle` and `device_provisioning_record`
(singular names, D30 convention; named constraints, E0.2 convention). The bundle row carries
its deployment, its pod when pod-scoped, its mode, the generating user, the generation
timestamp, the template version, the firmware key version, and the archive's own checksum. The
record row carries expected MAC or aggregator identity, target slot, the embedded
`config_revision` id, its checksum, the spec 8.5 status, and a status history. **Un-FK'd
evidence columns following the D33/D55 precedent** — a provisioning record is evidence about
what was prepared and must outlive the device it describes, including a device that never
existed in inventory. Statuses `generated`, `flashed`, `registered`, `confirmed`, `mismatch` as
a CHECK constraint. *Acceptance:* the status vocabulary is pinned in a test against a hardcoded
spec 8.5 list, the way the catalog is pinned against spec 5.3; deleting a listener leaves its
records intact and readable.

**E4.2 Versioned device config template.** The `device_config` v1 renderer per fixed choice 2:
a pure function from a resolved effective config plus identity to YAML bytes, emitting only
keys spec 5.4 permits on a card (identity, capture and audio settings, SD buffering, log
verbosity, the Aggregator target and stream endpoint, and the network block), with
`schema_version: 1` and the DRAFT marker. *Acceptance:* golden-file round trip (the renderer's
output is pinned byte for byte, so a change to the on-card format is a deliberate act); every
key it emits exists in the catalog, asserted against `CATALOG` rather than a copied list; **no
`secret=True` key appears in plaintext anywhere in the output**, asserted by scanning the bytes
for the known secret values, not by reading the template.

**E4.3 Per-pod file generation.** Spec 8.2 mode 1: one file for the Pod, written to every card
in it, carrying the shared network configuration and leaving the per-listener identity and
location fields blank for the E4.11 fill-in-later flow. *Acceptance:* the file is byte-identical
for every Listener in the Pod (this is the property that lets a field tech write one file to a
stack of cards, so it is asserted directly); the blank fields are exactly the catalog's
`resolution="inventory"` keys, derived from the catalog and not from a literal list; a Pod
whose `network.*` values are unset fails generation with a message naming the missing keys
rather than emitting a card that cannot join a network.

**E4.4 Per-listener file generation.** Spec 8.2 mode 2: one MAC-keyed file per Listener, a
folder for a Pod or a Deployment, and **mixed mode** — the pod file plus per-listener files for
only the devices that need distinct first-boot settings. *Acceptance:* a mixed-mode bundle
carries the pod file and per-listener files for exactly the named MACs; the manifest names both
kinds and which cards take which; a per-listener file for a MAC outside the selected scope is
rejected at generation, not silently included.

**E4.5 Device-facing envelope encryption.** Fixed choice 3, implemented as
`app/provisioning/envelope.py`: `wrap_dek`, `seal`, and a matching `unseal` used only by tests
and by any future rotation path. The wrapped DEK, the ciphertexts, and `key_version` are
embedded in the pod file per spec 8.4. *Acceptance:* a round trip that decrypts using only the
KEK and the file contents, standing in for what firmware does at boot; flipping any single
ciphertext byte fails GCM authentication rather than yielding garbage; the plaintext PSK is
provably absent from the generated bytes; generating the same Pod twice produces different DEKs
and different ciphertexts (fresh key material per export is what makes per-Pod rotation
meaningful); a missing or malformed `EOE_FIRMWARE_KEK` fails at app construction with a
fingerprint and never a value.

**E4.6 Aggregator `settings.yaml` with bootstrap block.** Fixed choice 1. The block carries the
`aggregator_uuid` read from inventory, the broker endpoint and TLS expectation read from
`deployment_service`, and the per-device credentials from the `BrokerCredentialProvider` —
**plaintext by spec 16.4's deliberate decision**, which the README restates so nobody
"improves" it later. *Acceptance:* both flag states are tested, and with the flag off the file
is still valid YAML that names what is missing; the embedded `aggregator_uuid` equals the
inventory row's, is recorded on the bundle, and is never regenerated; generation for a
deployment with no MQTT `deployment_service` row is refused with a 422 naming the deployment.

**E4.7 Manifest and README generation. TEST-CRITICAL (spec 14.5).** `manifest.json` per spec
8.3: every file in the archive with its path, its target MAC or pod, its checksum, the
generating config revision, and the expected on-device contents; plus the human-readable README
describing what goes where, which flags carry the DRAFT template status, the plaintext
bootstrap block, and any absent credentials. *Acceptance:* the suite is table-driven over every
file kind and both generation modes, and asserts **completeness in both directions** — every
file in the archive appears in the manifest and every manifest entry names a file that is in
the archive. From this task onward the suite is locked documentation under rule R0: extend it,
never weaken it, and record any change to it in `DECISIONS.md`.

**E4.8 Export archive and download.** Archive assembly (plain zip, fixed choice 4) and the spec
13 endpoints: `POST /provisioning/bundles` (generate; body names the target and the mode),
`GET /provisioning/bundles/{id}`, `GET /provisioning/bundles/{id}/download`,
`GET /provisioning/bundles` (D7 list envelope). `MANAGE_PROVISIONING` to generate,
`VIEW_PROVISIONING` to read, every mutation audited through the E0.8 hook, all scoped by
deployment. *Acceptance:* a downloaded archive's manifest checksums verify against its own
members; **a secret-leak scan runs over the whole archive and over every API response in the
suite**, asserting the plaintext PSK and stream key appear in neither (rule R2, spec 14.1);
regenerating a bundle for the same Pod produces a new bundle row rather than mutating the old
one, because the old one describes cards that may already be in the field.

**E4.9 Registration matching and record transitions.** A reconciler in
`app/provisioning/records.py` that re-derives record status from E3's evidence, per the
structural choice above. The rules, which belong in the code as written here:

- `flashed` — operator-set only, via `PATCH /provisioning/records/{id}`. The platform cannot
  observe a card being written.
- `registered` — a `device_state` row exists for the record's expected identity. First contact
  is exactly that row's existence; no new first-seen column is needed.
- `confirmed` — that row's checksum equals the embedded revision's checksum, **and the status
  latches.** A device that later takes new config legitimately must not fall out of confirmed;
  confirmation is a statement about what happened at first boot, not a live comparison.
- `mismatch` — an identity quarantine exists for that MAC (`quarantined_report` with reason
  `mac_conflict`, `name_conflict` or `unknown_mac`), or the device reports a checksum belonging
  to no `config_revision` for that device at all. A checksum belonging to a *later* revision is
  not a mismatch; it is an operator having changed the config, and treating it as a mismatch
  would fill the board with false alarms the first time anyone edits anything.

*Acceptance:* the full journey driven end to end against a SIM mock Aggregator (or E3's in-test
mock if SIM has not landed, noted in the ledger) — generate, mark flashed, bring the device up,
watch registered then confirmed; a device reporting a conflicting MAC lands in `mismatch` with
inventory provably unchanged; the reconciler is idempotent and restart-safe, asserted by running
it twice and diffing the rows.

**E4.10 Provisioning wizard and tracking UI.** The wizard: pick a Pod or Deployment, pick the
mode per Pod (including mixed), review what will be generated, generate, download. The board:
S6's layout at current token values, records grouped by bundle with their statuses, the flashed
action gated by `Can`. Replaces the `Provisioning.tsx` `EmptyState`. **Record status is not
device status** — `StatusChip` renders device states and only device states (D40 and the DES
three-channel rule), so the board uses its own vocabulary following the `.outcome-*` precedent
E1.6 set for import results. *Acceptance:* the E4.9 journey is legible on the board without a
reload path through the API docs; a viewer sees the board and no generate button; every new
class lands in an `app.css` section and every new token is an additive `tokens.ext.css`
extension with its dark value (D21), with `tokens.test.ts` green.

**E4.11 Fill-in-later flow, and the E4 verification walkthrough.** Cloud-side editing of `name`
and GPS for registered Listeners (spec 8.6), writing the inventory columns that the catalog
marks `resolution="inventory"` — through the E1 endpoints, not a second write path — surfaced
from the provisioning board where a blank-card device shows up after registering. Plus
`guide/e4-verification.md` (rule R1): the living acceptance walkthrough for this epic, which
also amends any assertion in `guide/e1-verification.md`, `e2-verification.md` or
`e3-verification.md` that this epic invalidates, in this batch. *Acceptance:* a Listener
provisioned from a per-pod card, registered by MAC with no name or GPS, is given both from the
UI and moves from cluster leaf to map pin's worth of data (E6 renders it; this phase supplies
it); the walkthrough runs start to finish by hand against a clean stack.

## 5. Definition of done

An operator generates a Pod bundle in the UI and downloads an archive containing the pod config
file with firmware-encrypted secrets, the Aggregator's `settings.yaml` with its bootstrap
block, `manifest.json`, and a README; provisioning records track generated through
confirmed/mismatch as devices come online, driven by real reported state and nothing hand-set
except `flashed`. Plus:

- The manifest suite is test-critical, complete in both directions, and locked.
- **No plaintext device secret exists in any generated byte, API response, log line, fixture,
  or committed file** — asserted, not asserted-to.
- `EOE_FIRMWARE_KEK` is required and fail-loud; `EOE_BOOTSTRAP_CREDENTIALS` is off by default
  and both states are tested.
- The bootstrap block is structurally complete behind its flag, and E5.6's remaining work is to
  add one provider implementation and flip one default.
- The on-card template is versioned and visibly DRAFT, so the firmware conversation is a v2
  renderer rather than an archaeology exercise.
- The universal definition of done (handbook section 4): CI green, every mutation audited,
  every endpoint RBAC-gated, `INTERFACES.md` and `DECISIONS.md` updated, the demo fixture still
  seeds, the compose stack still starts clean.

## 6. Handoff artifacts

- `docs/INTERFACES.md` gains an **Owned by E4** section: the two tables and the spec 8.5 status
  vocabulary; the `device_config` v1 template with its DRAFT status and the fact that v2 is
  additive; the firmware envelope format **as a wire contract firmware must parse** (field
  names, base64 encoding, `key_version`, the algorithm) — published the moment it merges, the
  way `contracts/mqtt.py` was; `BrokerCredentialProvider` marked **E5.6 IMPLEMENTS THIS**
  alongside the flag and the degraded verified-broker predicate marked **E5.5 REPLACES THIS**;
  the bundle and record endpoints; and the E4.9 transition rules including the latch and the
  later-revision exception.
- `docs/DECISIONS.md`: the four fixed choices with their rationale and their open remainders
  (spec 17 items 1 and 7 stay open and must say so), plus every deviation found while building.
- `guide/e4-verification.md`, and the amendments this epic forces on earlier walkthroughs.
- `project_planning/e4-progress-ledger.md`, kept current per task — the file a session joining
  mid-epic reads first.
- `deploy/.env.example` and the environment-variable table in `INTERFACES.md`'s **Owned by E0**
  section both gain `EOE_FIRMWARE_KEK` (required) and `EOE_BOOTSTRAP_CREDENTIALS` (optional,
  default off) — by name, never by value. That table is E0's and this is an additive row, not a
  change to anything it already documents.
