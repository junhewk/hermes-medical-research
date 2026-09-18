# Changelog

## 0.6.0 - 2026-09-18

- Replaced the cron bot fleet with user-invoked steps. Work now advances only when the operator runs
  one step, and a step claims and answers one stage's items until that queue is empty. On the real
  170-record review the bot design took about 90 seconds per screening record and about 15 minutes
  per extracted article, and 9 hours did not finish it, because every item was a multi-turn agent
  session that read an instruction file, hand-edited `proposal.json`, and shelled out to a submit
  command. One tool-free call per record screened all 170 records in 20.2 minutes, 7.1 seconds mean,
  and agreed with the bot Selector on 152 of the 163 answers it could parse.
- Made every step start a detached worker and return one line. A `type: exec` Hermes quick command
  is killed after 30 seconds and receives no arguments, on the CLI and the messaging path alike, so
  nothing about the work can be passed in: the active Review is `<store>/steps/active.json`, set by
  `hmr step use NAME`, and progress, failures, and the next command live in
  `<store>/steps/<review>/<step>/status.json`, which `hmr step status` reads.
- Added `hmr hermes commands`, which installs one quick command per step into the host Hermes config
  as a marked region owned by a digest manifest: `/hmr-search`, `/hmr-selector`, `/hmr-extractor`,
  `/hmr-synthesizer`, `/hmr-auditor`, `/hmr-status`, `/hmr-next`, `/hmr-stop`, `/hmr-finalize`, and
  `/hmr-retry`. The config is backed up once, re-parsed after the edit, and restored if anything
  outside the managed entries changed.
- Answered screening, coverage, study linking, and synthesis with one constrained tool call instead
  of a session. The submit tool takes a single `result` object because llama.cpp compiles a real
  grammar only for non-string tool arguments, and a string enum counts as a string: `result` with
  `decision` and `reason` inside it binds, while a flat `decision` enum does not. Enforcement also
  needs `tool_choice: required`, pinned in the profile's provider `extra_body`, because under `auto`
  the tool grammar stays lazy until the model opens a tool call. In the measured screening run 7 of
  170 answers were prose rather than JSON, which is why the shape is now constrained rather than
  requested.
- Chose a tool payload over `response_format`, measured on the operator's own gateway. A schema in
  `response_format` works only without tools: the gateway returns HTTP 400 for tools plus
  `response_format`, and llama-server drops the schema when tools are present. A JSON-Schema
  `parameters` block composes with tools and is also how tools work on OpenAI, vLLM, SGLang, and
  Ollama, so the design does not depend on one server.
- Turned reasoning off in a constrained call, because llama.cpp ignores a schema while thinking is
  enabled.
- Made the lane of each Task kind deterministic. `call` covers screening, coverage, study linking,
  and synthesis, each of which decides one packet. `agent` covers per-outcome assessment, audit, and
  every audit correction, which must read arbitrary full text; a correction re-mints the same packet,
  so a deterministic call would repeat the rejected answer. `none` covers search and full-text
  acquisition, which call no model at all. Synthesis falls back to `agent` when its packet was
  shortened to fit, because its rows are then summaries. A `call` gets two constrained attempts and
  then one session with tools before the durable failure path runs.
- Kept authority exactly where it was. The schemas buy shape only: every value, identifier, digest,
  and cross-field rule is still checked by `validation` and `evidence` when the mapped proposal
  reaches `TaskEngine.submit`, a rejection comes back to the model as the validator's own message,
  and record, study, and finding ids, protocol outcomes, schema versions, base digests, and
  certainty origin are never taken from a payload. The schemas are protocol-independent because a
  profile's `extra_body` is fixed at bootstrap time.
- Installed nine managed profiles. `hmr-screen`, `hmr-cover`, `hmr-link`, and `hmr-finding` host one
  stdio MCP submit tool each, carry no skill and no shell toolset, and cap their answer tokens;
  `hmr-coordinator`, `hmr-selector`, `hmr-extractor`, `hmr-synthesizer`, and `hmr-auditor` keep
  their skill and tools. `hmr-searcher` is retired and removed, because the search step runs no
  session. `hmr hermes profiles` creates, checks, and removes them and records a per-kind model and
  lane in `<hermes_home>/hmr-steps.json`.
- Renamed the CLI, the managed profiles, the managed manifests, and the store environment variable
  from `mdr` to `hmr`, because `mdr` is the separate medical-deep-research line. `MDR_HOME` is still
  honored and `mdr-*` actor profiles in existing receipts still resolve to their role, so old runs
  stay readable.
- Changed three queue rules the step model forces. A step waits out the 5-minute and 30-minute retry
  backoff instead of mistaking a backed-off Task for an empty queue. Abandonment blocking after 95
  minutes is deleted, because an unclaimed Task between two slash commands is now normal. An
  interrupted runner releases its claim on the next start without spending one of its three
  attempts, and a step claims only inside the Review it was pointed at.
- Retired cron routine creation, the serial drain, `work probe`, `work cron-tick`, `hermes drain`,
  and the `medical-search` skill, whose step now runs no session. `hmr hermes routines` is
  removal-only, so a host that still has the fleet can be migrated; a Routine claiming on a timer
  would race the operator's own step runner.
- Served read-only manifest views from a stat-keyed cache. A submission validated every row against
  a freshly loaded and deep-copied manifest, so on the production copy one appraisal validation took
  119 seconds and held the Run lock throughout, which timed out every other claim and crashed the
  extractor runner. The same validation now takes 0.11 seconds, and a busy Run lock is retried five
  times at 30-second intervals instead of ending a runner.

## 0.5.11 - 2026-09-17

- Seeded assessment proposals with one full `study-appraisal-...` template and `same_as` outcome
  rows that copy it at submission; a separate full appraisal is needed only when an outcome's risk
  of bias differs. In the 0.5.9 smoke test the Extractor spent about 13 of 32 minutes writing seven
  near-identical appraisals into a 49 KB proposal.
- Rejected per-outcome assessments whose extracted rows still have pending appraisals, so an
  accepted record is not immediately reopened.

## 0.5.10 - 2026-09-17

- Stopped cascading validation errors: when one stage in a submission fails, stages that depend on
  it report a single deferred note instead of errors echoing the upstream failure. In the 0.5.9
  host smoke test one missing `effect.missing_reason` produced eight misleading errors.
- Rejected test statistics and P values as effect estimates in per-outcome runs, and stated in
  field rules and the Extractor skill that discussion or limitation remarks are not measured
  outcomes. The smoke test recorded a chi-square statistic as a between-group effect and extracted a
  discussion remark as an outcome.
- Ignored citation counts and source result positions when deciding whether a screening decision
  can be reused in a refresh Cycle. They blocked reuse of 16 of 46 decisions in the production
  refresh without changing anything a screener judges.

## 0.5.9 - 2026-09-17

- Renewed worker claims every 10 minutes while a serial runner's Hermes session is still working,
  and allowed 75 minutes per session attempt. On the Hermes host a single model turn can take 30
  seconds or more, so an assessment session could otherwise outlast its 60-minute claim lease.
- Stated every allowed value and cross-field rule in assessment and synthesis packets as
  `field_rules`, built from the validators' own constants. In the 0.5.8 host smoke test the
  Extractor followed the new outcome procedure but spent many turns reading package source code
  to learn the schema; the skills now point to `field_rules` and forbid reading source or help text.

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
