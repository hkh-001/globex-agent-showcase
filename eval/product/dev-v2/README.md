# Explicit public development schema migration

This directory is a new v2 asset/batch identity. The original `../dev/tasks.json` remains byte-identical, task hash `35db66f946237b37d8024fed914152e58d682d3b69455e5665c27ad9cb11dbb6`. New task hash: `46d62cc3be0d04c323f22112b6c4005cf142686e871fa7225d6bfb4a36ff9d42`.

`manifest.json` maps all12 original task IDs/source hashes to target hashes and original check IDs. No task, mandatory check, expected value or external user action was removed, selected again or simplified. Only schema version and empty/default v2 extension fields changed. Old observations and reports are not re-scored or overwritten. Formal comparisons must use one explicit protocol/version for all three strategies and new exclusive output directories; previous v1 results cannot be mixed into a v2 table.

The migration does not invent missing user-source annotations or extra fact targets. The original12 scripts retain their original check coverage; this is not a claim that they exhaustively demonstrate every G01–G06 capability. New protocol capabilities are validated by anonymous synthetic contract cases in `tests/evaluation/test_protocol_v2.py`, including actual AgentScope/Store tool and transaction tests. Independent sealed tasks are adapted only by their author after review, without exposure or deletion of their required semantics.

No real API or formal evaluation was run to create these files. Offline smoke/pilot is contract validation, not model efficacy. If inherited source script/oracle semantics prove incomplete, disclose the gap and version a public correction before any formal run, rather than silently weaken checks or use private outcomes to tune this dataset.
