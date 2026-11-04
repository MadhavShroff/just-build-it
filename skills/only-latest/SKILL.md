---
name: only-latest
description: Build the latest design only. Leave no legacy path, shim or fallback for old behaviour. Keep auth, export, erase, backup and restore.
---

# Only the latest design

Build for the design in the current specification. Do not carry the past.

## Do
- Replace the old behaviour. Do not keep it beside the new one.
- Delete code that nothing calls.
- Migrate data once, in a script, and then delete the script.
- Write one way to do each thing.

## Do not
- Do not add a compatibility layer, a feature flag for old behaviour, or a deprecated alias.
- Do not keep a second code path "just in case".
- Do not write a comment that explains what the code used to do.

## Always keep
These are product duties, not legacy:
- authentication and access control;
- export of the user's data;
- erase of the user's data;
- backup;
- restore.

If a change would remove one of the five, stop and open a decision.
