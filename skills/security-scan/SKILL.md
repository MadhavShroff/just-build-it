---
name: security-scan
description: Scan a diff for injection, secrets, authentication flaws and unsafe patterns. At the highest rigor, audit dependencies too.
---

# Security scan

## Steps
1. List the entry points the diff adds or changes: routes, commands, file reads, subprocess calls.
2. Trace each input to where the code uses it. Look for:
   - injection into a shell, a query, a path or a template;
   - a path that escapes its directory;
   - a missing authentication or origin check on a state-changing request;
   - a secret in code, a log line, a command line or a file;
   - unsafe deserialisation and unbounded input size.
3. Search the diff for secrets: keys, tokens, passwords, private key headers.
4. For an audit, list the new or changed dependencies. Check each for a known advisory and for an unpinned version.
5. Report each finding with a file, a line, the attack and the fix.

## Report form
- Severity: HIGH, MEDIUM, LOW.
- Verdict: PASS when no HIGH or MEDIUM finding remains.

## Rules
- Do not edit code. Report only.
- Never print a found secret in the report. Print its file and line.
- A finding needs a concrete input that triggers it. Mark a guess as INFERRED.
