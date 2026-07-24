"""Merge-blocking proof (scratch): deliberately failing test.

Demonstrates whether GitHub blocks merging a red PR on main. Branch and
draft PR are deleted after; the run log is the evidence.
"""


def test_deliberate_failure_for_block_proof():
    assert False, "deliberate failure: merge must be blocked if protection works"
