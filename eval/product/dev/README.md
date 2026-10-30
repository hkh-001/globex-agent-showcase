# Public product evaluation development set

Only these 12 development tasks are public. This directory contains no independent holdout body or oracle. Coverage is modification 4, hard constraints 2, long history 2, empty/switch 1, orders 3. D01 has exactly 14 checks; D02 exactly 15. `manifest.json` freezes catalog/task hashes and family identifiers. Rebuild with `.venv/bin/python -m scripts.evaluation.build_dev` before freezing; changed tasks require a new manifest/version and new experiment batch, never overwrite a measured batch.

`tasks.json` is an external evaluation asset, including oracle checks. It must never be passed to an agent/model. Harness sends only one frozen user message at a time; template IDs bind actual visible ordered product cards, eligible SKU and evidence. No unseen raw search IDs substitute for missing products. The private set is separately authored and custody-audited; this development author has not accessed it.

## Run safely

```
.venv/bin/python -m scripts.evaluation.harness --output /tmp/globex-dev-dry-unique
.venv/bin/python -m scripts.evaluation.harness --output /tmp/globex-dev-offline-unique --execute-offline
.venv/bin/python -m pytest tests/evaluation/test_scoring.py -q
```

Default is dry/not_run: all task/check denominators remain. Offline execution uses real AgentScope tools and isolated SQLite, but a recorded fixture is not model efficacy. Color/material/history reasoning unsupported by that fixture may fail; do not remove the failed checks or call these formal task-success scores. Real execution is programmatic only, denied unless a separately authorized global persisted evaluation gate is installed on the client by the main runner; smoke authorization is not formal evaluation authorization.

Each task/strategy/repeat has an exclusive directory and DB. External user selection only chooses eligible displayed SKUs. Confirmation/approval/rejection is an actual trusted Store action, never model text. Mutations and expiry are frozen external test actions. Restart restores the same task's DB. A timeout/failure preserves the original planned denominator and all existing audit/provider events. No automatic retry.

Free-text facts are deliberately unscored until source-grounded human review. `observations.json` contains visible answers and actual batch bindings. Reviewer adds `facts[fact_id]` with value, supported, reviewed=true and evidence_refs pointing to the emitted `_evidence` map; historical facts must reference the correct batch/SKU/time. Unknown is truthful only when evidence confirms the field is unknown; it does not satisfy a verified-known fact. The scorer rejects unsupported self-attestation, missing facts and made-up evidence. Additional concrete assertions need full human annotation, not LLM judging. A zero-assertion truthfulness score is N/A and cannot imply task success.

`results.jsonl` per task obeys RunResult, while `scoring.json` contains immutable-denominator scores; manifest includes executed steps/error/mode. Call/event logs retain failures and unknown provider usage. Formal effect claims remain unverified until separately authorized real runs and full manual review are complete.

## V1 review correction: evidence, unknown and task gate

Historical/current separation requires the cited B1 reference and the actual current-query reference, same SKU, their individually matching prices and distinct observation-step/time provenance. Two arbitrary references or an externally marked `supported=true` cannot override conflicting source fields. Current-query captures record their `evidence_ref` explicitly. Human review remains necessary.

Honest unknown handling is reported separately as `honest_unknown_handled`: an allowed unknown check can pass, while `fact_coverage.supported_answered` remains zero and the original planned denominator remains. Only a supported non-null value counts as verified fact coverage.

Every frozen check remains a required task requirement. `critical` is a failure-severity flag only; setting it false does not turn a missing required fact into an optional diagnostic. The scorer reports this gate policy explicitly. No existing public task requirement or gold has been relaxed in this correction.

Actual user turns include frozen clarification responses. A shared task-level budget counts every submitted user action and every model reservation across turns and runtime restarts (maximum 60 calls, maximum 600 seconds; stricter per-turn/global authorization limits still apply). Reaching the limit prevents the next action/request. `actions.jsonl` preserves the initial clarification, finite response and result; manifests preserve branch counts and budget counters. Failed and not-reached checks retain their planned denominator. This runtime restart guarantee concerns a task's controlled restart step; the CLI never resumes or overwrites an existing output directory.
