"""The only place a deployment service is dialled from (phase-5 fixed choice 8).

Every tester in `app/services/testers/` is written in terms of a client here,
and no other module opens a socket to a deployment's Influx, Prometheus,
Grafana, object store or broker. That is what makes spec 16.5's "reusing the
Section 10 read clients" true **by construction** when E7 arrives, rather than
a promise someone has to remember to keep: when E7.1 needs `aggregator_uuid`-
scoped SQL and E7.2 needs PromQL, they add query methods to these classes and
inherit credential resolution, timeouts, TLS handling and error mapping for
free. Spec 18 says as much - "the service clients built here are reused by
Phase 7".

**Naming caveat, stated once per phase-5 fixed choice 8:** `app/services/`
means **deployment services**. `app/config/service.py` is the merge accessor
and is unrelated to any of this.

**Nothing here decides a verdict.** A client dials, and raises or returns; the
tester next door turns that into a `CheckResult` with a remedy. Keeping the
split means E7 can reuse the dialling without inheriting E5's opinions about
what an operator should be told.
"""
