# Echoes of Earth Management Platform: Technical Specification

**Version:** 1.1 (amended baseline for phased implementation)
**Date:** 2026-07-11
**Status:** Approved for phase breakdown
**Document owner:** Hudson Reynolds

**Changes in 1.1:** Adds Section 16 (deployment services onboarding): a guided flow that connects or generates each deployment's service stack (Mosquitto, InfluxDB 3, Prometheus, Grafana, object storage), tests the connections, and delivers configuration to Aggregators through a plaintext bootstrap block in `settings.yaml` plus post-connect delivery over the MQTT control plane. Adds a fourth primary goal (1.2), deployment service settings to the catalog (5.3), the bootstrap block to provisioning (8.2, 8.3), deployment services API endpoints (13), and design-token theming for the frontend (3.2). Renumbers old Sections 16 and 17 to 17 and 18, adds open issues 13 and 14, and inserts a new Phase 5 into the phase breakdown.

---

## 0. How to read this document

This specification defines a web platform for deployment configuration, remote monitoring, and remote reconfiguration of the Echoes of Earth bioacoustic monitoring system. It is the single source of truth that later phase instruction documents draw from. Each major section maps cleanly to one or more implementation phases, and Section 18 proposes that phase split.

The document uses active voice and avoids em dashes by preference. Conventions: MUST and SHOULD carry their RFC 2119 meaning. Code identifiers appear in `monospace`. The word "device" without qualification means any node in the hierarchy.

---

## 1. System overview and goals

### 1.1 What the platform manages

Echoes of Earth captures field audio for conservation and soundscaping. The platform manages a four-level device hierarchy and the data and configuration that flow through it. Listeners capture audio and stream it plus statistics to their Aggregator. Aggregators run edge AI analysis, publish metrics to Prometheus, write analysis to InfluxDB 3, and upload raw audio to S3. A Pod is one Aggregator plus the Listeners that stream to it, the point of the extended star. The Aggregator connects by Ethernet to the Pod's HaLow router, and the Listeners connect wirelessly to that router, so the Listeners in a Pod share one network configuration. Deployments group Pods around one shared telemetry stack. An Organization owns everything and sits at the top.

### 1.2 Primary goals

The platform delivers four capabilities to stakeholders of every size, from a single field team to a global operator overseeing petabytes.

1. **Deployment configuration.** Operators prepare a deployment before hardware ships. The platform generates one shared config file per Pod, which a field tech writes identically to every microSD card in that Pod, and it can also generate per-listener files where specific devices need them. It exports these for loading onto the cards and tracks which configuration belongs where. Field staff fill in remaining per-listener details (GPS, names) later from the cloud, matched by MAC.
2. **Remote monitoring.** Operators see device status, alerts, and telemetry on a map and in tables, from a global summary down to a single Listener.
3. **Remote reconfiguration.** Operators change settings at any level of the hierarchy, the platform pushes desired state to devices, and devices report back until desired and reported state converge.
4. **Deployment services onboarding.** Operators stand up or connect the deployment's service stack (MQTT broker, InfluxDB 3, Prometheus, Grafana, object storage) through a guided flow. The platform verifies every connection, stores credentials encrypted, and delivers what each Aggregator needs through the provisioning bundle and the control plane (Section 16).

### 1.3 Design principles

The platform runs both self-hosted on a single machine and on major cloud infrastructure, and it ships self-hosted (Docker Compose) first. It stays secure, scalable, reliable, performant, and maintainable. It integrates with the services the system already runs (InfluxDB 3, Grafana, Prometheus, S3, BalenaCloud) rather than replacing them. It avoids hard dependence on any one external platform, in particular it does not build its control plane on Balena.

### 1.4 Explicit non-goals for v1

The platform does not re-implement Grafana dashboards natively, does not run the edge AI models, does not transport raw audio, does not provide a global telemetry aggregation layer (it queries each deployment directly), and does not write microSD cards from the browser. It does not handle live-GPS device tracking as a built feature in v1, though the data model reserves room for it. The platform does not host or run the per-deployment services itself; it connects to services the operator supplies, or generates a ready-to-run stack the operator launches on their own host (Section 16), and in v1 it does not provision cloud VMs to run that stack (Section 17, item 13 tracks Chameleon auto-provisioning as future work).

---

## 2. Glossary

This section defines terms used throughout, including ones flagged as unclear during planning.

- **Organization:** The top of the hierarchy and the unit of ownership. v1 runs a single Organization. The schema supports more later.
- **Deployment:** A group of Pods that share one telemetry stack (InfluxDB 3, Grafana, Prometheus, MQTT broker) on one KVM host.
- **KVM host:** The virtualization host that runs a deployment's shared services.
- **Pod:** One Aggregator and the Listeners that stream to it, forming one arm of the extended star. The Aggregator ethernets into the Pod's HaLow router, and the Listeners join that router wirelessly and share one network configuration (WiFi/HaLow credentials plus the target to reach the Aggregator). A Pod has exactly one Aggregator.
- **HaLow router:** The 802.11ah access point in a Pod. The Aggregator connects to it by Ethernet, and the Listeners connect to it wirelessly.
- **Aggregator:** A Raspberry Pi running balenaOS. It receives audio streams from its Listeners, runs edge AI, and ships analysis, metrics, and raw audio to the deployment's services.
- **Listener:** An ESP32-S3 device that captures audio and streams it plus statistics to its Aggregator. Identified immutably by MAC address.
- **Desired state:** The configuration the operator wants a device to have, stored in the platform.
- **Reported state:** The configuration a device says it currently has, sent back by the device.
- **Reconciliation:** The process of driving reported state to match desired state and flagging divergence (drift).
- **RBAC (role-based access control):** An authorization model where each user holds a role, and each role grants a fixed set of permissions.
- **OIDC (OpenID Connect):** A standard that lets users log in through an external identity provider (for example Google, Microsoft, Okta, Keycloak, Authentik) instead of a password held by this app. v1 does not require it but the auth layer leaves room for it.
- **MQTT:** A lightweight publish/subscribe messaging protocol built for intermittently connected devices. The platform uses it as the device control plane.
- **Retained message:** An MQTT feature where the broker keeps the last message on a topic and delivers it immediately to any client that subscribes later. The platform uses this to deliver desired config to reconnecting Aggregators.
- **LWT (Last Will and Testament):** An MQTT feature where the broker publishes a predefined message when a client disconnects unexpectedly. The platform uses this for offline detection.
- **Envelope encryption:** A scheme where a data encryption key encrypts the secret, and a separate key encryption key encrypts the data encryption key. It lets the platform rotate keys and store secrets safely.
- **Drift:** A state where a device's reported configuration diverges from its desired configuration.
- **Deployment services:** The per-deployment stack a deployment depends on: the Mosquitto broker, InfluxDB 3, Prometheus, Grafana, and object storage (S3 or MinIO), normally running on the deployment's KVM host.
- **Bootstrap block:** The minimal plaintext section of an Aggregator's `settings.yaml` (identity plus broker access) that lets the device reach the control plane. Everything else arrives over MQTT after first connect (Section 16.4).
- **dynsec:** Mosquitto's dynamic security plugin. It lets an authorized client create and manage broker credentials and topic ACLs at runtime over MQTT control topics, which the platform uses to mint per-device broker credentials without touching the broker host.

---

## 3. Architecture overview

### 3.1 Component map

```
                         ┌─────────────────────────────────────────┐
                         │            Web Platform (this spec)       │
                         │  ┌──────────┐  ┌──────────┐  ┌─────────┐  │
   Browser  ◄──────────► │  │ Frontend │  │   API    │  │ Workers │  │
   (operator)            │  │ React/TS │  │ FastAPI  │  │ (async) │  │
                         │  └──────────┘  └────┬─────┘  └────┬────┘  │
                         │                     │             │       │
                         │             ┌───────┴──────┐      │       │
                         │             │  PostgreSQL  │      │       │
                         │             │ (inventory,  │      │       │
                         │             │  config,     │      │       │
                         │             │  audit, etc) │      │       │
                         │             └──────────────┘      │       │
                         │             ┌──────────────┐      │       │
                         │             │ Redis (opt.) │◄─────┘       │
                         │             └──────────────┘              │
                         └───────────┬──────────────────┬───────────┘
                                     │                  │
              per-deployment reads   │                  │  device control plane
              (queries, embeds)      │                  │  (MQTT)
                                     ▼                  ▼
   ┌───────────────────────────────────────┐   ┌────────────────────────┐
   │  Deployment KVM host (one per deploy)  │   │  MQTT broker (Mosquitto)│
   │  InfluxDB 3 · Grafana · Prometheus     │   │  on the same KVM host   │
   │  (S3 lives in cloud or on-host MinIO)  │   └───────────┬────────────┘
   └───────────────────────────────────────┘               │
                                                  retained desired / reported / status / events
                                                            │
                                              ┌─────────────┴─────────────┐
                                              │   Aggregators (Pi/balenaOS)│
                                              │   each holds + pushes      │
                                              │   listener config locally  │
                                              └─────────────┬─────────────┘
                                                            │ local config push/notify
                                                            ▼
                                                   Listeners (ESP32-S3)
```

### 3.2 Why these technology choices

**Backend: FastAPI (Python).** Pydantic models the typed, inheritable, validated configuration and the desired/reported schemas directly, and serializes cleanly to device config formats. The core workload is async fan-out to many deployment-local services plus an MQTT event stream, which fits async Python well. Every external system here has a first-class Python client (boto3 for S3, the Influx client, aiomqtt or paho for MQTT, REST for Balena and Grafana). The main alternative, Go, offers a single-binary deploy that Docker Compose already neutralizes, at the cost of Pydantic and development velocity.

**Database: PostgreSQL.** It holds the relational hierarchy, the configuration with JSONB for flexible per-level settings, and the reconciliation history and audit log under real transactional guarantees. It scales from a Compose container to managed cloud without code change.

**Frontend: React with TypeScript (Vite).** It carries the deepest ecosystem for the exact widgets this needs: react-leaflet for the map, TanStack Query for data fetching and caching, TanStack Table for device grids. The frontend isolates all theming behind design tokens (CSS variables for color, spacing, typography, and component styling) so the visual design system, selected by a parallel design track in Figma, applies by changing token values without structural rework. Implementation phases build against a neutral default theme until that selection lands, and applying the chosen design system is a discrete task rather than a rewrite.

**Control plane: MQTT (Mosquitto), per deployment.** MQTT targets intermittently connected field devices. Retained messages deliver desired config to reconnecting Aggregators with no polling. LWT gives offline detection automatically. Per-topic ACLs and mTLS are mature. This keeps Balena scoped to OS and container lifecycle only.

**Map: Leaflet, with PMTiles for offline.** Leaflet plus react-leaflet is the widely supported, low-maintenance choice. PMTiles provides a single-file vector basemap for air-gapped local hosting with no tile server to operate.

**Optional Redis.** Used only inside the backend for caching fanned-out telemetry, websocket live-update fan-out, and job queues. The simplest self-hosted deploy omits it.

### 3.3 Data and control flow separation

The platform separates two planes cleanly. The **read/telemetry plane** queries each deployment's InfluxDB 3, Prometheus, and Grafana, and renders summaries natively or embeds Grafana panels for depth. The **control plane** uses MQTT to push desired configuration and consume reported state and events. Postgres-owned data (inventory, config, reconciliation, audit, alerts) renders natively. Influx and Prometheus data render through Grafana embeds.

---

## 4. Domain model and hierarchy

### 4.1 Entity hierarchy

```
Organization
└── Deployment            (owns one telemetry stack + one MQTT broker on its KVM host)
    └── Pod               (one Aggregator + its Listeners; one arm of the star, around a HaLow router)
        └── Aggregator    (exactly one per Pod; Raspberry Pi, balenaOS; Ethernet to the HaLow router; runs edge AI)
            └── Listener  (up to ~50; ESP32-S3; HaLow wireless to the router, streams to the Aggregator; immutable MAC)
```

### 4.2 Identity and keys

The Listener MAC address is the immutable primary identity for a Listener across the whole platform, because the device already knows it and it is globally unique. The human-friendly Listener name is a mutable label that MUST be unique within its Deployment. The Aggregator carries both a platform UUID and its Balena device UUID for cross-reference. Pods and Deployments carry platform UUIDs and human names unique within their parent.

The Aggregator also carries an `aggregator_uuid`, and this value is the single join key that unifies three data planes: it is the label the Aggregator injects on every Prometheus series, the tag on its InfluxDB series, and the prefix on its S3 upload path. Metrics, analysis, and raw audio for one device therefore all correlate through it. The platform stores it as a first-class, indexed field on the Aggregator and uses it to join telemetry, science output, and object storage back to the inventory row.

The value is platform-assigned, not the Balena UUID by default, and resolves on the device in this precedence: an explicit `AGGREGATOR_UUID` environment variable, then `aggregator_uuid` in `settings.yaml`, then `${BALENA_DEVICE_UUID}` as a fallback, then a dev-only last resort for off-Balena testing. The Balena fallback exists to guarantee uniqueness for any device that reaches the field unprovisioned, which removes the risk of multiple unprovisioned devices colliding on one shared default across all three planes. The platform always knows the value without trusting a device self-declaration: in the primary path the provisioning tool generates it, writes it into `settings.yaml` in the bundle, and records it on the inventory row (no Balena dependency); in the fallback path the platform reads `BALENA_DEVICE_UUID` from the Balena API and records that.

### 4.3 Duplicate identifier handling

The platform handles collisions at two points.

1. **At config generation.** When an operator creates or imports devices, the platform validates Listener name uniqueness within the Deployment and MAC uniqueness globally. On a name collision it rejects by default and offers an auto-suffix option (`name`, `name-2`). On a MAC collision it always rejects, because a MAC is physical identity and a duplicate means a data-entry error or a cloned device.
2. **At registration and reconciliation.** When a device reports state, the platform matches on MAC. If a reporting MAC carries a name that disagrees with desired state, or two devices report the same MAC, the platform raises a `duplicate_identity` alert and quarantines the conflicting reported state rather than overwriting inventory.
3. **On Aggregator identity.** The platform validates `aggregator_uuid` uniqueness within the Organization. Unprovisioned detection keys off inventory membership, not a single sentinel value: any Aggregator that reports metrics, analysis, or objects under an `aggregator_uuid` that does not match an existing inventory row raises a `provisioning_required` alert rather than joining to that row. Because the check is membership-based rather than equality to one hardcoded default, the dev-only default can safely be a per-device value (for example the Pi's own MAC) without two unprovisioned dev boxes colliding with each other, since neither value is in inventory.

---

## 5. Configuration model

### 5.1 Inheritance with override

Configuration resolves through the hierarchy. Each entity stores only the settings explicitly set at that level (a sparse override map). The effective configuration for any device is the deep merge of all override maps from Organization down to that device, with lower levels winning.

```
effective(listener) =
    merge(org.overrides,
          deployment.overrides,
          pod.overrides,
          aggregator.overrides,
          listener.overrides)   // later args win on key conflict
```

This satisfies the requirement that settings such as duty cycle, sampling rate, bits per sample, log verbosity, and microSD buffering apply at any level. Because every Listener in a Pod shares one network, the WiFi/HaLow credentials, security type, and the target to reach the Aggregator resolve at the Pod level, and every Listener in the Pod inherits them. Settings that are specific to the compute node and not network-shared, for example the analysis model or the S3 prefix, resolve at the Aggregator level.

### 5.2 Bulk selection: the "select all that apply" mechanism

The platform offers two complementary mechanisms, because the two underlying needs differ.

1. **Inheritance edits** (set once, apply to many): set a value at a higher level and every descendant inherits it unless it overrides. This is the primary tool for "all Listeners under this Aggregator" or "everything in this Deployment."
2. **Tag and query selection** (cross-cutting, ad hoc): every entity carries free-form tags. A selector lets operators target a set by query (for example "all Listeners where `audio.sample_rate_hz = 48000`" or "all Aggregators tagged `coastal`"), preview the affected set, save the selection, and apply a change. Device tables also offer plain checkbox multiselect for one-off edits.

Every bulk edit shows an explicit preview of affected devices and the resulting effective config before it commits.

### 5.3 Settings catalog

The following table defines the v1 settings. "Lowest level" is the most specific level at which setting the value is normally meaningful, though inheritance permits setting most keys higher.

| Key | Type | Default | Lowest level | Secret | Notes |
|-----|------|---------|--------------|--------|-------|
| `audio.sample_rate_hz` | int enum {8000,16000,32000,48000,96000,192000,250000,384000} | 48000 | Listener | no | |
| `audio.bits_per_sample` | int enum {16,24} | 16 | Listener | no | |
| `audio.channels` | int | 1 | Listener | no | |
| `capture.mode` | enum {continuous, duty_cycle, schedule} | duty_cycle | Listener | no | |
| `capture.duty_on_seconds` | int | 60 | Listener | no | applies when mode=duty_cycle |
| `capture.duty_off_seconds` | int | 0 | Listener | no | |
| `capture.schedule` | cron-like object | null | Listener | no | applies when mode=schedule |
| `listener.wake_grace_seconds` | int | 30 | Aggregator | no | grace period past a Listener's declared wake time before it's marked offline; overridable at Aggregator, Deployment, or Organization level |
| `buffering.sd_enabled` | bool | true | Listener | no | microSD fallback buffering |
| `buffering.sd_max_bytes` | int | 0 (unbounded to card) | Listener | no | |
| `logging.verbosity` | enum {error,warn,info,debug,trace} | info | any | no | status/log verbosity |
| `network.wifi_ssid` | string | none | Pod | no | |
| `network.wifi_password` | secret ref | none | Pod | yes | stored encrypted; never in plaintext export |
| `network.wifi_security` | enum {WPA2,WPA3,WPA2_ENT,OPEN} | WPA3 | Pod | no | |
| `network.stream_key` | secret ref | none | Pod | yes | symmetric key encrypting audio and local control traffic between Listeners and their Aggregator, independent of WiFi-layer encryption; delivered and rotated per Sections 8.4 and 8.7 |
| `network.aggregator_ip` | string (IP) | none | Pod | no | shared by the Pod's Listeners |
| `network.stream_endpoint` | string (host:port/path) | none | Pod | no | shared by the Pod's Listeners |
| `network.stream_protocol` | enum {ws,tcp,udp} | ws | Pod | no | shared by the Pod's Listeners |
| `identity.name` | string | none | Listener | no | unique within Deployment |
| `identity.mac` | string (MAC) | device-known | Listener | no | immutable key |
| `location.gps_lat` | float | null | Listener | no | nullable until filled |
| `location.gps_lon` | float | null | Listener | no | |
| `analysis.model_id` | string | birdnet-default | Aggregator | no | edge AI model selection |
| `analysis.confidence_threshold` | float | 0.5 | Aggregator | no | |
| `upload.s3_bucket` | string | deployment default | Deployment | no | |
| `upload.s3_prefix` | string | "" | Aggregator | no | |
| `upload.s3_endpoint` | string | deployment default | Deployment | no | endpoint URL for non-AWS object stores (MinIO, Chameleon Object Store) |
| `upload.s3_access_key` | secret ref | none | Deployment | yes | delivered to Aggregators post-connect only (Section 16.4) |
| `upload.s3_secret_key` | secret ref | none | Deployment | yes | delivered to Aggregators post-connect only (Section 16.4) |
| `telemetry.influx_url` | string | deployment default | Deployment | no | |
| `telemetry.influx_token` | secret ref | none | Deployment | yes | delivered to Aggregators post-connect only (Section 16.4) |
| `telemetry.influx_database` | string | recordings | Deployment | no | |
| `telemetry.prometheus_url` | string | deployment default | Deployment | no | read URL the platform queries |
| `telemetry.prom_remote_write_url` | string | deployment default | Deployment | no | receiver endpoint Aggregator agents push to |
| `telemetry.prom_remote_write_user` | string | none | Deployment | no | |
| `telemetry.prom_remote_write_password` | secret ref | none | Deployment | yes | delivered to Aggregators post-connect only (Section 16.4) |
| `telemetry.grafana_url` | string | deployment default | Deployment | no | |

The catalog is data, not hard-coded UI. The platform stores it as a versioned schema so new keys add without a frontend rewrite, and the UI renders editors from the schema (type, enum, range, secret flag).

The deployment service settings (`telemetry.*`, `upload.s3_*`) resolve at the Deployment level and reach Aggregators only through the post-connect path in Section 16.4. They never appear in Listener-bound files (Section 5.4), and the deployment services onboarding flow (Section 16) writes them rather than the operator editing them key by key.

### 5.4 What goes in device-bound config files

Per-listener identity files contain only non-secret identity and stream-target data: MAC, name, sample rate, bit depth, capture settings, SD buffering, log verbosity, Aggregator IP, and stream endpoint. They never contain the WiFi password or any other secret. Secrets live at the Pod or Aggregator level, are stored encrypted in the platform, and reach devices only through the encrypted bundle path described in Section 8. This is what makes the per-pod and zero-touch models safe.

---

## 6. Reconciliation: desired state versus reported state

### 6.1 Model

Every Aggregator and Listener carries a desired configuration (computed effective config, snapshotted into a revision) and a reported configuration (last state the device sent). The platform's job is to converge them and to surface divergence.

### 6.2 Config revision lifecycle

The platform versions desired config as immutable `config_revision` rows. Each revision has a state. The states and transitions:

| State | Meaning |
|-------|---------|
| `draft` | Operator is editing. Not published to any device. |
| `pending` | Published to the device's desired topic, not yet acknowledged. |
| `applied` | Device reported state matching this revision. |
| `drifted` | Device previously applied, but reported state now diverges from desired. |
| `failed` | Device reported an error applying, or the pending window timed out. |
| `superseded` | A newer revision replaced this one. |

Transitions:

| From | To | Trigger |
|------|-----|--------|
| draft | pending | operator publishes |
| pending | applied | device reports matching state |
| pending | failed | device reports apply error, or timeout elapses |
| pending | superseded | a newer revision is published before ack |
| applied | drifted | device reports state diverging from desired |
| applied | superseded | operator publishes a newer revision |
| drifted | pending | operator (or auto-reconcile policy) re-publishes |
| failed | pending | operator retries |

```
        publish            ack match
draft ──────────► pending ──────────► applied
                    │                    │
                    │ error/timeout      │ divergence
                    ▼                    ▼
                  failed ◄──retry──── drifted
                    ▲   re-publish       │
                    └────────────────────┘
   (any non-terminal) ──new revision──► superseded
```

### 6.3 History and audit

The platform records every transition with a timestamp, the actor (user or system), the before/after effective config diff, and any device-supplied detail (error text, reported values). A per-device timeline view renders this history. An Organization-wide and per-Deployment audit log renders the same events filtered by scope. This satisfies the requirement to see the possible states, the transitions, the history, and the logs.

### 6.4 Reconciliation loop

A background worker:

1. Computes effective desired config for each device on change.
2. Publishes a new `config_revision` to the device's retained desired topic.
3. Consumes reported state from the reported topic, compares it to desired, and advances the revision state.
4. Times out pending revisions after a configurable window and marks them failed.
5. Periodically re-compares applied devices to detect drift even without a device-initiated report.

Listeners do not connect to MQTT directly. The Aggregator holds its Listeners' desired config, applies it to them over the local link (push or notify, not mandatory polling), and reports each Listener's state upward on the Listener reported subtopic.

### 6.5 Listener liveness detection

Listeners have no MQTT LWT, and the platform does not poll them, because a sleeping Listener does not respond to a poll. Liveness instead derives from expected wake windows that the Listener declares to its Aggregator over the local HaLow link.

Before a Listener enters an off-window under `capture.mode=duty_cycle` or `capture.mode=schedule`, it sends its Aggregator a local message declaring how long it plans to sleep (or the wake time it computes from its own clock). The Aggregator trusts this self-declared time rather than recomputing the schedule itself, since the Listener's own clock governs when it actually wakes.

Each Listener carries one of three liveness states as tracked by its Aggregator:

- **Streaming.** Actively sending audio. Healthy.
- **Sleeping (expected back at `T`).** The Listener declared an off-window and no data is expected until `T`. This reads as healthy in status and on the map, since the gap is expected.
- **Offline (missed window).** `T` plus the configured `listener.wake_grace_seconds` (Section 5.3, default 30s) has passed with no resumed stream. The Aggregator raises a `listener_missed_wake_window` event and reports the Listener as offline on the next `lst/{mac}/reported` publish.

This gives a no-poll liveness signal for sleeping devices: the Aggregator only has to compare its own clock to a time the Listener already promised, never send anything to wake it. For `capture.mode=continuous`, no wake window applies, and an unexpected gap in streaming still falls through to the existing `listener_stream_gap` detection (Section 7.3).

The wake-declaration message and the audio stream itself share the same and only channel between a Listener and its Aggregator, the Pod's HaLow WiFi network (Section 17, item 2 tracks the exact framing of that local link).

---

## 7. Control plane: MQTT contracts

### 7.1 Broker placement and security

Each Deployment runs one Mosquitto broker on its KVM host beside InfluxDB 3, Grafana, and Prometheus. The platform connects outbound to each deployment's broker using stored per-deployment credentials. Transport uses TLS. Aggregators authenticate with per-device credentials (username/password at minimum, mTLS where the deployment supports it) and are restricted by per-topic ACL to their own subtree. The platform account holds broker-wide publish/subscribe within the deployment namespace.

### 7.2 Topic namespace

All topics live under a deployment-scoped root. `{dep}` is the Deployment slug, `{agg}` the Aggregator id, `{mac}` the Listener MAC.

| Topic | Direction | Retained | Payload |
|-------|-----------|----------|---------|
| `eoe/{dep}/agg/{agg}/desired` | platform to device | yes | desired config revision |
| `eoe/{dep}/agg/{agg}/reported` | device to platform | no | reported Aggregator state |
| `eoe/{dep}/agg/{agg}/status` | device to platform (LWT) | yes | `online` / `offline` |
| `eoe/{dep}/agg/{agg}/event` | device to platform | no | logs, errors, lifecycle events |
| `eoe/{dep}/agg/{agg}/cmd` | platform to device | no | one-shot commands (restart, resync, flush buffer) |
| `eoe/{dep}/agg/{agg}/lst/{mac}/desired` | platform to device | yes | desired Listener config (Aggregator applies) |
| `eoe/{dep}/agg/{agg}/lst/{mac}/reported` | device to platform | no | reported Listener state |

The desired topics are retained so a reconnecting Aggregator immediately receives current desired state. The status topic carries the LWT payload so the broker marks a device offline on unexpected disconnect.

### 7.3 Message schemas

All payloads are JSON with a top-level `schema_version`. Examples below are illustrative shapes, not exhaustive.

Desired config:
```json
{
  "schema_version": 1,
  "revision_id": "uuid",
  "generated_at": "2026-06-23T12:00:00Z",
  "target": {"type": "aggregator", "id": "agg-uuid"},
  "config": { "logging.verbosity": "info", "analysis.confidence_threshold": 0.6 },
  "checksum": "sha256:..."
}
```

Reported state:
```json
{
  "schema_version": 1,
  "reported_at": "2026-06-23T12:00:05Z",
  "applied_revision_id": "uuid",
  "config": { "logging.verbosity": "info", "analysis.confidence_threshold": 0.6 },
  "health": {"uptime_s": 86400, "coarse": "ok"},
  "checksum": "sha256:..."
}
```

The `health` block in reported state is a coarse, best-effort liveness hint only. Prometheus is the authoritative source for operational health metrics (CPU, memory, disk, temperature, queue depth), delivered through the ingestion path in Section 10.4. The platform does not treat MQTT health numbers as metrics and does not chart them, which avoids two competing sources of truth for the same values.

Reported Listener state (published by the Aggregator on the Listener's behalf, per Section 6.5):
```json
{
  "schema_version": 1,
  "reported_at": "2026-06-23T12:00:05Z",
  "applied_revision_id": "uuid",
  "config": { "audio.sample_rate_hz": 48000 },
  "liveness": {
    "state": "sleeping",
    "last_audio_at": "2026-06-23T11:58:00Z",
    "expected_wake_at": "2026-06-23T12:05:00Z"
  },
  "checksum": "sha256:..."
}
```
`liveness.state` is one of `streaming`, `sleeping`, `offline`. `expected_wake_at` is present only while sleeping and comes from the Listener's own wake declaration; it is absent once the Listener resumes streaming or once it flips to `offline` after missing that time by more than `listener.wake_grace_seconds`.

Status (LWT):
```json
{ "schema_version": 1, "state": "offline", "at": "2026-06-23T12:01:00Z" }
```

Event:
```json
{
  "schema_version": 1,
  "at": "2026-06-23T12:00:30Z",
  "level": "warn",
  "code": "listener_stream_gap",
  "detail": "listener AA:BB:CC:DD:EE:FF gap 240ms",
  "listener_mac": "AA:BB:CC:DD:EE:FF"
}
```

`listener_missed_wake_window` event (Section 6.5), raised when a sleeping Listener does not resume streaming within its declared wake time plus grace:
```json
{
  "schema_version": 1,
  "at": "2026-06-23T12:05:31Z",
  "level": "warn",
  "code": "listener_missed_wake_window",
  "detail": "listener AA:BB:CC:DD:EE:FF expected 12:05:00Z, grace 30s elapsed",
  "listener_mac": "AA:BB:CC:DD:EE:FF"
}
```

### 7.4 Idempotency and ordering

The platform treats reported messages as idempotent by `applied_revision_id` plus checksum. It tolerates out-of-order delivery by comparing revision timestamps and ignoring stale reports. Commands carry a `command_id` so a device can deduplicate retries.

### 7.5 Balena relationship

The platform reads Balena device health and fleet variables where useful and may trigger OS-level or container-level actions through the Balena API. It never requires Balena to deliver application configuration. If Balena is unavailable, configuration and reconciliation continue over MQTT unaffected. This bounds the Balena dependency to OS and container lifecycle.

---

## 8. Provisioning and the microSD configuration tool

### 8.1 Goal

Operators prepare a deployment before hardware reaches the field. The platform generates configuration, exports it for loading onto microSD cards, and tracks which configuration belongs on which device, with history and expected contents. The browser does not write cards; a person loads the exported files.

### 8.2 Two supported generation modes (hybrid)

1. **Per-pod file (primary).** The platform generates one config file for the Pod and that identical file goes onto every microSD card in the Pod. This works because every Listener in a Pod shares the same network configuration (WiFi/HaLow credentials, security type, and the target to reach the Aggregator), which is the whole reason the Pod exists. That shared file is enough to bring each Listener up: it joins the network and streams to the Aggregator. The unique per-listener data (name, GPS) is left blank on the card and filled in later in the cloud, matched by the MAC the device already knows and reports on first contact. This is the default for a Pod and scales to large fleets because a field tech writes one file to a stack of cards.
2. **Explicit per-listener files (when needed).** The platform generates one config file per Listener, keyed by MAC, plus a folder containing all files for a Pod or Deployment, and a manifest. Use this only when specific devices need distinct settings at first boot rather than filled in later.

Operators pick per Pod, and may mix the two (the per-pod file for the Pod plus optional per-listener overrides for the few devices that need them).

For each Aggregator in a bundle, the platform generates its `aggregator_uuid`, writes it into the Aggregator's `settings.yaml`, and records it on the inventory row before the card ships. This makes the join key (Section 4.2) known and unique from first boot, keyed to the platform rather than to Balena, and leaves the `BALENA_DEVICE_UUID` fallback to cover only devices provisioned outside this tool.

The same `settings.yaml` carries the full bootstrap block (Section 16.4): the `aggregator_uuid`, the deployment's MQTT broker endpoint and TLS expectation, and the device's own per-device broker credentials. That is everything the Aggregator needs to reach the control plane; every other setting and secret arrives as retained desired configuration after first connect. Generating a provisioning bundle therefore requires the deployment's broker to be configured and verified first (Section 16.5).

### 8.3 Export format and contents

An export is a bundle (archive) containing:

- The Pod's shared config file (written to every card in the Pod), or one config file per Listener when per-listener mode is used, in a documented format the firmware reads.
- The Aggregator's `settings.yaml` containing the bootstrap block (Section 16.4), stored in plaintext by design.
- A `manifest.json` listing every file, its target MAC or pod, a checksum, the generating revision, and the expected on-device contents.
- A human-readable `README` describing what goes where.

Device-bound files contain no secrets in plaintext (Section 5.4). The WiFi password travels inside the Pod's config file as firmware-only ciphertext, which the field tech writes to the card exactly as generated, per Section 8.4.

### 8.4 Secret handling in exports

The WiFi PSK (and the stream key introduced in Section 8.7) are delivered to the firmware using envelope encryption, the same pattern the platform already uses for its own storage (Section 12.4), extended one layer further to the device itself:

- **Key-encryption key (KEK):** one static key, embedded in every Listener's firmware across the Organization. It is never used to encrypt a secret directly, only to wrap and unwrap data keys.
- **Data key (DEK):** generated fresh per Pod (or per export), and used to directly encrypt the actual WiFi PSK and stream key.

The platform wraps the DEK with the firmware KEK, and embeds the wrapped DEK plus the DEK-encrypted secrets in the Pod's config file. At boot, the firmware unwraps the DEK with its built-in KEK, then uses the DEK to decrypt the PSK and stream key. This is what makes one binary for every Listener in the Organization possible: there's no per-device build, no per-card passphrase, and no operator decryption step at flash time. The platform never writes these secrets anywhere, on a card or otherwise, as plaintext outside its own storage.

Because the DEK is what actually touches the secret and the KEK only ever wraps DEKs, a Pod's secrets can be rotated by generating a new DEK and a new wrapped-DEK-plus-ciphertext, without touching firmware at all (Section 8.7 covers delivering that rotation over the local link once a Listener has joined). The KEK itself is the one piece that's fixed: recovering a secret from a stolen card still requires the firmware's KEK, which lives behind ESP32 flash encryption and NVS encryption, both a MUST for this design given how much of the security model depends on them. Rotating the KEK itself, should it ever be suspected compromised, still means reflashing every Listener in the Organization; this remains tracked as an open item in Section 17.

Inside the platform, the pre-firmware-encryption secret is itself stored using its own separate envelope encryption at rest: a data key encrypts the secret, and a key-encryption key (from an environment variable in the simplest deploy, or a secret manager in cloud) encrypts that data key. This protects the secret in Postgres and is independent of the device-facing KEK/DEK pair described above.

### 8.5 Tracking helper and provisioning records

The platform stores a `provisioning_bundle` per export and a `device_provisioning_record` per device-to-card assignment. Each record tracks expected MAC, target slot (pod/aggregator), the config revision embedded, a status, and a history. Statuses: `generated`, `flashed` (operator marked the card written), `registered` (device connected and registered by MAC), `confirmed` (reported state matches the embedded revision), `mismatch` (registered device reports unexpected identity or config). This gives operators a live picture of what was prepared, what was loaded, what came online, and what disagrees with expectation.

### 8.6 Filling in details later

Listeners with no GPS render in the UI as leaves clustered around their Aggregator (Section 9). When an operator fills in GPS coordinates from the cloud, the Listener moves to a map pin. Live-GPS devices, when they exist in a future phase, render as moving dots. v1 supports the manual fill path only.

### 8.7 Secret rotation over the local link, and traffic encryption

Once a Listener has joined its Pod's network and is streaming to its Aggregator, the Aggregator can push a freshly generated wrapped-DEK-plus-ciphertext (Section 8.4) to it over the same local link already used for config push (Section 6.4) and wake declarations (Section 6.5). This lets an operator rotate a Pod's WiFi PSK and stream key without re-carding any device, and is the direct answer to a suspected-stolen or compromised Listener: rotate the Pod's secrets among every Listener still reachable, excluding the missing device's MAC from the push, then update the HaLow router to the new PSK. The excluded device is left holding a PSK that no longer works. First-boot delivery still requires the physical card, since a Listener with no prior connection has no live link yet to receive a push.

The Pod's WiFi-layer encryption (`network.wifi_security`, Section 5.3) protects the HaLow link only as long as the PSK itself stays secret; anyone who has the PSK can passively decrypt everything on that network with commodity hardware. Because this whole design assumes a stolen device or a compromised card is a real, planned-for scenario rather than a hard failure, audio and local control traffic between a Listener and its Aggregator are also encrypted with the stream key, a symmetric key independent of the WiFi PSK and delivered and rotated through the same envelope-encryption and local-push mechanism described above. This means a passive antenna capture of the HaLow airtime is not readable even by someone who has since obtained the Pod's WiFi PSK, without also having the stream key. The exact framing (per-packet AEAD versus a lightweight handshake) still needs to be pinned down against firmware behavior, tracked alongside the local-link transport decision in Section 17, item 2.

---

## 9. Map and visualization

### 9.1 Map technology

The map uses Leaflet with react-leaflet and OpenStreetMap tiles by default. For offline or air-gapped local hosting, the platform supports PMTiles, a single-file vector basemap requiring no tile server.

### 9.2 Hierarchy on the map

Each hierarchy level renders distinctly and is clickable, drilling from Organization summary to a single Listener.

- **Deployment:** A region or cluster marker at the deployment's representative coordinates, showing rolled-up status and alert counts.
- **Pod:** One marker at the Aggregator's location, since the Pod has a single Aggregator on Ethernet at the HaLow router. It is sized or badged by its Listener count and aggregate status, and its Listeners cluster around it as leaves until they get coordinates.
- **Listener with GPS:** A pin at its coordinates.
- **Listener without GPS:** A leaf node clustered visually around its Aggregator, not placed on real coordinates, until coordinates are filled in.
- **Live-GPS Listener (future):** A moving dot. Reserved, not built in v1.

Marker clustering keeps the map performant at scale. Status drives color (healthy, degraded, offline, alerting). Any device with an active alert renders with an alert badge. Clicking a marker opens a detail panel with status, effective config, reconciliation state, recent events, and a link or embed to its Grafana telemetry.

### 9.3 Status model

A device's displayed status derives from its online/offline signal, reconciliation state (applied/drifted/failed), and active alerts from Grafana. The platform computes a single rolled-up status per parent so a Deployment or Pod marker reflects the worst status among descendants; the Organization-wide rollup surfaces in the Owner summary (Section 10.3) rather than as a map marker, since the Organization has no map coordinates of its own.

For Aggregators, MQTT LWT is the authoritative real-time online/offline signal. Prometheus is not used for the instant liveness verdict, because the remote-write agent buffers to a write-ahead log and replays after reconnect (Section 10.4), so a returning Aggregator backfills and central Prometheus lags real time by design. Prometheus supplies health detail, history, and scrape-level `up` for diagnostics, while LWT drives the live status dot.

For Listeners, there is no LWT, since Listeners never hold an MQTT session (Section 6.4). Online/offline instead derives from the Aggregator-tracked liveness state in Section 6.5: `streaming` and `sleeping` both display as healthy, and `offline` (a missed wake window past grace) displays as offline, the Listener equivalent of the LWT signal.

---

## 10. Telemetry integration

### 10.1 Split of responsibilities

Two time-series stores hold different data, and the platform reads them for different purposes. Prometheus holds operational metrics: host CPU, memory, disk, and temperature, pipeline queue depth, processed-job counts, and per-process health, at short to medium retention. InfluxDB 3 holds the science output: detections, species identifications, confidence scores, and soundscape indices, at longer retention and queried for research and for the "total audio collected" style summaries. The platform reads Prometheus over PromQL for operational status and sparklines, and InfluxDB 3 over SQL/FlightSQL for analysis summaries. Both stores also feed Grafana.

The platform renders Postgres-owned data natively: inventory, config, reconciliation state and history, provisioning records, alert state, and small status indicators including sparklines built from cached samples. It does not re-implement Grafana dashboards. For rich telemetry it embeds existing Grafana panels (per deployment) by URL, scoped to the selected device, and links out to Grafana for deep exploration.

### 10.2 Querying deployment services

The platform queries each deployment's InfluxDB 3 (SQL/FlightSQL) and Prometheus directly using stored per-deployment credentials, captured and verified through the onboarding flow in Section 16. The Organization summary fans out across deployments and caches rolled-up results (total devices online out of registered, total audio collected, active deployments). There is no global aggregation layer in v1. The design leaves room to add one (for example Thanos or Mimir, or InfluxDB federation) without changing the device data model.

### 10.3 Summary metrics for the Owner view

The Owner dashboard shows, across all deployments: devices online out of devices registered, total audio collected, count of active deployments, and a global alert summary. These come from cached fan-out queries refreshed on an interval, so the Owner view stays fast even with many deployments.

### 10.4 Prometheus ingestion path

The platform reads from a central per-deployment Prometheus, and the ingestion into that Prometheus already follows a push (remote-write) design that suits field devices behind NAT. The platform integrates with this existing path rather than replacing it.

On each Aggregator, a metrics publisher process reads the local Redis pipeline state and the local supervisord interface and exposes a `/metrics` endpoint. A co-located Prometheus running in agent mode scrapes that endpoint plus node_exporter and a supervisor exporter, injects the `aggregator_uuid` label on every series, and remote-writes to the deployment's central Prometheus, which runs with the remote-write receiver enabled and basic auth. The agent buffers to a write-ahead log, so a device that loses connectivity backfills on reconnect. This is why Section 9.3 treats Prometheus as lagged for liveness and defers the instant verdict to MQTT LWT.

The platform does not scrape devices itself and does not run this agent. It stores each deployment's central Prometheus URL and read credentials (encrypted), and queries by PromQL, filtering on `aggregator_uuid` to scope series to a device. The same `aggregator_uuid` scopes InfluxDB queries and S3 prefixes, so one key correlates all three planes (Section 4.2).

### 10.5 Metric catalog

The platform relies on these known metric families for native status, sparklines, and alert rules. Exact names track the firmware and are pinned in the integration contract.

| Family | Type | Meaning | Platform use |
|--------|------|---------|--------------|
| pipeline queue depth (`stream:analyze`, `stream:upload`, `stream:publish_analysis`, `stream:publish_upload`) | gauge | backlog per pipeline stage | primary "falling behind" signal; drives backpressure alerts |
| processed-job counters (per job type) | counter | jobs fully processed per stage | throughput, progress, rate panels |
| `supervisor_process_state`, `supervisor_process_exitcode` | gauge (labeled) | per-process run state and last exit code | process-health status and alerts |
| node_exporter host metrics | gauge | CPU, memory, disk, temperature | device health, capacity alerts, sparklines |
| `up` (per scrape target) | gauge | scrape reachability | diagnostics, not the live status dot |

The pipeline queue-depth gauges deserve emphasis: they are the measurable form of the requirement that Aggregators keep up with analysis and upload without falling behind. Rising depth means the Aggregator is not draining its queues fast enough, so the platform treats sustained queue growth as a first-class health and alert condition, not just a chart.

---

## 11. Alerting integration

### 11.1 Source of truth

Grafana alerting is the alert source, since each deployment already runs Grafana. The platform does not build a parallel alert engine. Most alert rules evaluate PromQL over the deployment's central Prometheus, so Prometheus is the substrate under the alerting the platform surfaces. Alerts that depend on science thresholds may instead query InfluxDB 3 through Grafana.

Representative rules the deployment configures in Grafana (the platform receives and surfaces the results, it does not author them in v1):

- Aggregator falling behind: any pipeline queue-depth gauge sustained above a threshold for a set window.
- Device down: scrape target `up == 0`, or absence of recent samples, corroborated by MQTT offline for the live verdict.
- Process failure: `supervisor_process_state` not in the running state, or a nonzero `supervisor_process_exitcode`.
- Capacity: low free disk or high temperature from node_exporter.

### 11.2 Inbound path

Each deployment's Grafana sends alerts to a platform webhook contact point. The generated stack in Section 16.3 pre-provisions this contact point; for operator-supplied Grafana, the onboarding flow registers it during verification. The platform stores alert state (`firing`, `resolved`) with severity, labels, the mapped device, start and resolve times, and message. It maps alert labels to the device hierarchy so an alert attaches to the right Listener, Aggregator, Pod, or Deployment.

### 11.3 Backfill and surfacing

The platform can pull current alert state from the Grafana API to backfill after the platform itself was down. It surfaces alerts on the map (badges), in device detail panels, and in a deployment and Organization alert list. Outbound webhooks let the platform forward alert state to an external maintenance or notification system later, which keeps integration with existing systems open without coupling to one.

---

## 12. AuthN, AuthZ, and tenancy

### 12.1 Tenancy model

v1 runs a single Organization. The Organization is the root of the hierarchy table chain (Organization to Deployment to Pod to Aggregator to Listener), and access scoping flows through those foreign keys by join. The schema does not stamp a denormalized tenant id on every table. Adding more Organizations later means adding rows and a scoping filter, not a schema rewrite.

### 12.2 Authentication

v1 uses local accounts with passwords hashed using a modern algorithm (Argon2id). The self-hosted and air-gapped cases cannot depend on an external identity provider, so local accounts are the baseline. The auth layer is abstracted behind an interface so OIDC/SSO drops in later for organizations that centralize identity. Sessions use signed, expiring tokens. The platform supports optional TOTP two-factor for privileged roles.

### 12.3 Authorization (RBAC)

Four roles in v1:

| Role | Scope | Permissions |
|------|-------|-------------|
| Owner | Organization | Full access to all deployments, settings, users, billing-equivalent admin. |
| Deployment Operator | One or more assigned Deployments | Manage devices, config, reconciliation, provisioning, and view telemetry within assigned deployments. No org admin. |
| Field Tech | Assigned Deployments | Generate and track provisioning bundles, mark cards flashed, view provisioning status. No config push, no telemetry depth. |
| Viewer | Assigned scope | Read-only across maps, status, telemetry, and history. |

Permissions are checked at the API layer on every request and reflected in the UI by hiding or disabling actions the role cannot perform. Role assignments are scoped to deployments so an operator manages only their deployments.

### 12.4 Secrets and key management

Secrets (WiFi PSKs, deployment service credentials, MQTT credentials, S3 keys) are stored with envelope encryption. The key-encryption key comes from an environment variable in the simplest self-hosted deploy and from a secret manager (for example cloud KMS or HashiCorp Vault) in cloud. The platform supports key rotation by re-wrapping data keys. Secrets never appear in logs, API responses, or unencrypted exports.

---

## 13. API surface

The API is REST over HTTPS with a versioned prefix (`/api/v1`), plus a websocket channel for live updates. All list endpoints paginate, filter, and sort. All mutations are audited. The following is the resource surface, not an exhaustive endpoint list.

**Hierarchy and inventory**
- `GET/POST /organizations`, `GET/PATCH /organizations/{id}`
- `GET/POST /deployments`, `GET/PATCH/DELETE /deployments/{id}`
- `GET/POST /pods`, `GET/PATCH/DELETE /pods/{id}`
- `GET/POST /aggregators`, `GET/PATCH/DELETE /aggregators/{id}`
- `GET/POST /listeners`, `GET/PATCH/DELETE /listeners/{id}` (id by MAC)
- Bulk import endpoints for listeners and aggregators (CSV/JSON) with validation results.
- The platform enforces exactly one Aggregator per Pod. Creating a Pod either creates or attaches its single Aggregator, and attaching a second Aggregator to a Pod is rejected.

**Configuration and reconciliation**
- `GET /{entity}/{id}/config/effective` (computed merge)
- `GET/PUT /{entity}/{id}/config/overrides` (sparse overrides at this level)
- `POST /config/preview` (selection plus proposed change, returns affected devices and resulting effective config)
- `POST /config/apply` (publishes revisions to a selection)
- `GET /{entity}/{id}/revisions`, `GET /revisions/{id}`
- `GET /{entity}/{id}/timeline` (reconciliation and event history)
- `POST /aggregators/{id}/commands` (restart, resync, flush buffer)

**Selection and tags**
- `GET/POST /selections` (saved queries), `POST /selections/preview`
- `GET/PUT /{entity}/{id}/tags`

**Provisioning**
- `POST /provisioning/bundles` (generate; mode zero-touch or per-listener)
- `GET /provisioning/bundles/{id}`, `GET /provisioning/bundles/{id}/download` (encrypted archive)
- `GET/PATCH /provisioning/records/{id}` (status transitions: flashed, etc.)

**Deployment services**
- `GET/PUT /deployments/{id}/services` (service endpoints and credentials; secret fields are write-only and never echoed back)
- `POST /deployments/{id}/services/test` (run connection tests, returns per-service pass/fail with detail)
- `POST /deployments/{id}/services/stack` (generate the self-hosted stack bundle), `GET /deployments/{id}/services/stack/download`
- `GET /deployments/{id}/services/status` (per-service and rolled-up verification status)

**Telemetry and alerts**
- `GET /deployments/{id}/summary` (cached rollup)
- `GET /organization/summary` (global rollup for Owner view)
- `GET /{entity}/{id}/telemetry` (proxied/cached Influx and Prometheus reads)
- `GET /{entity}/{id}/grafana-embed` (signed embed URL)
- `POST /webhooks/grafana-alerts` (inbound alert receiver)
- `GET /alerts` (filterable list)

**Auth and admin**
- `POST /auth/login`, `POST /auth/logout`, `POST /auth/totp`
- `GET/POST /users`, `PATCH /users/{id}`, role and scope assignment
- `GET /audit` (audit log, filterable by scope, actor, action)

**Live updates**
- `WS /ws` channels for device status changes, reconciliation transitions, and new alerts, scoped by the user's role and assignments.

---

## 14. Non-functional requirements

### 14.1 Security

Transport uses TLS everywhere, including MQTT. Devices authenticate per-device and are restricted by topic ACL to their own subtree. Secrets held by the platform use envelope encryption at rest and never leave the platform in plaintext. Secrets delivered to Listeners (WiFi PSK, stream key) use a separate device-facing envelope scheme: a per-Pod data key encrypts the secret, wrapped by a key baked into every Listener's firmware, so secrets can be rotated per Pod without reflashing devices (Sections 8.4, 8.7). Audio and local control traffic between a Listener and its Aggregator is encrypted with the stream key independent of WiFi-layer encryption, so possession of the WiFi PSK alone does not expose that traffic to passive capture. The platform enforces RBAC on every request, hashes passwords with Argon2id, supports optional TOTP, and writes an immutable audit log of every mutation and config push. Input is validated by Pydantic at the boundary. The platform sets standard security headers and a strict CORS policy.

### 14.2 Scalability

The data model and APIs paginate and index for the stated v1 scale (order of 10 deployments, 20 aggregators each, 50 listeners each) and beyond. The Owner view relies on cached fan-out rather than live cross-deployment queries on every load. The map clusters markers. Telemetry reads cache with short TTLs. Per-deployment brokers and service stacks mean load scales horizontally by deployment. The simulation target (around 30 listeners across at least a few concurrent aggregators, plus up to around 20 or more mock aggregators) MUST run comfortably on a single self-hosted host.

### 14.3 Reliability

The control plane tolerates intermittent device connectivity by design: retained desired state, LWT offline detection, idempotent reported handling, and eventual consistency between desired and reported. The reconciliation worker retries and times out pending revisions deterministically. Platform restarts lose no committed state, because Postgres holds it and MQTT retains desired state at the broker. Alert and telemetry backfill recover state after platform downtime. The platform degrades gracefully when a deployment's services are unreachable, marking that deployment's data stale rather than failing the whole UI.

### 14.4 Performance

API reads target sub-200ms for cached and indexed queries at v1 scale. The map and dashboards render incrementally and never block on a slow deployment. Telemetry embeds load lazily. Bulk config previews compute server-side and stream results.

### 14.5 Maintainability

Everything ships as containers with declarative configuration and database migrations (Alembic). The settings catalog and config schema are versioned data, so new settings add without frontend rewrites. The auth provider, the secret backend, the map tile source, and the telemetry aggregation strategy each sit behind an interface so they swap without touching callers. Code is typed end to end (Pydantic and TypeScript). Tests cover the config merge, the reconciliation state machine, the provisioning manifest, and the RBAC checks.

---

## 15. Deployment topology

### 15.1 Self-hosted (ships first)

A single Docker Compose stack runs the platform: `api` (FastAPI), `frontend` (static build served by the API or a small web server), `postgres`, and optionally `redis`. Each Deployment's KVM host separately runs `mosquitto`, `influxdb3`, `grafana`, and `prometheus` (launched with the remote-write receiver enabled so Aggregator agents can push to it, per Section 10.4), and either cloud S3 or on-host MinIO. The platform connects outbound to each deployment's broker and services. The platform generates the per-deployment stack as a ready-to-run bundle when the operator chooses the self-hosted path (Section 16.3). The whole control platform fits one modest host. For the air-gapped case, the platform uses PMTiles for the map and local accounts for auth, requiring no external service.

### 15.2 Cloud

The same images run on Kubernetes. Postgres becomes a managed instance, S3 is cloud-native object storage, secrets move to a managed secret manager or KMS, and the frontend optionally serves from a CDN. The per-deployment services and broker remain per deployment. A future aggregation layer attaches here without changing devices.

### 15.3 Configuration

The platform reads configuration from environment variables and a config file, including database URL, key-encryption key or secret-manager reference, per-deployment service endpoints and credentials (stored encrypted after first entry), and map tile source. No secret is baked into an image.

---

## 16. Deployment services onboarding

### 16.1 Goal

Creating a Deployment includes standing up or connecting its service stack: the Mosquitto broker, InfluxDB 3, Prometheus, Grafana, and object storage. Until now this was a manual runbook in the aggregator-pi repository (SSH into the KVM host, install and configure each service by hand, then scatter the resulting tokens and passwords into Balena variables or `.env` files). The platform replaces that runbook with a guided flow. By the time hardware ships, the deployment's services exist, hold verified credentials the platform stores encrypted (Section 12.4), and the provisioning bundle plus the control plane together deliver everything each Aggregator needs. The operator chooses one of two paths per deployment and can move from one to the other.

### 16.2 Path A: connect existing services

For operators who already run the services, a form collects the endpoints and credentials per service, and the platform validates each entry with a live connection test before accepting it. The platform backend runs the tests, so the services must be reachable from the platform host; the air-gapped self-hosted case satisfies this because the platform and the deployment services share a network.

| Service | Inputs | Connection test |
|---------|--------|-----------------|
| Mosquitto | host, port, TLS on/off and CA, platform account username/password | Connect, then publish and subscribe on a reserved test topic under the deployment namespace. Also probe for the dynamic security plugin, which determines how per-device credentials are minted (Section 16.4). |
| InfluxDB 3 | URL, admin token, database name | Authenticated query against the database, plus write and delete of a single test point in a reserved measurement. |
| Prometheus | read URL, remote-write URL, basic auth username/password | Authenticated instant query (`up`) against the read URL, and an authenticated probe of the remote-write endpoint to confirm the receiver is enabled and the credentials are accepted. |
| Grafana | base URL, service account token | API health check, verification that Influx and Prometheus datasources exist (offering to provision them if absent), and registration of the platform's alert webhook contact point (Section 11.2). |
| Object storage (S3) | endpoint, region, bucket, access key, secret key | Head bucket, then put and delete a zero-byte test object under a reserved prefix. |

Each service carries a per-service status (`untested`, `verified`, `failed` with reason, and a last-tested timestamp). Object storage is required only when raw-audio upload is enabled for the deployment; the other four services are required. Secret fields are write-only: the API accepts them, stores them encrypted, and never returns them (Section 13).

### 16.3 Path B: generated self-hosted stack

For operators with no services yet, the platform generates a downloadable stack bundle: a `docker-compose.yml`, the per-service config files, and a README, targeted at the deployment's KVM host. This is the containerized equivalent of the manual runbook, with every credential generated by the platform, stored encrypted before download, and written into the bundle's config files so nothing needs hand-editing. Contents:

- **Mosquitto** with a TLS listener, the dynamic security plugin enabled, a pre-created platform account, and deployment-namespace ACLs.
- **InfluxDB 3** with an init step that creates the database and an admin token.
- **Prometheus** with basic auth (`web_config.yml`), the remote-write receiver enabled, a retention setting, and self-scrape plus node exporter scrape configs.
- **Grafana** with provisioned Influx and Prometheus datasources and the platform alert webhook contact point (Section 11.2).
- **MinIO** (optional) with a created bucket and generated access keys, for deployments that want on-host object storage rather than cloud S3.
- A README listing the required open ports (broker, 8181, 9090, 3000, and MinIO's if enabled) and the run instructions, which reduce to `docker compose up -d`.

The operator downloads the bundle, runs it on the KVM host, and clicks Verify services, which runs the same tests as Path A. Regeneration with rotated credentials is supported: the platform re-verifies and then republishes affected device configuration through the control plane (Section 16.4), so rotation is a config revision, not a manual redistribution. Auto-provisioning a VM through Chameleon Cloud preloaded with this stack is future work (Section 17, item 13).

### 16.4 Delivering service configuration to Aggregators

Delivery splits into a minimal bootstrap and everything else.

**Bootstrap block.** The Aggregator's `settings.yaml` in the provisioning bundle (Section 8.2) carries, in plaintext: the `aggregator_uuid`, the deployment's MQTT broker endpoint and TLS expectation, and the device's own per-device broker username and password. That is the complete set the device needs to reach the control plane, and nothing more. Plaintext here is an accepted trade, decided deliberately: the Pi sits outside the Listener firmware KEK/DEK scheme (Section 8.4), its storage travels with the physical device rather than on a card handed around in the field, the credentials are per-device and confined by topic ACL to the device's own subtree (Section 7.1), and the response to a lost or stolen Pi is revoking that one device's broker credentials, which cuts off everything downstream since all other secrets only ever reached the device through the broker.

**Post-connect delivery.** Every remaining value, including the Influx URL, token, and database, the Prometheus remote-write URL and credentials, the S3 endpoint, bucket, and keys, the Grafana URL, and all Section 5.3 settings, arrives as retained desired configuration on the Aggregator's desired topic through the ordinary reconciliation loop (Section 6.4). The worker publishes the deployment's service settings as part of effective config as soon as the device exists in inventory, so a device that comes online days later finds its full configuration waiting at the broker. This is the deferred-job behavior with no new machinery: the retained message is the job, and first connect is the trigger. Credential rotation, service migration, and endpoint changes are all config revisions that flow through the same states (Section 6.2).

**Balena as a convenience path only.** Balena fleet or device variables (the `balena_bulk_env.py` pattern in the aggregator-pi repository) remain supported as an optional convenience for development workflows, and environment variables continue to override `settings.yaml` on the device. Balena MUST NOT be the only path any configuration value can travel, consistent with Section 7.5.

**Per-device broker credentials.** The platform mints these at bundle-generation time. Against the generated stack, and against any Path A broker that passes the dynsec probe, it creates them through the Mosquitto dynamic security API over MQTT. Against a Path A broker without dynsec, the platform generates the credential pair and presents it to the operator to install on the broker manually, and the bundle is held until the operator confirms (Section 17, item 14 tracks whether to require dynsec instead).

### 16.5 Status and lifecycle

A Deployment carries a rolled-up `services_status`: `unconfigured`, `pending_verification`, `verified`, or `degraded`. Periodic re-checks, reusing the Section 10 read clients and lightweight probes, demote a deployment to `degraded` on repeated failure, and that state surfaces on the map rollup (Section 9.3) and the Owner summary (Section 10.3) as deployment-level health. Provisioning-bundle generation requires at minimum a verified broker, because the bootstrap block embeds broker credentials; the UI warns when generating bundles for a deployment whose remaining services are not yet verified, since devices would come online with nowhere to ship analysis, metrics, or audio.

---

## 17. Open issues and future work

These do not block phased implementation but need decisions before the relevant phase.

1. **Device-side config file format.** The exact on-card schema the firmware parses needs to be fixed jointly with the firmware. The platform treats it as a versioned template.
2. **Listener-to-Aggregator local transport framing. (Partially resolved.)** The Pod's HaLow WiFi network is confirmed as the only channel between a Listener and its Aggregator; there is no separate side channel. Still open: whether config push, config report, and the Section 6.5 wake-declaration message share the existing audio stream socket or use a separate local socket, and the exact framing needs confirmation against current firmware behavior.
3. **Auto-reconcile policy.** Whether drift auto-republishes or always waits for operator action, and per-deployment override of that policy.
4. **Telemetry aggregation layer.** If and when the Owner view needs more than cached fan-out, choose Thanos, Mimir, or Influx federation.
5. **OIDC rollout.** Which provider(s) to support first when SSO becomes a requirement.
6. **Live-GPS ingestion.** Topic and schema for moving-dot devices when that hardware exists.
7. **Firmware KEK custody and backup. (Revised from "bundle key escrow.")** The original passphrase-encrypted export path is gone; secrets now reach Listeners through firmware-side envelope encryption (Section 8.4), where the platform holds the org-wide KEK to wrap each Pod's DEK at export and rotation time. What's still open is where and how the platform stores and backs up that KEK: losing it would strand the ability to provision new Pods or rotate any Pod's secrets across the whole Organization.
8. **Multi-org isolation depth.** When multi-tenant arrives, confirm whether deployments can be shared across orgs or are always org-private.
9. **`aggregator_uuid` mapping. (Resolved.)** The value is platform-assigned, delivered through `settings.yaml` in the provisioning bundle, and recorded on the inventory row. The on-device resolution order is explicit `AGGREGATOR_UUID`, then `settings.yaml`, then `${BALENA_DEVICE_UUID}`, then a dev-only default. The dev-only default MAY be a per-device value (for example the Pi's own MAC) rather than one shared constant, because `provisioning_required` detection (Section 4.3) checks inventory membership, not equality to a single sentinel. The same value keys Prometheus labels, InfluxDB tags, and the S3 path (Sections 4.2, 4.3, 8.2, 10.4).
10. **Queue-depth alert thresholds.** Set the sustained depth and window per pipeline stage that count as "falling behind," per deployment or per Aggregator class.
11. **Remote-write scaling.** The current agent uses a single shard with capacity 5000, which suits v1 scale. Revisit shard count and queue sizing, or a store built for large-fleet remote-write (for example VictoriaMetrics), only if a deployment's central Prometheus becomes an ingestion bottleneck.
12. **Org-wide firmware KEK rotation.** Per-Pod secret rotation (WiFi PSK, stream key) is solved by Section 8.7's DEK rotation over the local link, no reflash required. What remains unsolved is rotating the KEK itself, the one key baked into every Listener's firmware: that still means reflashing every Listener in the Organization, and the operational plan for that (staged rollout, minimum firmware version tracking, etc.) needs to be decided before this ships to a large fleet.
13. **Chameleon Cloud VM auto-provisioning.** Extend Path B (Section 16.3) so the platform provisions a VM through Chameleon's OpenStack APIs preloaded with the generated stack, rather than handing the operator a bundle to run. Open questions: lease and credential custody for the operator's Chameleon account, image versus cloud-init delivery of the stack, and how far this generalizes to other OpenStack or cloud providers.
14. **Broker credential minting without dynsec.** Section 16.4 supports Path A brokers without the dynamic security plugin through a manual install step. Decide whether v1 should instead require dynsec (or an equivalent management API) for platform-managed brokers, which would remove the manual path and its held-bundle state at the cost of excluding some existing broker setups.

---

## 18. Proposed phase breakdown

This maps the spec to implementation phases for the separate phase instruction documents. Each phase is independently testable.

1. **Phase 0: Foundations.** Repo, container scaffolding, Postgres, migrations, FastAPI and React skeletons with the neutral design-token theme (Section 3.2), CI, local accounts and RBAC, audit log. (Sections 12, 15)
2. **Phase 1: Hierarchy and inventory.** Organization to Listener data model, CRUD, bulk import, duplicate-identifier handling, tags. (Sections 4, 13)
3. **Phase 2: Configuration model.** Settings catalog, sparse overrides, effective-config merge, selection and query, preview. (Section 5)
4. **Phase 3: Control plane and reconciliation.** Mosquitto integration, topic contracts, desired/reported, revision state machine, reconciliation worker, timeline. (Sections 6, 7)
5. **Phase 4: Provisioning tool.** Bundle generation (both modes), encrypted export, manifest, aggregator bootstrap block, provisioning records and tracking. (Sections 8, 16.4)
6. **Phase 5: Deployment services onboarding.** Services data model and encrypted credential storage, the Path A form and connection tests, the Path B generated stack, dynsec credential minting, post-connect delivery wiring through the reconciliation worker, and the services status lifecycle. Depends on Phases 1 and 3; the service clients built here are reused by Phase 7. (Section 16)
7. **Phase 6: Map and monitoring.** Leaflet map, hierarchy rendering, leaf-vs-pin logic, status rollup including deployment services status, device detail panels. (Section 9)
8. **Phase 7: Telemetry and alerts.** Per-deployment Influx/Prometheus reads, Grafana embeds, Owner summary fan-out and cache, Grafana alert webhook and surfacing. (Sections 10, 11)
9. **Phase 8: Hardening and cloud.** Secret rotation, performance tuning, reliability and degradation behavior, Kubernetes manifests, OIDC interface. (Sections 14, 15)

A simulation harness (mock Aggregators and Listeners over MQTT) should be built alongside Phase 3 so later phases test against a realistic fleet of around 20 or more mock aggregators with around 30 listeners each.

The frontend phases build against the neutral design-token theme. A parallel design track selects the visual design system and UX patterns in Figma, and applying that selection lands as a discrete task once the track concludes, with no structural rework required (Section 3.2). The project management document, not this spec, tracks that track's participants and schedule.

---

*End of specification v1.1.*
