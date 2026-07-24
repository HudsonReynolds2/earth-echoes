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

## The five-minute path

```
git clone <this repository> && cd earth-echoes
cp deploy/.env.example deploy/.env        # fill in every value
docker compose -f deploy/docker-compose.yml up -d --build
cd backend
uv run python -m app.seed                 # prints the owner credentials ONCE
uv run python -m app.verify               # proves the whole platform works
```

Then open `http://localhost:5173` and sign in with the seeded credentials.

## What this platform is

The management plane for the Echoes of Earth bioacoustic monitoring system: deployment
configuration, remote monitoring, and remote reconfiguration of Listener/Aggregator fleets.
This repository currently ships the platform foundations (accounts, roles, audit,
encrypted secret storage); device hierarchy, configuration, and the live control plane
arrive in subsequent releases. The full technical specification lives in
`project_planning/`.
