## 3. Constructing and Editing DAGs with `jbi.dag_tool`

Every mutating command validates the edited DAG before writing to disk and refuses to write an invalid DAG.

```bash
# 1. Create a new DAG file with an initial task
python3 -m jbi.dag_tool new /tmp/feature-dag.json \
  --project tangram-v1 --repo /home/madhavshroff/tangram-workspace \
  --mode existing --waves auto \
  --id D01 --title "Native table contracts" --acceptance "npx tsc --noEmit"

# 2. Add a task with owned paths and dependencies
python3 -m jbi.dag_tool add-task /tmp/feature-dag.json \
  --id D03 --title "Shared Artifact core" \
  --deps D01 --owns "tangram-backend/nest-backend/src/platform-direct/artifacts/**" \
  --acceptance "node tangram-backend/nest-backend/scripts/verify-launch-artifact-core.mjs"

# 3. Add a dependency edge between existing tasks
python3 -m jbi.dag_tool add-dep /tmp/feature-dag.json --task D03 --dep D01

# 4. Add ordered batch subtasks to a node
python3 -m jbi.dag_tool add-subtask /tmp/feature-dag.json \
  --node D03 --id D03-B1 --batch B1 \
  --title "D01 imports and pure record validation" \
  --owns "tangram-backend/nest-backend/src/platform-direct/artifacts/storage/**"

python3 -m jbi.dag_tool add-subtask /tmp/feature-dag.json \
  --node D03 --id D03-B2 --batch B2 \
  --title "Atomic source publication and Library pointer" \
  --deps D03-B1 \
  --owns "tangram-backend/nest-backend/src/platform-direct/artifacts/artifact-core.service.ts"

# 5. List subtasks on a node
python3 -m jbi.dag_tool list-subtasks /tmp/feature-dag.json --node D03

# 6. Assign explicit wave groups (only when waves are not auto-expanded)
python3 -m jbi.dag_tool set-wave /tmp/feature-dag.json --wave 2 --tasks D03

# 7. Remove a task
python3 -m jbi.dag_tool remove-task /tmp/feature-dag.json --id D03 --prune-deps

# 8. Validate and inspect waves, parallelism, critical path, and ETAs
python3 -m jbi.dag_tool validate /tmp/feature-dag.json
python3 -m jbi.dag_tool show /tmp/feature-dag.json
```

---

