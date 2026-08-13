"""Deployment services: the five spec 16.2 services a Deployment connects to.

**`app.services` means DEPLOYMENT services, not "the service layer".** The
merge accessor is `app.config.service` and is unrelated (phase-5 fixed
choice 8, stated once so the two are never confused).

E5.1 ships `store.py`, the row access for the widened `deployment_service`
table. Later units add the write-only credentials API, the connection
testers, the status rollup, dynsec credential minting and the generated
stack; `app/services/clients/` will be the only place a deployment service is
dialled from, so spec 16.5's "reusing the Section 10 read clients" is true by
construction rather than by discipline.
"""
