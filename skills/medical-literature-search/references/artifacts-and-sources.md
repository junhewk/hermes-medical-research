# Artifacts and source behavior

## CLI configuration

The companion CLI reads credentials from the local environment. The installed skill does not read
or store them.

- PubMed/PMC: `NCBI_EMAIL` required; `NCBI_API_KEY` optional.
- OpenAlex: `OPENALEX_API_KEY` optional.
- Semantic Scholar: `SEMANTIC_SCHOLAR_API_KEY` or `S2_API_KEY` optional.
- Scopus: `SCOPUS_API_KEY` required; `SCOPUS_INSTTOKEN` optional.

Never include secret values in question JSON, commands, artifacts, or chat. `manifest.json` contains
only configured/not-configured indicators.

## Query capabilities

- PubMed and direct PMC preserve MeSH, title/abstract fields, dates, and nested Boolean groups.
- OpenAlex preserves nested Boolean groups. It submits resolved MeSH as free text and lacks PubMed
  field tags.
- Semantic Scholar review mode uses bulk search to preserve `+`/`|` groups. Quick relevance search
  accepts plain text only, so the CLI submits every canonical group text and records degradation.
- Scopus preserves nested groups in `TITLE-ABS-KEY(...)`, translates MeSH to free text, and applies
  publication-year clauses. Access depends on institutional entitlement. The STANDARD view stops
  paginating at 5000 records, so a larger result set is marked `truncated` in the manifest.

Language filters compare normalized ISO 639-1 codes, so a filter written as `english` matches
PubMed's `eng`, OpenAlex's `en`, and the English name alike. Records whose provider reports no
language, or reports it as undetermined, are retained rather than dropped.

MeSH resolution validates each `candidate_mesh` entry and the group's canonical text against NCBI,
and accepts a descriptor only when it shares real vocabulary with the candidate. A heading derived
from the canonical text rather than an explicit candidate is recorded as a strategy warning,
because it widens the PubMed query; re-plan with `--no-mesh` to drop it.

Each provider strategy contains structured `degradations` with `feature`, `reason`, and `fallback`.
Review these alongside general warnings before approval.

## Review state and approval

Review manifests progress through `awaiting_strategy_approval`, `strategy_approved`,
`preflight_ready` or `preflight_failed`, `running`, and `complete` or `failed`.

`approval.json` binds approval to the exact strategy digest and selected per-source variants. An
all-results confirmation additionally records the preflight digest, expected total, and timestamp.
This is an audit record of the user-confirmed action, not an authenticated signature.

Review preflight requires strategy approval. Review search requires a successful digest-bound
preflight and never runs preflight implicitly. Editing a strategy invalidates the run; revise the
structured question and create a new run instead.

## Run artifacts

- `question.json`: normalized schema-v2 groups and filters actually used.
- `strategy.json`: exact sensitivity and precision queries, selected variants, and degradations.
- `approval.json`: digest-bound review and all-results confirmations.
- `preflight.json`: source access, counts, strategy/preflight digests, and confirmation token.
- `manifest.json`: state, checkpoints, redacted configuration, tool version, and errors.
- `sources/<source>.jsonl`: native provider order and source rank. `source_rank` is the record's
  position in the provider's own result list, so client-side filtering leaves gaps.
- `results.jsonl`: deterministic deduplicated order.
- `ranked-results.jsonl`: separate `mdr-v2-grouped` prioritization with per-group relevance. Each
  record carries `ranked_as_of`; recency is scored against the strategy's creation date, not the
  wall clock, so resuming or re-running a run reproduces the file byte for byte.
- `summary.json`: retrieval and deduplication counts; not a research report. `records_by_source`
  counts retained records and `records_filtered_by_source` counts records the provider returned
  that a language or publication-type filter then dropped. Report a non-zero filtered count:
  a source that retrieves records and retains none is not an empty source.

Deduplication uses DOI, then PMID/PMCID crosswalks, then normalized title plus year when strong
identifiers are unavailable. Source records and native ranks remain attached to the canonical record.

Quick mode may finish with omissions and must report them prominently. Review mode stops before
retrieval when any selected source is unavailable; failures during retrieval retain partial artifacts
and mark the run failed.
