# Echoes of Earth Platform: Implementation Handbook

**Purpose:** How to run implementation sessions against the document set. Read this once before starting Phase 0, and give the relevant parts to anyone joining the project.

---

## 1. The document set

- **Technical Specification v1.1.** The single source of truth for what the platform is. Authoritative in every session. Amend it (with a version bump) rather than letting sessions drift from it.
- **Project Development Plan v1.0.** The epics, tasks, and dependency ordering. Source for the Jira board. Not handed to implementation sessions except when re-planning.
- **Phase documents (one per epic E0 through E8, plus SIM).** Each scopes one implementation session series. Phase 0, 1, and 2 exist now; write each later one as its predecessor nears completion, folding in what actually got built.
- **`docs/INTERFACES.md` (lives in the repo).** The growing contract between phases: every phase reads it first and appends what it owns. This file is what lets a fresh conversation continue without breaking earlier work.
- **`docs/DECISIONS.md` (lives in the repo).** Deviations from the spec or phase documents, with rationale and date. Feed these back into the next spec or phase-doc revision.

## 2. Running an implementation session

Give a new conversation exactly three inputs: the spec, the phase document, and the current `INTERFACES.md` (plus `DECISIONS.md` once it has content). Attach or paste relevant existing code when the phase touches it. Then use a kickoff prompt along these lines:

```
You are implementing Phase N of the Echoes of Earth management platform.

Attached:
1. Technical Specification v1.1. Authoritative. Section references in the
   phase document point here.
2. The Phase N document. Your scope. Implement its task list and nothing
   outside it.
3. docs/INTERFACES.md (current). Contracts owned by earlier phases. Do not
   change these without flagging the change to me explicitly first.
4. [docs/DECISIONS.md, relevant existing code, or repo tree as needed.]

Rules:
- The "Out of scope" section of the phase document is binding. If a task
  seems to need something out of scope, stop and ask.
- Every mutation endpoint uses the audit hook. Every endpoint uses the RBAC
  dependency. Secrets only ever move through SecretStore.
- Follow the migration conventions (append-only, reversible).
- End the session by updating INTERFACES.md with what this phase now owns,
  updating DECISIONS.md if anything deviated, and listing the handoff
  artifacts from the phase document.

Start with a short implementation plan for my approval before writing code.
Work task by task in the phase document's order unless you propose and I
approve a different order.
```

Sessions within one phase can be split by task group (for example, one session for E0.1 through E0.5, another for auth and RBAC). When splitting, hand the later session the same three inputs plus the repo state, and say which tasks are already done.

## 3. Cross-phase conventions

These apply to every phase and are restated here so no phase document has to repeat them.

- **Scope discipline.** The most likely failure mode of session-per-phase development is a session helpfully building a neighboring phase's feature, badly. The "Out of scope" sections exist to stop that. When a genuine cross-phase need appears, the answer is a feature flag or a stub with a documented contract, not an implementation.
- **Feature flags over blocking.** Where the plan notes a cross-epic wait (E2.6 apply-without-publish behind `EOE_PUBLISH_ENABLED`; E4.6 bootstrap block flagged until E5.6 lands), build behind the flag rather than idling or reaching into the other epic.
- **Tests as the contract.** Spec 14.5 names the merge engine, the reconciliation state machine, the provisioning manifest, and the RBAC checks as test-critical. Their test suites double as documentation, and no later session may weaken them.
- **Typed end to end.** Pydantic at every API and MQTT boundary, TypeScript on the frontend, no `any` escapes without a comment (spec 14.5).
- **Secrets.** Only through `SecretStore`. Never in logs, API responses, fixtures, or committed files. `.env.example` documents names, never values.
- **Formatting and style.** Match the linters E0 sets up; do not relitigate style per session.

## 4. Universal definition of done (per phase)

In addition to the phase document's own definition of done:

1. CI green: lint, typecheck, tests, migration up/down, container builds.
2. Every new mutation audits; every new endpoint enforces RBAC.
3. `INTERFACES.md` updated; `DECISIONS.md` updated if anything deviated.
4. The demo fixture still seeds and the compose stack still starts from clean.
5. Handoff artifacts from the phase document exist and are named in the closing summary.

## 5. Jira setup notes

Create one Jira epic per plan epic, one story per task, carrying the task IDs as labels. Encode the cross-epic task dependencies listed in plan Section 4 as issue links (Blocks/Is blocked by); the epic-level ordering can stay as documentation. Add the four gating spec decisions (spec Section 17 items 1, 3, 7, 14) as separate decision tickets linked as blockers to E4.2, E3.7, E4.5, and E5.6 respectively, so the open questions are visible on the board rather than buried in the spec.

The DES track's tasks go in as a parallel epic with no code dependencies except DES.7 (blocked by DES.4, DES.5, and E0.4) and DES.6 linked as a non-blocking input to E2.8, E4.10, and E5.12.

## 6. Suggested first moves

1. Create the repo and the Jira board from the plan.
2. Run a Phase 0 session (tasks E0.1 through E0.5), then a second for E0.6 through E0.12.
3. In parallel, start DES.1 and DES.2 with the design group; deliver the E0.4 token namespace to them as soon as it exists.
4. When E0 closes, run Phase 1, and draft the Phase 3 document (E3 and E2 run in parallel after E1, so its document is needed at the same time as Phase 2's, and the two sessions must agree the `config_revision` shape early; Phase 2's document already records the default if E2 goes first).
