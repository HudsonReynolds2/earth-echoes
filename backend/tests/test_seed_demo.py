"""Gate 28: E1.9 demo fixture (D43).

One command seeds owner + the canonical hierarchy; the fixture is
deterministic and referenced BY NAME by later epics (and mirrored by the
frontend test fixture); re-seeding refuses; the base no-flag path is
untouched (test_seed.py still covers it verbatim). Runs app.seed in a real
subprocess against an ephemeral postgres, like test_seed.py.
"""

import os
import subprocess
import sys

import pytest
from conftest import REPO_ROOT, ephemeral_postgres, make_kek
from sqlalchemy import func, select

from app.db import create_session_factory
from app.models import Aggregator, AuditLog, Deployment, Listener, Organization, Pod

pytestmark = pytest.mark.integration

BACKEND = REPO_ROOT / "backend"


def _run_seed(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "app.seed", *args],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EOE_SESSION_SECRET": f"seed-demo-{make_kek()[:12]}",
            "EOE_KEK": make_kek(),
        },
        timeout=180,
    )


def test_demo_seed_is_one_command_deterministic_and_refuses_reruns():
    with ephemeral_postgres(migrate=False) as url:
        first = _run_seed(url, "--demo")
        assert first.returncode == 0, first.stderr
        # Owner credentials printed exactly once, demo counts announced.
        assert first.stdout.count("password:") == 1
        assert "demo hierarchy seeded: 2 deployments, 6 pods, 6 aggregators, 28 listeners" in (
            first.stdout
        )

        _, factory = create_session_factory(url)
        with factory() as db:
            org_names = set(db.scalars(select(Organization.name)))
            assert org_names == {"Earth Echoes Demo"}
            slugs = set(db.scalars(select(Deployment.slug)))
            assert slugs == {"redwood-coast", "high-desert"}
            pods = set(db.scalars(select(Pod.name)))
            assert "Pod 01 · Alder Creek" in pods and "Pod 03 · Dry Wash" in pods
            assert db.scalar(select(func.count()).select_from(Pod)) == 6
            agg_uuids = set(db.scalars(select(Aggregator.aggregator_uuid)))
            assert agg_uuids == {
                "demo-agg-rc-01",
                "demo-agg-rc-02",
                "demo-agg-rc-03",
                "demo-agg-hd-01",
                "demo-agg-hd-02",
                "demo-agg-hd-03",
            }
            assert db.scalar(select(func.count()).select_from(Listener)) == 28
            # Deterministic rows E2/E6 reference by name; first listener of the
            # first pod carries the pod tags and GPS.
            alder_01 = db.get(Listener, "02:EE:0E:01:01:01")
            assert alder_01 is not None and alder_01.name == "alder-creek-01"
            assert alder_01.tags == ["coastal"] and alder_01.gps_lat == 47.6
            alder_02 = db.get(Listener, "02:EE:0E:01:01:02")
            assert alder_02 is not None and alder_02.gps_lat is None
            # One system audit row summarizes the seed.
            detail = db.scalar(
                select(AuditLog.detail).where(AuditLog.action == "inventory.seed_demo")
            )
            assert detail == {
                "deployments": 2,
                "pods": 6,
                "aggregators": 6,
                "listeners": 28,
            }

        rerun = _run_seed(url, "--demo")
        assert rerun.returncode == 1
        assert "demo hierarchy already exists" in rerun.stderr
        assert "password:" not in rerun.stdout  # no second credential print


def test_demo_flag_adds_hierarchy_to_an_already_seeded_environment():
    with ephemeral_postgres(migrate=False) as url:
        base = _run_seed(url)
        assert base.returncode == 0 and base.stdout.count("password:") == 1
        demo = _run_seed(url, "--demo")
        assert demo.returncode == 0, demo.stderr
        # Hierarchy only: the owner exists, so no new credentials print.
        assert "password:" not in demo.stdout
        assert "demo hierarchy seeded" in demo.stdout
        _, factory = create_session_factory(url)
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Listener)) == 28
