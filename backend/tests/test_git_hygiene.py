"""Gate 0 check 9 (rule R3): no AI attribution anywhere in git history.

Scans every commit message, author, and committer on the current branch, plus
annotated tag messages, for the forbidden strings listed in
.claude/rules/project-rules.json. Runs at every gate so an accidental trailer
is caught before the push rather than living in history.
"""

import json

from conftest import REPO_ROOT
from conftest import run_git as _git


def _forbidden_strings() -> list[str]:
    rules = json.loads(
        (REPO_ROOT / ".claude" / "rules" / "project-rules.json").read_text(encoding="utf-8")
    )
    return rules["rules"]["R3_git_and_publication"]["attribution"]["forbidden_strings"]


def test_no_ai_attribution_in_commit_history():
    log = _git("log", "--format=%B%n%an <%ae>%n%cn <%ce>").lower()
    for forbidden in _forbidden_strings():
        assert forbidden.lower() not in log, (
            f"forbidden attribution string {forbidden!r} found in git history (rule R3)"
        )


def test_no_ai_attribution_in_tag_messages():
    tags = _git("tag", "-l", "--format=%(contents)%(taggername)").lower()
    for forbidden in _forbidden_strings():
        assert forbidden.lower() not in tags, (
            f"forbidden attribution string {forbidden!r} found in a tag (rule R3)"
        )
