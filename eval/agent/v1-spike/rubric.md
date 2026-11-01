# Stage 0 exit rubric

Stage 0 passes only when all checks below are reproducible with zero external
model requests.

- Agent-originated tool events contain `correlation.agent`.
- Child-agent tool events contain a unique `dispatch_id`.
- Two deliberately interleaved workers do not exchange dispatch ownership.
- Real EventBus output from the interleaved workers passes the graph scorer.
- Error and cancellation restore the parent execution scope.
- A worker exception emits one terminal `status=error` dispatch event and is
  scored as `task_failed`, not `evidence_invalid`.
- Direct-main and dispatched-search paths both satisfy the search contract.
- A forbidden edge fails.
- Removing `correlation.agent` returns `evidence_invalid`.
- Each transaction run uses a fresh data directory and repository assembly.
- Repeated transaction runs have identical initial-state hashes.
- State snapshots and prohibited side effects are machine-checkable.
- Missing `usage.summary` is unknown usage, never zero model calls.
- The subprocess socket guard observes zero external connection attempts and
  its source hash is recorded in the manifest.
- Task specifications are validated but explicitly marked `tasks_executed=false`.
- Evidence binds source, task and fixture hashes.

Failure of any check blocks bulk Core/Ablation task generation.
