"""Gate 5: CI pipeline checks (task E0.5).

The pipeline's honesty rests on one invariant: CI jobs and the local gate
execute the same registry stages in gate.sh, and every stage blocks the merge
through the ci-green fan-in. These tests enforce the registry/workflow parity
in both directions, the fan-in completeness, and the workflow details the
suites depend on (history depth, tags, the migrations service, BUILD_SHA).
"""

import re

import yaml
from conftest import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GATE_SH = REPO_ROOT / "gate.sh"
FAN_IN = "ci-green"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _workflow()["jobs"]


def _triggers() -> dict:
    data = _workflow()
    # PyYAML 1.1 parses the bare `on:` key as boolean True.
    return data.get("on", data.get(True))


def _registry_stages() -> list[str]:
    match = re.search(r'^STAGES="([^"]+)"', GATE_SH.read_text(encoding="utf-8"), re.M)
    assert match, "gate.sh has no STAGES registry line"
    return match.group(1).split()


def _stage_functions() -> list[str]:
    text = GATE_SH.read_text(encoding="utf-8")
    return [name.replace("_", "-") for name in re.findall(r"^stage_([a-z0-9_]+)\(\)", text, re.M)]


def _job_stage_invocations() -> dict[str, str]:
    """Map job id -> registry stage invoked via `sh gate.sh <stage>`."""
    invocations: dict[str, str] = {}
    for job_id, job in _jobs().items():
        for step in job.get("steps", []):
            run = step.get("run", "") or ""
            match = re.search(r"sh gate\.sh ([a-z0-9-]+)", run)
            if match:
                invocations[job_id] = match.group(1)
    return invocations


# --- check 1: triggers and concurrency ---


def test_workflow_parses_with_push_and_pr_triggers_and_concurrency():
    triggers = _triggers()
    assert "push" in triggers, "CI must run on every push (phase-0 E0.5)"
    assert "pull_request" in triggers
    concurrency = _workflow()["concurrency"]
    assert "group" in concurrency and concurrency.get("cancel-in-progress") is True


# --- checks 2 and 3: registry/workflow parity, both directions ---


def test_registry_and_stage_functions_agree():
    assert sorted(_registry_stages()) == sorted(_stage_functions()), (
        "STAGES line and stage_* functions in gate.sh disagree"
    )


def test_every_registry_stage_is_invoked_by_exactly_one_job():
    invoked = list(_job_stage_invocations().values())
    for stage in _registry_stages():
        assert invoked.count(stage) == 1, (
            f"stage {stage!r} must be invoked by exactly one CI job "
            f"(3-step recipe, docs/INTERFACES.md 'CI pipeline')"
        )


def test_every_job_invokes_a_registry_stage_or_is_the_fan_in():
    invocations = _job_stage_invocations()
    stages = set(_registry_stages())
    for job_id in _jobs():
        if job_id == FAN_IN:
            continue
        assert job_id in invocations, f"job {job_id!r} does not invoke a gate.sh stage"
        assert invocations[job_id] in stages, (
            f"job {job_id!r} invokes unknown stage {invocations[job_id]!r}"
        )


# --- check 4: fan-in completeness ---


def test_fan_in_needs_every_other_job_and_fails_instead_of_skipping():
    jobs = _jobs()
    assert FAN_IN in jobs, "ci-green fan-in job missing"
    fan_in = jobs[FAN_IN]
    expected = sorted(job_id for job_id in jobs if job_id != FAN_IN)
    assert sorted(fan_in["needs"]) == expected, (
        "ci-green must need every other job; a job outside the needs list cannot block merge"
    )
    assert fan_in.get("if") == "always()", (
        "ci-green must run always(): GitHub treats a skipped required check as satisfied"
    )
    run = " ".join(step.get("run", "") or "" for step in fan_in["steps"])
    for outcome in ("failure", "cancelled", "skipped"):
        assert outcome in run, f"ci-green does not fail on dependency outcome {outcome!r}"


# --- check 5: backend-tests job carries what the suites need ---


def test_backend_tests_job_has_history_tags_and_commit_template():
    job = _jobs()["backend-tests"]
    checkout = next(s for s in job["steps"] if "checkout" in (s.get("uses") or ""))
    assert checkout["with"]["fetch-depth"] == 0, "git-hygiene scan needs full history"
    assert checkout["with"]["fetch-tags"] is True, "planning-baseline compare needs tags"
    runs = " ".join(step.get("run", "") or "" for step in job["steps"])
    assert "git config commit.template .gitmessage" in runs


# --- check 6: migrations job runs the literal D9 commands ---


def test_migrations_stage_runs_the_literal_reversibility_commands():
    job = _jobs()["migrations"]
    assert "postgres" in job["services"]
    assert "DATABASE_URL" in job["env"]
    stage = GATE_SH.read_text(encoding="utf-8")
    body = stage.split("stage_migrations_check()")[1].split("\n}")[0]
    for command in (
        "alembic upgrade head",
        "alembic downgrade -1",
        "alembic downgrade base",
    ):
        assert command in body, f"migrations-check stage missing: {command}"
    assert body.count("alembic upgrade head") >= 3, "round trip requires three upgrades"


# --- check 7: containers job builds everything with the real SHA ---


def test_containers_job_injects_build_sha_and_builds_all_targets():
    job = _jobs()["containers"]
    assert job["env"]["BUILD_SHA"] == "${{ github.sha }}"
    stage = GATE_SH.read_text(encoding="utf-8")
    body = stage.split("stage_containers_build()")[1].split("\n}")[0]
    assert "--build-arg BUILD_SHA" in body
    assert "--target dev" in body and "--target prod" in body


# --- check 8: e2e job installs browsers ---


def test_e2e_job_installs_playwright_browsers():
    runs = " ".join(step.get("run", "") or "" for step in _jobs()["frontend-e2e"]["steps"])
    assert "playwright install" in runs


# --- check 9: the extension recipe is documented ---


def test_interfaces_documents_the_extension_recipe():
    text = (REPO_ROOT / "docs" / "INTERFACES.md").read_text(encoding="utf-8")
    assert "### CI pipeline" in text
    section = text.split("### CI pipeline")[1].split("### ")[0]
    for needle in ("gate.sh", "3-step", "ci-green", "needs"):
        assert needle in section, f"CI pipeline section missing: {needle}"
