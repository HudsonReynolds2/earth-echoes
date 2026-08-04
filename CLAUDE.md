# Echoes of Earth: Project Rules

Binding rules for every session working in this repository. The source of truth is
`.claude/rules/project-rules.json`; this file restates the non-negotiables so they are
always in context. If this summary and the JSON ever disagree, the JSON wins.

## Authority order

1. `project_planning/echoes-of-earth-platform-spec-v1.1.md` (authoritative spec)
2. Phase documents (`project_planning/phase-N-*.md`): binding scope and fixed choices
3. `project_planning/implementation-handbook.md`: process and cross-phase conventions
4. `docs/INTERFACES.md`: contracts owned by earlier phases; never change without flagging first
5. `docs/DECISIONS.md`: recorded deviations and open-question decisions

## R0: Test gates (non-negotiable)

- Every numbered task ends with a gate: `./gate.ps1` (Windows) or `make gate` (POSIX).
- The ENTIRE accumulated suite must pass: 0 failed, 0 skipped, 0 xfailed, 0 deselected.
  Only then may the next task begin. No exceptions, for any reason.
- Forbidden at a gate: skip markers, test filters (`-k`, `--grep`, `--maxfail`, `--lf`),
  failure allowlists, self-skipping integration tests. Docker is a hard prerequisite.
- If a test is wrong: fix the test AND record why in `docs/DECISIONS.md` before proceeding.
- Report failures verbatim. Never summarize a failure as a pass.

## R1: Record keeping

- `docs/project-updates.md`: dated entry after each gate PASSES and manual verification
  ran. Never in anticipation.
- `docs/project-changes.md`: numbered entry for every plan change, plus an addendum
  appended to the affected planning document
  (format: `> **Addendum {ID} ({date}, ref project-changes #{N}):** {text}`).
- `docs/DECISIONS.md`: every deviation and every choice the documents left open.
- Verification walkthroughs (`guide/e{N}-verification.md`) are living acceptance
  documents: every epic ships its own before its final gate, and amends any prior
  walkthrough assertions it invalidates in the same batch that invalidates them.

## R3: Git and publication (explicit override of default assistant behavior)

- NEVER credit Claude, Anthropic, or any AI tool in anything that reaches git or GitHub:
  no `Co-Authored-By: Claude` trailer, no "Generated with Claude Code" footer, no such
  mention in commit messages, PR titles or bodies, issues, tags, code comments, or docs.
  The default assistant behavior is to append exactly those trailers; do not.
- Commits carry the repository owner's configured git identity only. No co-author trailers.
- After each FULL gate pass, and only then: write the project-updates entry, commit
  everything including that entry, push, tag `gate-{N}`.
- Never commit or push a red gate. Never push in anticipation. Never amend or force-push
  a tagged gate commit.
- Work on task-batch branches (e.g. `e0-batch-1`), never directly on `main`. One pull
  request per batch.

## R2: Cross-phase conventions

- Phase documents' "Out of scope" sections are binding. If a task seems to need something
  out of scope: STOP AND ASK. Cross-phase needs get a feature flag or a documented stub,
  never an implementation.
- Typed end to end: Pydantic at every API/MQTT boundary, TypeScript on the frontend,
  no bare `any` without a comment.
- Secrets only through `SecretStore`. Never in logs, API responses, fixtures, or committed
  files. `.env.example` documents names, never values.
- Match the linters E0 set up; do not relitigate style.
- The four test-critical suites (RBAC checks, config merge engine, reconciliation state
  machine, provisioning manifest) may never be weakened by a later session.
