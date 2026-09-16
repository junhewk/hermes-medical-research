# Project context

## Vision

Hermes Medical Research turns a structured medical question into an auditable evidence report. It
uses persistent Hermes bot profiles for semantic work and a deterministic Python boundary for every
stateful or safety-critical operation. No model is trusted to preserve corpus integrity, invent its
own task scope, validate its own citations, or declare the run complete.

## Domain language

- A **Run** is one protocol and its content-addressed artifact history.
- A **Review** is a human-named, immutable protocol plus a schedule and an ordered series of Cycles.
- A **Cycle** is one Run in a Review. One-off Reviews have one Cycle; living Reviews may have many.
- A **Task** is a bounded assignment for exactly one specialist role and one immutable input state.
- A **Packet** is the small, digest-bound view needed to perform a Task.
- A **Proposal** is the only agent-editable staging artifact. It has no authority until accepted.
- A **Receipt** binds an accepted result to its Run, Task, actor profile, session, input digests, and
  immutable result file.
- A **Lease** is a 60-minute, session-bound capability to perform one cron-managed Task. Failed
  Leases retry after 5 and 30 minutes and the third failure blocks the Cycle.
- The **Outbox** carries only completed, no-change, and blocked Cycle notifications to Coordinator.
- The **Corpus** is the search and source material owned by the CLI. Bots access it only through a
  bounded Task packet or paginated `mdr source show` calls.
- The **Candidate** is the frozen pre-audit protocol, workflow provenance, evidence, and synthesis.
- **Coordinator, Searcher, Selector, Extractor, Synthesizer, and Auditor** are isolated Hermes
  profiles. Cron workers claim Tasks from the CLI; the Auditor is independent of every evidence
  author.

## Architectural principles

1. Bots receive only opaque IDs, a short-lived claim token, and bounded artifact paths; the artifact
   store carries all substantive data.
2. The CLI owns schemas, exact coverage, citations, digests, legal transitions, and completion.
3. Task inputs and accepted results are immutable and content-addressed. Stale proposals fail closed.
4. Work is deliberately small: one record, one outcome, or one audit group per Task.
5. Hermes integration uses public profile, skill, terminal, file, and Bot Mode surfaces only.
6. Bootstrap is non-mutating by default and never overwrites an unmanaged or locally edited profile.
7. v0.4 runs may be copied into the shared store, but old host-native review remains provenance only;
   finalization requires a fresh v0.5 independent audit.
8. Hermes cron is an edge Adapter, not the workflow authority. Review, Cycle, Lease, retry, reuse,
   and notification transitions remain deterministic and restart-safe in the CLI core.
9. A living Review replays its accepted search plan. Protocol changes fork a new Review. Exact
   source digests permit reuse; changed metadata or retraction state invalidates affected work.

## System boundary

`hermes_medical_research.tasks` is the deep per-Run Module.
`hermes_medical_research.automation` is the deep cross-Run Review/queue Module. Their narrow
Interface is the `mdr` CLI. `hermes_medical_research.hermes` is the localized true-external Adapter
for profiles and Routines; it does not implement workflow state. Search retrieval and evidence
validation remain internal libraries. Hermes skills describe commands; they do not implement
correctness. The default store is
`$XDG_DATA_HOME/hermes-medical-research` (or `~/.local/share/hermes-medical-research`) and can be
isolated with `MDR_HOME`.
