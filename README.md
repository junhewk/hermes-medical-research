# Hermes Medical Literature Search

A portable Agent Skill and deterministic CLI for reproducible medical-literature retrieval.
It structures PICO/PCC questions, creates transparent database-specific strategies, searches
PubMed, PMC, OpenAlex, Semantic Scholar, and optionally Scopus, then preserves native and
deduplicated MDR-ranked results. It does not generate research reports and is not an MCP server.

## Install the skill

Hermes:

```bash
hermes skills tap add junhewk/hermes-medical-search
hermes skills install junhewk/hermes-medical-search/skills/medical-literature-search
```

Other Agent Skills-compatible clients can install the
`skills/medical-literature-search` directory. The skill invokes the CLI from the matching GitHub
release with `uvx`; no server remains running.

## CLI development

```bash
uv sync --extra dev
uv run hermes-medical-search --help
uv run pytest
```

Configure `NCBI_EMAIL` for PubMed/PMC. Optional credentials are `NCBI_API_KEY`,
`OPENALEX_API_KEY`, `SEMANTIC_SCHOLAR_API_KEY` (or `S2_API_KEY`), `SCOPUS_API_KEY`, and
`SCOPUS_INSTTOKEN`. Secrets are read only by the CLI and are never written to run artifacts.

Create a strategy:

```bash
uv run hermes-medical-search plan question.json --mode review --limit-per-source 100
```

Quick mode defaults to 20 results per source and the previous three years:

```bash
uv run hermes-medical-search run question.json
```

The score in `ranked-results.jsonl` is a transparent prioritization heuristic. It is not GRADE,
risk-of-bias assessment, evidence quality, or a systematic-review conclusion.
