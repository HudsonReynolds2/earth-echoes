"""Gate 0: governance scaffolding checks (task E0.0).

Verifies the binding rule set, the rules loader, the three project logs, the
INTERFACES skeleton, the addendum linkage between docs/project-changes.md and
the planning documents, and that the planning documents' original text is
unmodified apart from appended addendum blocks.
"""

import json
import re

from conftest import REPO_ROOT, run_git

RULES_PATH = REPO_ROOT / ".claude" / "rules" / "project-rules.json"
LOADER_PATH = REPO_ROOT / "CLAUDE.md"
DOCS = REPO_ROOT / "docs"
PLANNING = REPO_ROOT / "project_planning"

ADDENDUM_RE = re.compile(
    r"^> \*\*Addendum ([A-Z][A-Z0-9.]*-[0-9.]+-\d{2}) "
    r"\((\d{4}-\d{2}-\d{2}), ref project-changes #(\d+)\):\*\* .+$"
)


def _read(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


# --- Gate 0 check 1: rules JSON parses and carries all documented keys ---


def test_rules_json_parses_with_all_rule_sections():
    rules = json.loads(_read(RULES_PATH))["rules"]
    assert set(rules) == {
        "R0_test_gate_protocol",
        "R1_record_keeping",
        "R2_cross_phase_conventions",
        "R3_git_and_publication",
    }


def test_rules_json_r0_documented_keys():
    r0 = json.loads(_read(RULES_PATH))["rules"]["R0_test_gate_protocol"]
    for key in ("statement", "requirements", "forbidden", "on_failure", "test_critical_suites"):
        assert key in r0, f"R0 missing key: {key}"
    assert len(r0["test_critical_suites"]["components"]) == 4


def test_rules_json_r1_documented_keys():
    r1 = json.loads(_read(RULES_PATH))["rules"]["R1_record_keeping"]
    assert set(r1["logs"]) == {"project_updates", "project_changes", "decisions"}
    assert "format" in r1["addendum_convention"]


def test_rules_json_r2_documented_keys():
    r2 = json.loads(_read(RULES_PATH))["rules"]["R2_cross_phase_conventions"]
    assert "scope_discipline" in r2["conventions"]
    assert len(r2["universal_definition_of_done"]) == 5


def test_rules_json_r3_documented_keys():
    r3 = json.loads(_read(RULES_PATH))["rules"]["R3_git_and_publication"]
    assert r3["attribution"]["forbidden_strings"]
    assert len(r3["push_protocol"]["sequence"]) == 6


# --- Gate 0 check 2: the loader references the JSON and restates the R3 ban ---


def test_loader_references_rules_json_and_restates_attribution_ban():
    loader = _read(LOADER_PATH)
    assert ".claude/rules/project-rules.json" in loader
    assert "Co-Authored-By" in loader  # restates the banned trailer by name
    assert "gate" in loader.lower()


# --- Gate 0 check 3: all governance files exist with a top-level heading ---


def test_governance_files_exist_with_headings():
    for path in (
        LOADER_PATH,
        DOCS / "project-updates.md",
        DOCS / "project-changes.md",
        DOCS / "DECISIONS.md",
        DOCS / "INTERFACES.md",
    ):
        assert path.is_file(), f"missing: {path}"
        assert _read(path).startswith("# "), f"no top-level heading: {path}"
    assert RULES_PATH.is_file()


# --- Gate 0 check 4: DECISIONS carries D1 through D9 ---


def test_decisions_d1_through_d9_present():
    decisions = _read(DOCS / "DECISIONS.md")
    for n in range(1, 10):
        assert f"## D{n} (" in decisions, f"missing decision D{n}"


# --- Gate 0 checks 5 and 6: change-log and addenda cross-link both ways ---


def _change_entries():
    text = _read(DOCS / "project-changes.md")
    entries = []
    for block in re.split(r"^## ", text, flags=re.M)[1:]:
        number = re.match(r"#(\d+) \(\d{4}-\d{2}-\d{2}\)", block)
        affects = re.search(r"- \*\*Affects:\*\* (\S+)", block)
        addendum = re.search(r"- \*\*Addendum:\*\* (\S+)", block)
        assert number and affects and addendum, f"malformed change entry: {block[:80]!r}"
        entries.append((int(number.group(1)), affects.group(1), addendum.group(1)))
    return entries


def test_every_change_entry_names_an_addendum_that_exists():
    entries = _change_entries()
    assert entries, "project-changes.md has no entries"
    for number, affects, addendum_id in entries:
        doc = REPO_ROOT / affects
        assert doc.is_file(), f"change #{number} references missing document {affects}"
        assert f"**Addendum {addendum_id} (" in _read(doc), (
            f"change #{number} names addendum {addendum_id}, not found in {affects}"
        )


def test_every_addendum_matches_format_and_backreferences_a_change():
    changes = _read(DOCS / "project-changes.md")
    found = 0
    for doc in sorted(PLANNING.glob("*.md")):
        for line in _read(doc).splitlines():
            if line.startswith("> **Addendum"):
                match = ADDENDUM_RE.match(line)
                assert match, f"malformed addendum in {doc.name}: {line[:100]!r}"
                assert f"## #{match.group(3)} (" in changes, (
                    f"addendum {match.group(1)} references project-changes "
                    f"#{match.group(3)}, which does not exist"
                )
                found += 1
    assert found >= 4, f"expected at least 4 addenda, found {found}"


# --- Gate 0 check 7: original planning text unmodified apart from addenda ---


def _strip_addenda(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        if ADDENDUM_RE.match(line):
            if lines and lines[-1] == "":
                lines.pop()
            continue
        lines.append(line)
    return "\n".join(lines)


def test_planning_documents_unmodified_except_appended_addenda():
    for doc in sorted(PLANNING.glob("*.md")):
        baseline = run_git("show", f"planning-baseline:project_planning/{doc.name}").replace(
            "\r\n", "\n"
        )
        assert _strip_addenda(_read(doc)) == baseline, (
            f"{doc.name} differs from planning-baseline beyond appended addendum blocks"
        )


# --- Gate 0 check 8: planning documents are tracked ---


def test_planning_documents_tracked_by_git():
    tracked = run_git("ls-files", "project_planning/").splitlines()
    assert len(tracked) >= 7


# --- Gate 0 check 10: commit template exists, matches R3, carries no trailer ---


def test_gitmessage_template_matches_r3():
    template = _read(REPO_ROOT / ".gitmessage")
    assert "{TASK_ID}" in template
    assert "Gate {N} passed" in template
    assert "Co-Authored-By" not in template
    configured = run_git("config", "commit.template").strip()
    assert configured == ".gitmessage"
