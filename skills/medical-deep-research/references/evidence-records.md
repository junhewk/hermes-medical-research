# Evidence records and packets (v2)

Use `research next RUN` to create `packet.json` with source context and an editable `input.json`.
It leaves judgments blank/pending on purpose. Read and fill the template; do not write a builder
program. The command returns file paths and exact supported commands. It preserves an edited
input file if the same packet is requested again.

## Batch contract

```json
{"schema_version":"2","base_digests":{"records":"digest from packet"},"stages":{"screening":{"schema_version":"2","records":[]}}}
```

Keep the complete `base_digests` returned by the packet. Each keyed row merges by its stable ID;
synthesis replaces the complete synthesis object. Submit through `research record RUN --batch
--input FILE`. All supplied stages are validated together before the manifest changes. A failed
batch commits nothing. Existing evidence revisions remain immutable. Fetch a fresh packet after
other changes; never patch a digest just to bypass stale-work protection.

`research check RUN --input FILE` previews a batch and reports both input errors and remaining
workflow work. It is normal for early stages to have readiness errors for later missing stages.
The `record` response's `accepted` field means the submitted records are valid, not that a report
is complete. `research record RUN --stage STAGE --input FILE` still replaces a whole stage.

## Screening, coverage and study links

Screening: `record_id`, `decision` (include/exclude/uncertain), `basis` (title-abstract/fulltext/registry),
`reason`. Use fulltext basis only with an acquired full text. Screen every retrieved record.

Coverage: `record_id`, `selection` (selected/deferred/unavailable), `reason`, `protocol_outcomes`
(array of exact names from the protocol; may be empty for context). Cover every included record.
Select by relevance, methods, important outcomes, contradictory findings and harms. A deferred
paper remains eligible. A selected record needs extraction; document genuinely unavailable results
as unavailable instead of leaving selected work unfinished.

Studies: `study_id`, `record_ids`, `kind` (primary/systematic-review/guideline/other), `basis`.
Link reports using identity evidence such as registrations. Do not merge merely similar titles.
Each record belongs to one study group. Use existing IDs when extending a group.

## Extractions

The packet supplies all common fields. Each extraction has an `extraction_id`, `record_id`,
`study_id`, `population`, `comparison`, `outcome`, `timepoint`, `sample_size` (number or null),
`result`, `comparator_type`, `outcome_type`, `effect`, `favors`, `direction_rationale`,
`source_location`, `support_checked`, and `support_rationale`.

- `comparator_type`: inactive, active_exercise, active_other, mixed, none, not_applicable.
- `outcome_type`: benefit, harm, context.
- `effect.basis`: between_group, within_group, group_summary, association, diagnostic_accuracy,
  ranking, qualitative. Preserve the actual estimate basis even when the study has a comparator.
- `effect`: measure, basis, value, ci_low, ci_high, units, interval_type, interval_level, missing_reason.
  Numerical fields use finite numbers or null. A missing value requires missing_reason. Interval type
  is confidence/credible/none; a reported interval requires both bounds and its percentage level
  (e.g. 95). With none, both bounds are null. Qualitative results have null numeric values.
- `favors`: intervention/comparator/neither/uncertain/not-applicable. Nonsignificance does not establish
  equivalence. Rankings use uncertain or not-applicable; SUCRA is not clinical superiority.
- `source_location`: document_id, locator, quote. Copy a short exact passage with enough context to
  support the result. The quote must occur at that location and belong to the record. Check semantic
  support before setting support_checked to true and explaining support_rationale.

For harms, also supply:

```json
{"harms":{"reporting":"monitored_without_counts","attribution":"Monitoring described, but arm-specific event counts are unavailable.","arms":[]}}
```

Reporting is counts/monitored_without_counts/not_reported. With counts, each arm has name, event,
events, denominator and missing_reason when a number is unavailable. Counts may be descriptive;
a comparator-arm event is not an intervention event. Unreported events must never be encoded as zero.

## Appraisals

Use [appraisal.md](appraisal.md) and `research methods` for the selected instrument/version/domain keys.
Each extraction needs its own appraisal. Each appraisal has extraction_id, method, method_version,
overall (prose), overall_judgment, rationale, completion and domains.

- `overall_judgment`: low/some_concerns/high/unclear/not_assessable/descriptive.
- Each domain has status (pending/assessed/unavailable), judgment, rationale and source_locations.
  Assessed judgments require quotations. Pending/unavailable domains use judgment `not_assessed`.
- An unavailable domain also needs missing_reason (access_unavailable/not_reported/insufficient_detail),
  assessment_basis and inspected_locations. Each inspected location has document_id and locator.
  Access unavailable requires a recorded full-text attempt and no stored full text. Not reported or
  insufficient detail requires actual inspected locations. Read available methods first.
- `completion`: pending if any domain is pending; otherwise limited if any domain is unavailable;
  otherwise complete. Pending/limited appraisals use overall_judgment unclear or not_assessable.
  They cannot claim completed low-risk assessments. A favorable abstract is not a methods appraisal.

## Synthesis

Synthesis uses schema_version, title, findings and limitations (an array of explicit report limits).
For each finding include the common scope fields, finding_id, conclusion, protocol_outcomes,
claim_basis, comparator_type, evidence, certainty and overlap.

`claim_basis`: comparative/within_group/association/diagnostic_accuracy/ranking/context/gap.
`protocol_outcomes` names exact protocol outcomes. Each outcome must appear in at least one finding.
For a gap, supply gap_reason and gap_basis explaining the attempted search/assessment. Evidence
may be empty for gaps; certainty must be not-assessable or descriptive/not-applicable.

Each evidence contribution has:

```json
{"extraction_id":"result-id","relationship":"supports","use":"direct","weight_rationale":"Why this result informs the conclusion.","alignment_rationale":"How population, comparator, outcome and timepoint align.","claim_support_checked":true}
```

Relationships: supports/contradicts/mixed/incomparable/context. Use: direct/indirect/context.
Context-only data use relationship context or incomparable. Comparative claims require between-group
estimates for substantive contributions; within-group changes, group summaries and rankings belong
in separately labeled findings or context. Different comparator types cannot be direct evidence.
Do not call a BP-variability result evidence about mean BP, or a secondary outcome a primary outcome.

`overlap` has status (not_applicable/mapped/suspected/unknown) and rationale. Mapped overlap also lists
study_ids with recorded identity evidence. Suspected overlap is not independent corroboration.
Never sum participants across overlapping reviews.

`certainty` has origin report_assessment, framework GRADE-informed/descriptive, rating and rationale.
GRADE-informed also needs starting_point, rating_explanation and all five domains: risk_of_bias,
inconsistency, indirectness, imprecision, publication_bias. Ratings: high/moderate/low/very-low/
not-assessable. Descriptive maps use not-applicable. Missing methods do not mechanically imply low
certainty. If no substantive contributor has a completed appraisal, use not-assessable.

Published ratings remain separate in optional `published_certainty`: an array of extraction_id,
rating, scope and source_location. Do not present source-author GRADE as the report's own assessment.
Historical recommendations and preprints remain explicitly dated/labeled context.

## Separate claim review and completion

Record synthesis before requesting its review packet. A review batch cannot contain synthesis.
The packet supplies a review_digest tied to each finding and its evidence. Review every finding,
including gaps, against its quoted sources and appraisals. Each review has finding_id, review_digest,
status pass/revise and checks for estimates, scope, harms, overlap and certainty. Each check has
status pass/revise/not_applicable and a substantive rationale. Never fill all checks mechanically.
A required revision prevents a pass. Changed claims/evidence require new review digests and review.

`research finalize RUN [--input LAST_REVIEW_BATCH]` checks readiness, performs online citation identity
checks and exports. Missing domains may justify a qualified report only after actual inspection;
pending work, unsupported conclusions, stale stages and identity mismatches block completion.
`--offline` explicitly skips online identity checking and is disclosed in the report.

Outputs include Markdown, HTML, JSON, evidence/screening/studies/findings/appraisals/coverage CSVs,
selection counts and RIS. `completion.json` contains their paths, checksums and current dataset digests.
`resume.json` locates the most recent packet. Neither file claims independent medical validation.

## Legacy runs

Schema-v1 workspaces remain readable with status and can use their original single-stage commands.
Their exports explicitly state that v2 semantic fields and separate claim reviews were not validated.
Do not silently upgrade or fill missing evidence meanings. Initialize a separate v2 run for deliberate
re-assessment and preserve the original protocol, files and provenance as the historical record.
