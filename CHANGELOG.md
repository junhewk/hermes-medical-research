# Changelog

## Unreleased

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
