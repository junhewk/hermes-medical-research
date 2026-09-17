# Validation status

## Deterministic qualification

The v0.5 cutover is covered by the repository test suite. Its new architecture cases verify:

- creation and resolution of opaque Run and Task IDs inside an isolated artifact root;
- one-record Selector routing, bounded source access, exact target coverage, and CLI-recorded state;
- idempotent replay from a fresh Selector session;
- rejection of extra records, wrong roles, changed packets/results, and stale submissions;
- independent Auditor identity, exhaustive finding/report targets, quote/locator validation, and
  immutable audit receipts;
- copy-on-import v0.4 migration and provenance archiving;
- dry-run Hermes bootstrap, clean profile contents, least toolsets, automatic Bot Mode discovery,
  unmanaged-profile refusal, and locally edited-file refusal;
- human-named Review/Cycle creation, stable zero-idle-cost probes, session-bound claim tokens,
  bounded 5/30-minute retry and third-attempt blocking, and notification Outbox state;
- dry-run cron fleet planning with six base jobs and a real-cadence job for each living Review;
- atomic search reservation/task linkage, crash-safe reservation repair, immutable materialized
  plans, the report-mode biomedical-index and zero-hit gates, and explicit optional-source outages;
- anonymous and API-key PubMed access, explicit any/all concept semantics across query compilation
  and ranking, and regression coverage for alternative intervention families;
- an eight-turn Searcher profile, one-command search execution, immediate one-article Selector
  continuation through fresh serial host sessions, absolute Routine entrypoints, targeted profile
  bootstrap, managed Routine editing through Hermes's public CLI, and auditable active-Cycle
  cancellation;
- frozen living-review strategy validation, cumulative canonical corpus merge, source-digest reuse
  receipts, changed-source invalidation, and predecessor-linked no-change checkpoints;
- relevance-ranked screening order after corpus attachment (0.5.7);
- per-outcome decisions (0.5.8): scaffolds bound to each protocol outcome, rejection of undecided
  outcomes and of extractions bound to unreported outcomes, scaffold pruning, all-unreported
  records, routing that reopens a record without decisions, synthesis linkage by
  `protocol_outcome`, disposition audit targets and corrections, legacy-run warnings, bounded
  `source find`/`source read` access, and an end-to-end audited export with three findings;
- orchestration (0.5.8): continuation into the next role without a tick, paused Reviews that are
  unclaimable and never abandoned, in-place `review retry` that keeps partial proposals, runner
  failure handling that does not stop the queue, deterministic full-text submission, per-record
  audit groups with screening batches, content-bound audit reuse after a changed record, correction
  halts, and routine pause controls through Hermes's public cron CLI;
- the retained retrieval, evidence, appraisal, synthesis, verification, and export behavior.

Run locally with:

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run pytest
uv build
```

The 0.5.10 local run on 2026-09-17 collected and passed 217 tests; Ruff, `git diff --check`, wheel
construction, source-distribution construction, and a wheel-only `mdr` smoke test also passed. The
final count is updated with each release commit.

## Hermes operational gates

Hermes itself is not available in every development environment. Operational qualification therefore
has two explicit gates and preserves its JSON output:

1. In an isolated Hermes home, bootstrap the six profiles, apply the managed cron fleet, and run two
   fresh Selector host sessions against one synthetic article each. Each worker must claim its
   Task, inspect only its bounded source, submit one decision, and leave an accepted receipt.
2. Only if both Selector workers pass, run three clean end-to-end Reviews through the cron queue.
   Every Cycle must complete through Searcher, Selector, Extractor, Synthesizer, and Auditor.
3. Qualify one living Review with a changed refresh and one exact no-change refresh, verify coalescing,
   pause/resume, Outbox acknowledgement, retry/backoff, and gateway-restart recovery.

The harness is `scripts/qualify_hermes.py`. A failure of the first gate is a stop condition: do not
weaken the CLI boundary or fall back to private delegated children.

### Current Hermes result (2026-09-15)

On `jkworkstation`, Hermes Agent 0.21.2 accepted the public creation of all six isolated profiles.
The earlier two-session Selector terminal gate passed: both fresh chats returned the assigned `run_id` and
`task_id` with state `accepted`, and the harness independently confirmed one accepted receipt in each
Run. The retained artifact is
`/tmp/hermes-medical-research-v0.5-codex-20260915/selector-qualification-2.json` on that host.

On 2026-09-16, the six profiles were also installed into the live `~/.hermes` registry on
`jkworkstation`, used by Hermes Agent 0.21.3 and the macOS remote gateway. Bot Mode discovered them automatically, and a
manual Selector task submitted through the macOS Bot roster produced one accepted CLI receipt. This
result qualifies terminal access but predates ADR 0002. The cron fleet, multiplexed gateway, three
full Reviews, and living-review gates have not yet been qualified.

### Extractor qualification (2026-09-16, 0.5.7)

A bounded linked Run on `jkworkstation` acquired three full texts and recorded eight extraction and
appraisal pairs. A separate reviewer verified all 8 extraction quotes, 31 appraisal quotes, locators,
and numerical transcriptions, but found that one trial's Mini-CEX and satisfaction outcomes were never
extracted. That gap motivated the 0.5.8 outcome-decision contract. The run is retained unchanged as
provenance; it is not a qualification of 0.5.8.

### 0.5.8 host smoke test (2026-09-17)

In an isolated store on `jkworkstation`, the three extractor-qualification articles were seeded
with screening, coverage, and study links, and the 0.5.8 Extractor runner was started. The runner
acquired all three full texts deterministically without a model session and routed straight into
the first assessment. The session followed the per-outcome procedure: it searched every protocol
outcome with `source find` and read the exam paragraph and both Mini-CEX and satisfaction tables
with `source read`. It then spent most of 30 turns reading installed package source to learn allowed
field values, and single model turns took 10 to 60 seconds. The attempt was stopped before
submission. These findings produced 0.5.9: packet `field_rules`, and claim renewal during long
sessions.

### 0.5.9 host smoke test (2026-09-17)

The same seeded store was rerun on 0.5.9. The Extractor session read `field_rules` instead of
package source and decided all seven protocol outcomes for PMID 42700004: knowledge test score,
Mini-CEX clinical skills, and satisfaction were extracted, which closes the qualification gap;
cognitive load was marked not reported with eight inspected locations; and one untouched scaffold
was pruned. The first submission was rejected for one missing `effect.missing_reason`, which
cascaded into eight misleading dependent-stage errors; the session patched the row and the second
submission was accepted in one attempt, after about 32 minutes with a shared model endpoint.
Review of the stored rows found a chi-square statistic recorded as a between-group effect and a
discussion remark extracted as an outcome. These findings produced 0.5.10.

The production refresh Cycle for `ai-med-ed-evidence-report-v4` reused 30 of 46 prior screening
decisions; the other 16 differed only in citation counts and source result positions, which 0.5.10
treats as volatile for future refreshes.

## Repository and release record

On 2026-09-17 the Hermes line split from the Claude Code/Codex plugin line. GitHub
`junhewk/medical-deep-research-plugin` was renamed `junhewk/hermes-medical-research`; the 0.4.0
plugin line remains on branch `legacy/claude-codex-0.4` and tag `v0.4.0`. Releases are Git tags
installed with `uv tool install` or `pipx install` from the repository. There is no PyPI or
plugin-ZIP release in this design. The cron fleet, multiplexed gateway, three full Reviews, and
living-review gates above remain open until recorded here.
