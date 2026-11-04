---
name: review-diff
description: Review one diff for correctness and quality in one round. Add a second round only for tier-2 surfaces.
---

# Review a diff

## Scope
- One round checks correctness and quality together.
- A second round runs only for a tier-2 surface: a public contract, a data format, authentication, or a change that crosses services.
- Do not start a third round. Open a decision instead.

## Steps
1. Read the brief. Read the diff against the pinned base.
2. Check correctness first: wrong results, missed cases, races, failed error paths, broken contracts.
3. Check quality next: names, duplicated logic, dead code, unclear interfaces, missing tests.
4. Run the acceptance command and the changed tests.
5. Report each finding with a file, a line, the defect and the fix.

## Report form
- Severity: BLOCK (must fix), FIX (should fix), NOTE (optional).
- Give a verdict: APPROVE when no BLOCK remains, else CHANGES.

## Rules
- Report every actionable finding in the round. Do not hold some back for the next round.
- Do not edit code. Findings go back to a new implementer.
- Do not report style that a formatter fixes.
