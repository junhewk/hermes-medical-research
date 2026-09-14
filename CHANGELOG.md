# Changelog

## Unreleased

- Known issues in 0.4.0 from full-report qualification (see VALIDATION.md): the native review
  packet presents author-tagged outcome overlap mappings as proven, so reviewers can accept a
  review-level trial list as outcome-pool membership (all three Hermes reports failed on this or
  related premises); limitations, coverage reasons and appraisal rationales are exported
  without review; and the rule that record titles are not citable led reviewers to remove true
  study-design labels. Codex and Claude synthetic reports had no false premise; the live-source
  Codex report passed.

## 0.4.0 — 2026-09-14

- Add source packets, exact source lookup, atomic related-stage updates, aggregate checks and resumable finalization.
- Record estimate basis, comparator/interval types, harms availability and explicit appraisal completion.
- Add ROBIS, detailed-assessment coverage and a separate digest-bound host claim-review stage.
- Lead reports with findings and limitations; export flat estimates plus findings, appraisal and coverage tables.
- Preserve legacy runs with explicit limits; update all hosts to the shared v2 report workflow.
- Native review robustness: reviewers get an explicit citation contract and example in the
  frozen packet, may write one result file per finding, read their output back, and validate it
  with `research review-check` (Codex/Claude) or `medical_research_review_check` (Hermes) before
  stopping. Codex/Claude reviewers that stop with an invalid result receive the exact problems
  for at most two metered correction rounds.
- Receipts persist `failure_reason`/`failure_detail`; `research check`, `status` and `next`
  return `native_review`; hooks surface the latest review state to the author as tool context
  (Codex shows `systemMessage` only to the user) and block one premature stop on Claude.
- Hermes: path-based reviewer tool policy with instructive messages (the host's tool discovery
  helpers stay available), progress-bearing status
  polls with `wait_seconds` up to 300, acceptance decided by the validated artifact rather than
  the loop's exit shape, and no `None` output limit written onto the child.
- Add fail-closed `research review-abandon` / Hermes `action: "abandon"` for reviews whose host
  never reported termination; it charges the entire reservation and never produces a pass.
- Complete the Hermes setup instructions with `skills.external_dirs` so the installed report skill
  appears in the startup index and loads as `medical-deep-research` or `/medical-deep-research`.
- Extend Hermes integration checks to verify those user-facing surfaces and references after a
  same-process configuration change. Qualified plugin registration remains tested separately.


## 0.3.1

- Fix a Hermes Plugin Guard false positive in a URL-rejection test by using a harmless temporary
  file fixture. The local-file rejection remains tested; scanner rules and policy remain unchanged.
- Add a pinned Hermes integration job covering the full-source installation scan, disabled state,
  explicit enabling, and fresh-process listing/reading of both skills and their references.
- Document Hermes process restart, qualified skill names, and the difference between Plugin Doctor,
  installed/enabled status, and the running chat's skill registry.
- Make report examples invoke the skill explicitly and state per-report settings and defaults.

## 0.3.0

- Rename the search project to Medical Deep Research Plugin. Keep the legacy CLI alias and
  schema-1/2 search compatibility; the separate desktop repository remains unchanged.
- Add portable, Codex, Claude Code, and Git marketplace manifests with two shared skills.
- Add broad EBM schema-3 frameworks, explicit sensitive search components, Europe PMC,
  ClinicalTrials.gov, DOI verification, and bounded citation chaining.
- Add resumable protocols, shared budgets, source locations, screening, study links,
  provisional appraisals, and outcome-level synthesis records.
- Export cited Markdown/HTML, JSON/CSV evidence artifacts, selection counts, and RIS.
  Block unknown source locations, stale assessments, and identity mismatches.
- Preserve full-text XML tables and PDF page locations; disclose inaccessible/OCR-dependent texts.
- Add headless validation, deterministic integration tests, and a reproducible release ZIP builder.

## 0.2.x

Grouped PICO/PCC search, database-specific approval, preflight, resumable retrieval,
native provenance, deduplication, and transparent triage ranking.
