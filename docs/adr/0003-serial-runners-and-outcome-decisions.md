# ADR 0003: Serial runners for every role and per-outcome decisions

- Status: accepted; worker runners and the three-clean-run cron gate superseded by ADR 0004
- Date: 2026-09-17
- Supersedes: ADR 0002's gated agent workers for Searcher, Extractor, Synthesizer, and Auditor;
  ADR 0001's consequence that the repository is renamed only after three full runs

## Context

A bounded extractor qualification on the Hermes host recorded one extraction for a trial that also
reported two other protocol outcomes. An independent review caught the gap, but nothing in the CLI
required a decision for each outcome, and the Synthesizer would have turned the omission into an
evidence gap. The same qualification showed the agent-prompt cron workers fumbling argument order,
paging whole documents with ad hoc Python, looping on validation errors, and trying to edit their own
managed skill. Only the Selector used a serial host runner, and it aborted its whole queue on one
failed article.

Several queue rules also stalled multi-bot runs: an operator-paused Routine blocked its Cycle after
95 minutes with no in-place recovery; non-Selector Tasks waited for a minute tick; a retried Task
whose proposal had been edited could never be reopened; every audit correction invalidated every
audit receipt; and final checks rejected any audited report with more than one finding.

## Decision

Every assessed record carries a `dispositions` row that marks each protocol outcome `extracted`,
`not_reported`, or `not_applicable`. Extraction rows bind to `protocol_outcome`. The CLI rejects an
assessment that leaves an outcome undecided, prunes untouched scaffolds, links synthesis evidence by
the binding, audits each disposition row against the full text, and exports the decisions. The
contract is a manifest marker outside the protocol, so Review protocol digests are unchanged. Runs
that recorded extractions before the marker keep validating with an explicit warning.

Every worker Routine is a script-only `mdr hermes drain --role ROLE` runner. It claims one Task,
starts one fresh Hermes session with an instruction file of exact commands, and fails an unfinished
Task through the durable queue instead of stopping. Full-text acquisition is submitted without a model.
`mdr source find` and `mdr source read` give sessions locator-level access to tables and results.

Accepting a Task routes the next one immediately. Paused Reviews are unclaimable and never abandoned.
`mdr review retry` reopens a blocked Cycle in place. Report audits are packed per record, receipts bind
to each target's assertion and sources, corrections handle one group at a time, and a group unresolved
after two corrections halts the Run for an operator.

The repository split from the Claude Code/Codex plugin line on 2026-09-17: GitHub
`junhewk/medical-deep-research-plugin` became `junhewk/hermes-medical-research`, with the 0.4.0 plugin
line preserved as branch `legacy/claude-codex-0.4` and tag `v0.4.0`.

## Consequences

- One model session covers one Task for every role, with per-role turn ceilings in managed profiles.
- Hermes cron snapshot drift no longer affects workers, because scripts start sessions from the live
  profile configuration.
- Audit volume scales with assessed records rather than with every stored row, and a correction
  re-audits only changed groups. The audit contract version is 2; earlier receipts need a fresh audit.
- Operators recover from worker outages without discarding accepted work.
- Three clean full Hermes runs remain the qualification gate for declaring the cron fleet qualified,
  but no longer gate the repository name.
