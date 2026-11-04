---
name: ci-lane
description: Verify and merge a finished task on a pinned base. Prove the tested tree is the merged tree. Cache results by their inputs.
---

# CI lane

A lane takes one finished task at a time.

## Steps
1. Pin the base: record the base commit SHA before you start.
2. Make a temporary worktree at the pinned base.
3. Squash-merge the task branch into that worktree. Record the head SHA of the branch.
4. Run the checks for the rigor level. Stop at the first failure.
5. Prove the tree: record the tree hash of the tested worktree. After the merge, the merged commit must have the same tree hash.
6. Re-read the real base. If it moved, rebuild the combined tree on the new base and run the checks again.
7. Advance the base only with a compare-and-swap on the pinned SHA.

## Cache
Key a result by base SHA, head SHA and the check set. The same key never runs twice. Reuse the stored result.

## Rules
- Never merge a tree you did not test.
- Never rebase or amend the task branch in the lane.
- Report a failure with the check name and the last lines of output.
- Remove the temporary worktree on every exit path.
