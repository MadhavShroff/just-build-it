---
name: review-diff
description: Review one diff in read-only mode for correctness and quality. Add a second round only for tier-2 surfaces.
---

# Review a diff

## Scope
- One round checks correctness and quality together in read-only mode on `git diff`.
- A second round runs only for a tier-2 surface: a public contract, a data format, authentication, or a cross-service change.
- Do not start a third round. Open a decision instead.

## Steps
1. Read the brief. Read `git diff` against the pinned base.
2. Check correctness first: wrong results, missed cases, races, failed error paths, and broken contracts.
3. Check quality next: names, duplicated logic, dead code, and unclear interfaces.
4. Inspect the test changes in `git diff` to verify coverage. Do not run test or build suites in the shared worktree.
5. Report each finding with the file, the line, the defect, and the fix.

## Report form
- Severity: `BLOCK` (must fix), `FIX` (should fix), `NOTE` (optional).
- Return `CHANGES` only when at least one `BLOCK` defect exists.
- Return `APPROVE` with notes when only `FIX` or `NOTE` findings exist.
- Return `APPROVE` when no findings exist.

## Rules
- Keep the review strictly read-only on `git diff`. Do not edit files.
- Do not execute test suites, build commands, or the acceptance command in the shared worktree.
- Report every actionable finding in one round. Do not hold findings back for a later round.
- Do not report style issues that a formatter fixes.
