# Evidence record contracts

All stage files have `schema_version: "1"`. Submit a complete replacement via
`research record RUN --stage STAGE --input FILE`; revisions remain stored by digest.
View data with `research status RUN --stage STAGE`. This also identifies stale data.

For `screening`, `studies`, `extractions`, and `appraisals`, use `{ "schema_version": "1", "records": [...] }`.
Identifiers are nonempty strings. Use the CLI's record/document IDs; choose stable study, extraction,
and finding IDs. All source quotes must occur in the named document segment, and belong to the
extracted record. Report exports block unknown identifiers, stale assessments, and identity mismatches.

## Screening and study links

```json
{"record_id":"r-...","decision":"include","basis":"title-abstract","reason":"Matches the population and comparison in the protocol."}
```

Decisions: `include`, `exclude`, `uncertain`. Basis: `title-abstract`, `fulltext`, `registry`.
Full-text screening requires an acquired full text. Cover every retrieved record before export.

```json
{"study_id":"trial-1","record_ids":["r-...","r-..."],"kind":"primary","basis":"Both reports explicitly identify the same trial registration NCTxxxxxxxx."}
```

Kinds: `primary`, `systematic-review`, `guideline`, `other`. Each record belongs to at most one
study group. Multiple follow-up reports can share a study but have distinct extractions.
For an unidentified single study, create one group and explain its identity evidence; do not merge
similar titles or suspected overlapping cohorts without support. Explain unresolved overlap in synthesis.

## Extractions

```json
{
  "extraction_id":"trial-1-sbp-12w",
  "record_id":"r-...",
  "study_id":"trial-1",
  "population":"Adults with hypertension",
  "comparison":"Exercise versus usual care",
  "outcome":"Systolic blood pressure",
  "timepoint":"12 weeks",
  "sample_size":null,
  "result":"Write only the result supported by the source passage.",
  "effect":{"measure":"mean difference","value":null,"ci_low":null,"ci_high":null,"units":"mmHg","missing_reason":"The available passage does not report a numerical estimate."},
  "favors":"uncertain",
  "direction_rationale":"An effect cannot be inferred from the information available.",
  "source_location":{"document_id":"r-...:abstract","locator":"abstract","quote":"Exact text copied from this segment."},
  "support_checked":true,
  "support_rationale":"Explain why the passage supports this extraction, including its scope."
}
```

Use actual numbers only when reported. Preserve the effect measure, denominator/population, unit,
adjustment status in `result`, and follow-up; use null for missing numbers with `missing_reason`.
`favors`: `intervention`, `comparator`, `neither`, `uncertain`, `not-applicable`. Specify the endpoint's
direction in the rationale. Nonsignificance alone is not evidence of equivalence or no effect.

Documents are generated from retrieval or `research fulltext`. Available kinds are `abstract`,
`metadata`, `registry`, `fulltext`. Only stored result-bearing documents may support extractions;
metadata alone is insufficient. XML locators include `paragraph:1` and `table:1`; PDF locators use
`page:1`. Inspect source tables for spanning cells and PDF extraction errors. If no text can be
extracted, the CLI records an OCR-required failure; it does not silently invent a transcript.

## Appraisals

```json
{
  "extraction_id":"trial-1-sbp-12w",
  "method":"descriptive",
  "method_version":"1",
  "overall":"Insufficient information for a formal risk-of-bias judgment",
  "rationale":"Only an abstract is available.",
  "domains":{
    "limitations":{"judgment":"not_assessed","rationale":"Study methods are unavailable.","source_locations":[]},
    "applicability":{"judgment":"not_assessed","rationale":"Population details are unavailable.","source_locations":[]}
  }
}
```

Use the design-appropriate method from [appraisal.md](appraisal.md), or `descriptive` when a formal
instrument cannot be applied; explain the limitation. `research methods` lists exact domain keys.
For an assessed domain, provide its judgment, rationale, and source locations with quotes. Every
domain required by the selected method must appear. Mark missing information `not_assessed`.

## Synthesis

Synthesis has `title`, `findings`, and `limitations`, rather than `records`:

```json
{
  "schema_version":"1",
  "title":"Medical Deep Research: exercise and hypertension",
  "findings":[{
    "finding_id":"sbp-12w",
    "population":"Adults with hypertension",
    "comparison":"Exercise versus usual care",
    "outcome":"Systolic blood pressure",
    "timepoint":"12 weeks",
    "conclusion":"Write the supported synthesis and its uncertainty.",
    "evidence":[{
      "extraction_id":"trial-1-sbp-12w",
      "relationship":"context",
      "weight_rationale":"Limited contribution because an effect estimate is unavailable.",
      "claim_support_checked":true
    }],
    "certainty":{
      "framework":"GRADE-informed","rating":"not-assessable",
      "starting_point":"The study design cannot be established from the available evidence.",
      "domains":{
        "risk_of_bias":"Not assessable from the available methods.",
        "inconsistency":"Insufficient comparable results.",
        "indirectness":"Population and intervention details are incomplete.",
        "imprecision":"No estimate and confidence interval are available.",
        "publication_bias":"The available search and study data are insufficient."
      },
      "rating_explanation":"No numerical downgrade/upgrade calculation is justified.",
      "rationale":"Available evidence cannot support an outcome-level certainty judgment."
    }
  }],
  "limitations":["Explain important search, access, appraisal, and synthesis limitations."]
}
```

Relationships: `supports`, `contradicts`, `mixed`, `incomparable`, `context`. If any population,
comparison, outcome, or timepoint differs, add `alignment_rationale`. Do not average incompatible
measures, count significant studies as votes, or add participants across overlapping cohorts.

GRADE-informed ratings: `high`, `moderate`, `low`, `very-low`, `not-assessable`. Supply all five
domains, starting point, and downgrade/upgrade explanation. Scoping maps can use
`{"framework":"descriptive","rating":"not-applicable","rationale":"Descriptive evidence map."}`.

Each contributing extraction needs an appraisal, including explicit missing-information judgments.
Each cited record is generated from stored metadata; do not write a second bibliography manually.
The CLI independently reports the access level of every contribution, remaining unassessed included
records, source failures/truncation, and verification status. The renderer escapes external text.
