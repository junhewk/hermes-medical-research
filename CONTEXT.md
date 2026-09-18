# Project context

## Vision

Hermes Medical Research turns a structured medical question into an auditable evidence report. An
operator advances the work one step at a time. Each item is answered by one isolated Hermes surface,
and every stateful or safety-critical operation stays behind a deterministic Python boundary. No
model is trusted to preserve corpus integrity, invent its own task scope, validate its own citations,
or declare the run complete.

## Domain language

- A **Run** is one protocol and its content-addressed artifact history.
- A **Review** is a human-named, immutable protocol plus a schedule and an ordered series of Cycles.
- A **Cycle** is one Run in a Review. One-off Reviews have one Cycle; living Reviews may have many.
- A **Task** is a bounded assignment for exactly one specialist role and one immutable input state.
- A **Packet** is the small, digest-bound view needed to perform a Task.
- A **Proposal** is the only agent-editable staging artifact. It has no authority until accepted.
- A **Receipt** binds an accepted result to its Run, Task, actor profile, session, input digests, and
  immutable result file.
- A **Step** is one operator act. It claims and answers one stage's Tasks in the active Review until
  that queue is empty, then stops. Nothing advances a Review unless someone runs a Step.
- A **Published step** is a Step installed as a quick command: search, select, and extract. Synthesis
  and audit remain in the package and on the CLI but are not published, because each takes hours.
- The **Handover** is the point where extraction drains. The Run then holds an estimate, an appraisal,
  and a disposition for every protocol outcome, each with a document, a locator, and a verbatim
  quote, which is what a reviewer verifies and synthesizes from.
- A **Lane** is the surface that answers one item: `call`, `agent`, or `none`. The Lane of a kind is
  deterministic.
- A **Constrained call** is one fresh session with no tools whose profile carries that kind's answer
  schema on the request, so the answer arrives as one shape-checked JSON object. The typed submit
  tools are the session Lane's equivalent, for roles whose work needs tools and so cannot carry a
  schema.
- A **Lease** is a 60-minute, session-bound capability to perform one Task. Failed Leases retry after
  5 and 30 minutes and the third failure blocks the Cycle. An interrupted Step releases its Lease
  without spending an attempt.
- An **Outcome decision** is a `dispositions` row. It records, for one assessed record, whether each
  protocol outcome was extracted, not reported, or not applicable, with the locations inspected.
- A **Halt** stops a Run when an audit group stays unresolved after two corrections; an operator
  clears it with `hmr review retry` or `hmr step retry`.
- The **Outbox** carries only completed, no-change, and blocked Cycle notifications to Coordinator.
- The **Corpus** is the search and source material owned by the CLI. Sessions access it only through
  a bounded Task packet or paginated `hmr source show` calls.
- The **Candidate** is the frozen pre-audit protocol, workflow provenance, evidence, and synthesis.
- **Coordinator, Selector, Extractor, Synthesizer, and Auditor** are isolated Hermes session
  profiles; `hmr-screen`, `hmr-cover`, `hmr-link`, and `hmr-finding` are isolated constrained-call
  profiles. Search and full-text acquisition run no session at all. The Auditor is independent of
  every evidence author.

## Architectural principles

1. Bots receive only opaque IDs, a short-lived claim token, and bounded artifact paths; the artifact
   store carries all substantive data.
2. The CLI owns schemas, exact coverage, citations, digests, legal transitions, and completion.
3. Task inputs and accepted results are immutable and content-addressed. Stale proposals fail closed.
4. Work is deliberately small: one record, one outcome, or one audit group per Task, and one fresh
   Hermes session or one constrained call per item. Nothing batches items, and no surface keeps
   context between items.
5. Hermes integration uses public profile, quick-command, MCP server, skill, terminal, file, and Bot
   Mode surfaces only.
6. Bootstrap is non-mutating by default and never overwrites an unmanaged or locally edited profile.
7. v0.4 runs may be copied into the shared store, but old host-native review remains provenance only;
   finalization requires a fresh v0.5 independent audit.
8. Hermes is an edge Adapter for two things: invoking a session and giving the operator a command to
   type. No model output has authority, whatever shape it arrives in. Review, Cycle, Lease, retry,
   reuse, and notification transitions remain deterministic and restart-safe in the CLI core.
9. A living Review replays its accepted search plan. Protocol changes fork a new Review. Exact
   source digests permit reuse; changed metadata or retraction state invalidates affected work.

## System boundary

`hermes_medical_research.tasks` is the deep per-Run Module.
`hermes_medical_research.automation` is the deep cross-Run Review/queue Module. Their narrow
Interface is the `hmr` CLI. `hermes_medical_research.steps` is the user-invoked runner: it picks the
Lane, claims one item at a time, and owns the status a step reports.
`hermes_medical_research.answers` holds the constrained answer schemas and the prompts built from
them. `hermes_medical_research.mcp_server` is the tool Adapter for the session Lane: it maps one
checked payload into a proposal and through the normal submit path.
`hermes_medical_research.hermes` is the localized true-external Adapter for managed profiles and
session invocation, and
`hermes_medical_research.quick_commands` is the Adapter for the host command entries. Neither
implements workflow state. Search retrieval and evidence validation remain internal libraries.
Hermes skills describe commands; they do not implement correctness. The default store is
`$XDG_DATA_HOME/hermes-medical-research`, falling back to `~/.local/share/hermes-medical-research`,
and can be isolated with `HMR_HOME`.
