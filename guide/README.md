# Echoes of Earth — User Guide

**Start here.** This directory is the home of everything client-facing in this repository:
if you operate a deployment of the Echoes of Earth management platform (rather than develop
it), everything you need is in this folder. Engineering-internal material lives elsewhere
(`docs/` holds interface contracts, decision records, and project logs; `project_planning/`
holds the specification).

## What's here

| Document | Use it to |
|---|---|
| [Getting started](getting-started.md) | Bring the platform up from nothing: prerequisites, configuration, first launch, first sign-in |
| [The seed script](seed-script.md) | Create the initial owner account — what the script does, how to run it, and the security implications |
| [Verify your deployment](verify-deployment.md) | Prove every platform subsystem works end to end, using a temporary account that cleans up after itself |
| [Bulk import](bulk-import.md) | Register many listeners or aggregators at once from CSV or JSON, with per-row results |
| [E1 verification walkthrough](e1-verification.md) | Hand-verify the hierarchy and inventory release feature by feature against a seeded local stack |
| [E2 verification walkthrough](e2-verification.md) | Hand-verify the configuration release: the inheritance editor, secrets, bulk preview/commit, and saved selections |
| [E3 verification walkthrough](e3-verification.md) | Hand-verify the control plane: the MQTT broker and its isolation, publication, reconciliation, and device status |
| [E5 verification walkthrough](e5-verification.md) | Hand-verify deployment services onboarding: write-only credentials, the five connection tests, the rolled-up status, and the generated stack |
| [SIM verification walkthrough](sim-verification.md) | Hand-verify the simulation harness: the mock fleet, the six shipped scenarios, and the full-scale load procedure |

There is no E4 walkthrough yet: epic E4 (device provisioning) has not been built — E5 and the
simulation harness landed first, by owner decision (see `docs/DECISIONS.md` D160). Its
walkthrough ships with the epic, like every other row above.

## The five-minute path

```
git clone <this repository> && cd earth-echoes
cp deploy/.env.example deploy/.env        # fill in every value
cd backend
uv run python -m app.devbroker --certs-only   # the broker needs its TLS files first
cd .. && docker compose -f deploy/docker-compose.yml up -d --build
cd backend
uv run python -m app.seed                 # prints the owner credentials ONCE
uv run python -m app.verify               # proves the whole platform works
```

Then open `http://localhost:15173` and sign in with the seeded credentials.

On Windows, `.\qa-stack.ps1` from the repo root does all of the above in one command
(with the demo inventory seeded) — see the E1 verification walkthrough.

## What this platform is

The management plane for the Echoes of Earth bioacoustic monitoring system: deployment
configuration, remote monitoring, and remote reconfiguration of Listener/Aggregator fleets.
This repository currently ships the platform foundations (accounts, roles, audit,
encrypted secret storage), the device hierarchy and inventory, the configuration model,
the live MQTT control plane, deployment services onboarding with the generated
per-deployment stack, and a simulation harness that stands in for real fleets. Device
provisioning (bundle generation and tracking), the map, and alerting arrive in subsequent
releases. The full technical specification lives in `project_planning/`.
