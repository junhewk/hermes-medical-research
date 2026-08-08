# Artifacts and source behavior

## CLI configuration

The companion CLI reads credentials from the local environment. The installed skill does not read
or store them.

- PubMed/PMC: `NCBI_EMAIL` required; `NCBI_API_KEY` optional.
- OpenAlex: `OPENALEX_API_KEY` optional.
- Semantic Scholar: `SEMANTIC_SCHOLAR_API_KEY` or `S2_API_KEY` optional.
- Scopus: `SCOPUS_API_KEY` required; `SCOPUS_INSTTOKEN` optional.

Never include secret values in question JSON, commands, run artifacts, or chat. `manifest.json`
contains only boolean configured/not-configured indicators.

## Query capability differences

- PubMed and PMC preserve validated MeSH headings, title/abstract synonyms, Boolean concept
  groups, publication dates, languages, and publication types.
- PMC is queried through NCBI E-utilities with `db=pmc`; it is not Europe PMC.
- OpenAlex uses free-text search plus supported date/language filters. It cannot preserve MeSH or
  PubMed field tags.
- Semantic Scholar uses free-text Boolean concepts and year-level date filters. Language and
  publication-type filtering is applied only when returned metadata supports it.
- Scopus translates free-text groups to `TITLE-ABS-KEY(...)` and applies publication-year clauses.
  Access depends on the API key's institutional entitlement.

Every loss of query semantics appears in `strategy.json` warnings.

## Run artifacts

- `question.json`: validated framework, concepts, and filters actually used.
- `strategy.json`: exact sensitivity and optional precision queries for every source.
- `preflight.json`: source availability, result counts, and full-retrieval confirmation token.
- `manifest.json`: checkpoints, statuses, redacted configuration, tool version, and errors.
- `sources/<source>.jsonl`: native provider order and source rank.
- `results.jsonl`: deterministic deduplicated order.
- `ranked-results.jsonl`: separate `mdr-v1-generalized` prioritization.
- `summary.json`: retrieval and deduplication counts; not a research report.

Deduplication uses DOI, then PMID/PMCID crosswalks, then normalized title plus year when strong
identifiers are unavailable. Source records and native ranks remain attached to the canonical
record.

Review source queries before execution. If a source is unavailable, configure it or create a new
strategy that explicitly excludes it. Quick mode may finish with omissions, which must be reported
prominently.
