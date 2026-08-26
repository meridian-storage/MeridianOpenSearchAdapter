<!-- SPDX-License-Identifier: Apache-2.0 -->

# Generation migration hooks

OpenSearch mapping changes are handled through immutable physical generations and atomic aliases.
Platform/Vangu IaC owns the migration state machine; `GenerationManager` supplies deterministic
physical hooks.

1. `plan` validates a strictly newer target and returns the fixed hook sequence and fingerprint.
2. `create` creates the exact target index from the compiled, fingerprinted layout.
3. IaC performs the authoritative scan/backfill and tails the outbox with external source sequence
   ordering. The adapter does not discover or read the authoritative store.
4. `verify` checks the target mapping fingerprint. IaC additionally verifies counts, digests, lag,
   and deployment-specific readiness.
5. `activate` verifies again and changes both private read/write aliases atomically, retaining the
   previous generation number as evidence.
6. `rollback` atomically reactivates an explicitly retained compatible generation.
7. `retire` requires an exact mapping fingerprint, refuses an active generation, verifies the
   mapping again, and deletes only the explicitly named generation.

No hook performs wildcard deletion, autonomous discovery, automatic retirement, provisioning,
state persistence, recovery, or topology changes. A failed cutover leaves orchestration state with
IaC; callers retry or roll back using their persisted migration record.
