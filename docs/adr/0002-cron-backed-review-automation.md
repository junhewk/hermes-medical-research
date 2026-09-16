# ADR 0002: Cron-backed durable Review automation

- Status: accepted
- Date: 2026-09-16
- Supersedes: ADR 0001's Bot-to-Bot message routing and managed-roster readiness gate

## Context

Direct Bot messaging still left a human or a long-lived Coordinator turn responsible for relaying
opaque identifiers. Hermes Routines are the better persistent surface: they survive chat sessions,
show their cadence in Bot Mode, and can use script gates to avoid idle inference. Hermes scheduling
alone cannot guarantee exactly-once work, bounded retries, stale-result rejection, or living-review
reuse, so it cannot be the workflow authority.

## Decision

Add a human-named **Review** containing an immutable protocol, schedule, and ordered **Cycles**. Each
Cycle owns one existing Run. A deep automation Module above `TaskEngine` owns a durable FIFO queue,
60-minute capability **Leases**, three bounded attempts with 5/30-minute backoff, coalesced schedule
fires, append-only digest-linked Review events, and a notification Outbox. Work remains serial inside
one Run and may proceed concurrently across Runs.

Install six base profile-local Hermes Routines: a script-only Coordinator tick and one gated agent
worker for each specialist. A living Review adds one script-only Coordinator Routine at its real cron
cadence. Profile timezone is authoritative. Routine installation is dry-run by default, creates jobs
only through Hermes's public CLI, refuses unmanaged collisions or drift, and never pins or rewrites a
user's model, provider, reasoning effort, or Hermes pause state.

Workers call `mdr ROLE claim`; they do not receive work through chat. Every managed read or mutation
requires the returned token and originating session. Expired or replaced work fails closed. The
Coordinator receives only completed, no-change, and blocked notifications and acknowledges them.

Living Cycles replay the first accepted search plan. Retrieved records merge cumulatively using DOI,
PMID, PMCID, then source/source-ID identity. Digest-identical screening decisions receive immutable
reuse receipts; changed source metadata is rescreened. An exactly unchanged corpus produces an
immutable checkpoint referring to the predecessor report and skips downstream inference. Changed
Candidates always require a fresh audit.

## Consequences

- `message_agent` and Bot roster metadata are no longer workflow dependencies.
- Human operation uses Review names; Run/Task/claim IDs stay diagnostic and worker-facing.
- Hermes is a replaceable scheduling Adapter. Core state remains replayable without Hermes.
- Script gates make stable idle periods cost zero model calls after Hermes establishes the baseline.
- Pausing a Review prevents future Cycles but does not abort its active Cycle. Multiple fires during
  an active Cycle coalesce into one catch-up Cycle.
- Adopted standalone Runs remain valid, but recurring refresh requires a fork with a complete request.
