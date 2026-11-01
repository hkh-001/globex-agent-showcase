# Stage 0 exit rubric

Stage 0 passes only when all checks below are reproducible with zero external
model requests.

- Agent-originated tool events contain `correlation.agent`.
- Child-agent tool events contain a unique `dispatch_id`.
- Two deliberately interleaved workers do not exchange dispatch ownership.
- Error and cancellation restore the parent execution scope.
- Direct-main and dispatched-search paths both satisfy the search contract.
- A forbidden edge fails.
- Removing `correlation.agent` returns `evidence_invalid`.
- Each transaction run uses a fresh data directory and repository assembly.
- Repeated transaction runs have identical initial-state hashes.
- State snapshots and prohibited side effects are machine-checkable.
- Evidence binds source, task and fixture hashes.

Failure of any check blocks bulk Core/Ablation task generation.
