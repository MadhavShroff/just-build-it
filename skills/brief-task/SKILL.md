---
name: brief-task
description: Give one task to one fresh agent through a brief file, and take back one receipt. Use for every implementation, plan edit or review task.
---

# Brief a task

One task, one brief, one fresh agent, one receipt.

## Write the brief
1. Write the brief to a file. Keep it at 100 lines or fewer.
2. State the goal in one paragraph.
3. List the files the agent reads. List the files the agent owns and may edit.
4. List the interfaces the agent must match. Quote them exactly.
5. Write the acceptance command. It exits non-zero on failure.
6. State the finish rule: where to write the receipt, and whether to commit.

## Start the agent
1. Start a fresh agent. Do not reuse an agent that has worked on another task.
2. Tell the agent to read only the brief and the files it names.
3. Do not paste the brief into the launch message. Name the file.

## Read the receipt
The receipt is a file of 100 lines or fewer. The first line has a fixed form:

`BRIEF <task> | <commit sha or uncommitted> | PASS or FAIL`

The next sections are DID, PROOF and LEFT.

1. Read the first line. A receipt without that line is a FAIL.
2. Do not trust PROOF. Run the acceptance command yourself (see verify-outside).
3. If LEFT is not empty, decide: new brief, or accept the gap in writing.

## Rules
- Never hand one agent two tasks.
- Never edit a file outside the owned list.
- A task that needs more than 100 lines of brief is too large. Split it.
