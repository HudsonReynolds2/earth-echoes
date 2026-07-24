# Project Changes

Numbered, reverse-chronological, append-only log of changes to scope, sequencing, task
definitions, or acceptance criteria relative to the planning documents (rule R1,
`.claude/rules/project-rules.json`). Every entry names an addendum that exists in the
referenced planning document; an entry with no addendum is incomplete.

## #4 (2026-07-23): E0.8 spec citation corrected

- **What changed:** The phase document's E0.8 task cites "spec 12.1" as the source of the
  audit-log requirement; section 12.1 covers tenancy. The requirement actually derives from
  spec section 14.1 (immutable audit log of every mutation) and section 13 (`GET /audit`).
- **Why:** Later sessions follow section references literally; an incorrect citation sends
  them to the wrong requirement.
- **Affects:** project_planning/phase-0-foundations.md section 4 (E0.8)
- **Addendum:** PHASE0-4-02

## #3 (2026-07-23): Session inputs are three documents, not two

- **What changed:** Project plan section 5 says each implementation session receives "exactly
  two inputs: spec v1.1 and the phase document." The implementation handbook (section 2), the
  later and operational document, specifies three inputs plus one conditional: the spec, the
  phase document, the current `docs/INTERFACES.md`, and `docs/DECISIONS.md` once it has
  content. The handbook's list governs.
- **Why:** The two documents disagree; sessions must know which list to follow.
- **Affects:** project_planning/echoes-of-earth-project-plan.md section 5
- **Addendum:** PLAN-5-01

## #2 (2026-07-23): E0.12 Seed script missing from the project plan

- **What changed:** Project plan section 3 lists eleven E0 tasks (E0.1 through E0.11). The
  phase document defines twelve; E0.12 (dev seed creating the initial owner account,
  credentials printed once, fresh environment to logged-in owner in one command) exists only
  in the phase document. The Jira board needs the extra story.
- **Why:** Task inventory drift between the plan and the phase document would surface as a
  missing story on the board.
- **Affects:** project_planning/echoes-of-earth-project-plan.md section 3 (E0)
- **Addendum:** PLAN-3-01

## #1 (2026-07-23): Task E0.0 (governance scaffolding) added before E0.1

- **What changed:** A task E0.0 precedes E0.1: binding machine-readable project rules
  (`.claude/rules/project-rules.json`), the `CLAUDE.md` loader, the three project logs
  (`docs/project-updates.md`, `docs/project-changes.md`, `docs/DECISIONS.md`), the
  `docs/INTERFACES.md` skeleton, the git baseline of `project_planning/`, the commit message
  template, and the gate runner. E0.0 ends in Gate 0, subject to rule R0 like every task.
- **Why:** The project is built as fresh scope-limited sessions (handbook section 2); the
  governing rules and the running record must live in the repository rather than in any one
  conversation, and they must exist before the first build task.
- **Affects:** project_planning/phase-0-foundations.md section 4
- **Addendum:** PHASE0-4-01
