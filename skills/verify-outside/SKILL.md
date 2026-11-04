---
name: verify-outside
description: Verify a result from outside the agent that produced it. Show the proving command. Mark every inference.
---

# Verify from outside

The producer of a result cannot verify it. A separate agent or the system verifies it.

## Steps
1. Read the brief and the claim. Do not read the producer's reasoning.
2. Pick a command that proves the claim. Run it in a clean checkout of the branch.
3. Write the command and its exit code in the report. Write the key lines of output.
4. Check that the command can fail. Break the subject on purpose, or read the test, to see it fails.
5. Compare the diff with the owned-file list. A file outside the list is a FAIL.

## Report form
- PASS: the command, the exit code, the lines that prove it.
- FAIL: the command, the exit code, the first failing line, the file to fix.
- INFERRED: a statement you did not run a command for. Mark each one with the word INFERRED.

## Rules
- A claim with no command is INFERRED, not proven.
- Do not edit code. A verifier reports only.
- A test that passes on the broken subject proves nothing. Say so.
- Report a failure plainly. Do not soften it.
