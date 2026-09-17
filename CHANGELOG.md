# Changelog

## 0.5.8 - 2026-09-17

- Made assessment completeness deterministic. Each selected record now needs a `dispositions` row
  that marks every protocol outcome `extracted`, `not_reported`, or `not_applicable`, with inspected
  locations for unreported outcomes. Extraction rows bind to `protocol_outcome`; submissions that
  leave an outcome undecided are rejected and routing reopens a record without its decisions.
- Seeded one prefilled extraction/appraisal scaffold per protocol outcome, an `outcome_checklist` of
  likely source locations, and deterministic pruning of untouched scaffolds.
- Linked synthesis evidence through `protocol_outcome`, listed unreported records in synthesis
  packets, bounded synthesis packet size, audited disposition rows against full text, and exported
  `dispositions.csv` with a per-outcome decision table. Runs that recorded extractions before the
  contract keep validating with an explicit warning.
- Added `mdr source find` and `mdr source read` for locator-level access to tables and results, and
  accepted global options after the subcommand.
- Replaced the agent-prompt workers with script-only serial runners for every role
  (`mdr hermes drain --role`). Each Task gets one fresh Hermes session with an instruction file of
  exact commands and a per-role turn ceiling. An unfinished Task is failed with backoff instead of
  aborting the queue, and full-text acquisition is submitted without a model session.
- Routed the next Task immediately after any acceptance, closed claim records on acceptance, and
  pinned the resolved `mdr` executable in every returned command.
- Made paused Reviews unclaimable and exempt from abandonment blocking, added `mdr review retry` to
  reopen a blocked Cycle in place, and let a retried Task keep its partial proposal.
- Packed report audits per record with screening-only batches, bound receipts to target content so
  unchanged groups keep them, corrected one audit group at a time, and halted a Run whose group stays
  unresolved after two corrections. The audit contract version is now 2.
- Fixed final readiness checks rejecting every audited report with more than one finding.
- Added `mdr hermes routines --status`, `--pause-all`, and `--resume-all`, and reported paused
  Routines in doctor.
- Moved request-schema and recovery guidance into the managed Coordinator profile and told every
  profile never to edit skills or run scripts.

## 0.5.7 - 2026-09-16

- Preserve relevance-ranked search order when attaching a deduplicated corpus so one-article
  Selector tasks start with the highest-priority records instead of source-alphabetic records.
- Anchor attachment ranking to the search artifact's frozen ranking date and keep the detailed
  heuristic scores in `ranked-results.jsonl` as the audit trail.
- Put selected protocol outcomes and coverage into assessment packets, and state explicitly that
  their initial extraction row is a scaffold rather than a one-outcome limit.

## 0.5.6 - 2026-09-16

- Restored the strict one-article Selector task contract after withdrawing the unreleased v0.5.5
  batching experiment.
- Replaced the turn-capped Selector agent Routine with a serial host runner. It starts one fresh
  Hermes Selector session per article and immediately starts the next article after acceptance,
  continuing until the Selector queue is empty.
- Runs that serial worker as a managed no-agent Routine with a 24-hour script ceiling and an
  overlap lock, while each individual Selector session retains a small turn ceiling.

## 0.5.5 - withdrawn

- An unreleased bounded Selector batching experiment. It was reverted before deployment; 0.5.6
  restored one article per Selector task.

## 0.5.4 - 2026-09-16

- Pinned generated Routine scripts to the resolved `mdr` executable so Hermes cron workers do not
  depend on the scheduler's reduced `PATH`.
- Allowed a managed Routine script already matching the new packaged form to be adopted during an
  upgrade while retaining refusal for any other local edit.

## 0.5.3 - 2026-09-16

- Made Review-managed Selector submissions route the next Selector task immediately, removing the
  Coordinator-tick delay between articles while preserving one bounded packet and receipt each.
- Updated the Selector Routine and skill to process up to ten consecutive tasks per invocation and
  raised only that profile's turn ceiling for the bounded loop.

## 0.5.2 - 2026-09-16

- Made bounded PubMed and PMC retrieval available without requiring an NCBI email, while retaining
  optional email/API-key support and accepting `PUBMED_API_KEY` as an alias for `NCBI_API_KEY`.
- Added explicit `operator: any` concept blocks so alternative intervention families compile with
  `OR` and are scored by their strongest matching family; compound concepts remain `operator: all`.
- Taught the Coordinator to distinguish alternative modalities from jointly required concepts.
- Prevented Coordinator-generated AI searches from widening compound interventions with broad,
  independently ineligible MeSH headings.

## 0.5.1 - 2026-09-16

- Fixed an atomicity defect that erased a search budget reservation immediately after creating its
  child search, causing every automated retrieval attempt to fail before database ingestion.
- Added a bounded `search execute` flow, immutable retry plans, an eight-call Searcher limit, and
  fail-fast worker instructions so database retrieval does not become an agent diagnosis loop.
- Added a report-mode biomedical-index gate, zero-hit detection, high-recall Coordinator guidance,
  targeted profile bootstrap, managed Routine updates, and auditable Review cancellation.

## 0.5.0 - 2026-09-15

- Renamed the distribution to `hermes-medical-research`, the import package to
  `hermes_medical_research`, and the sole executable to `mdr`.
- Replaced host-native plugin delegation with six isolated Hermes profiles and four CLI-driven
  skills.
- Added a Run/Task ledger with opaque IDs, bounded packets, exact-scope submissions, content-addressed
  results, stale rejection, idempotent replay, role enforcement, and legal state transitions.
- Replaced native review lifecycle/budget accounting with independent digest-bound audit Tasks and
  immutable receipts for every finding and report assertion.
- Added dry-run-first, ownership-safe Hermes profile bootstrap and automatic Bot Mode discovery
  through public profile and gateway interfaces only.
- Added copy-on-import v0.4 migration that archives old native-review provenance and requires a fresh
  audit.
- Removed Hermes/Codex/Claude plugin manifests, lifecycle hooks, private child construction, legacy
  executables, plugin archives, and host-specific adapters.
- Added the two-session synthetic Selector gate and an optional three-clean-run Hermes qualification
  harness.
- Replaced Bot-to-Bot task relay with human-named Reviews, ordered Cycles, and a cron-backed durable
  queue. Managed work uses 60-minute session-bound claim tokens, 5/30-minute backoff, three bounded
  attempts, fair cross-Run claiming, and a notification Outbox.
- Added dry-run-first Hermes Routine management: six base profile-local jobs plus one true-cadence
  script-only job per living Review, with stable idle probes and ownership/drift checks.
- Added frozen search-plan replay, cumulative refresh identity, source-digest screening reuse
  receipts, and no-change checkpoints for living Reviews.

## 0.4.0

The prior release introduced evidence schema v2, outcome coverage, richer appraisal and synthesis
rules, report-level review targets, and a host-native review adapter. The evidence validation and
retrieval work is retained in v0.5; the host integration is not.
