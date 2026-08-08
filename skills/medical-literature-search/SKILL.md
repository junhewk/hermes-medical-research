---
name: medical-literature-search
description: Structures PICO/PCC questions, compiles transparent database-specific strategies, and retrieves reproducible medical-literature result sets from PubMed, PMC, OpenAlex, Semantic Scholar, and configured Scopus. Use when a user asks for medical or biomedical article discovery, PICO/PCC search planning, MeSH-aware database queries, multi-database evidence retrieval, or auditable review-search artifacts without report generation.
---

# Medical Literature Search

Use the pinned, on-demand CLI. Do not start an MCP server:

```bash
uvx --from git+https://github.com/junhewk/hermes-medical-search.git@v0.1.0 hermes-medical-search --help
```

Read [question-schema.md](references/question-schema.md) when converting a question to JSON. Read
[artifacts-and-sources.md](references/artifacts-and-sources.md) when selecting sources, explaining
query limitations, resuming work, or interpreting artifacts.

## Choose the workflow

- Use `quick` for bounded discovery when the user does not request a review-grade strategy.
- Use `review` for systematic/scoping-review preparation, exhaustive retrieval, or whenever the
  user wants to inspect and approve database queries.
- Never describe either workflow as a completed systematic review. This skill retrieves and
  prioritizes records; it does not screen studies, assess bias, apply GRADE, or generate a report.

## Structure the question

1. Preserve the user's original question verbatim.
2. Choose PICO for an intervention/exposure question; choose PCC for a scoping or concept/context
   question. Ask the user only when that choice materially changes the intended search.
3. Create a version-1 question JSON. Supply concise synonyms and candidate MeSH headings; never
   invent identifiers or claim candidate headings are validated.
4. Keep comparison/outcome (PICO) and context (PCC) as optional precision concepts. The default
   search deliberately uses only Population + Intervention or Population + Concept for recall.

## Run a quick search

Run from the user's chosen working directory:

```bash
uvx --from git+https://github.com/junhewk/hermes-medical-search.git@v0.1.0 hermes-medical-search run question.json
```

Quick mode defaults to 20 retained records per source and a visible three-year date bound. It
continues when one provider fails. Report the run directory and every omitted or failed source.

## Run a review search

1. Require an explicit per-source limit or `all`.
2. Generate the strategy without searching:

```bash
uvx --from git+https://github.com/junhewk/hermes-medical-search.git@v0.1.0 hermes-medical-search plan question.json --mode review --limit-per-source 100
```

3. Show the user `strategy.json`, including exact source queries, applied filters, selected
   sensitivity/precision variant, and degradation warnings. Do not continue until the user
   approves it.
4. Run `preflight <run-dir>`. Stop if any selected or auto-configured source is unavailable.
5. If the limit is `all`, show the per-source counts and expected total. Obtain explicit approval,
   then pass the emitted token as `search <run-dir> --confirm-all TOKEN`.
6. Otherwise run `search <run-dir>`. Re-running the same command resumes checkpointed sources.

## Apply source and credential rules

- Search PubMed, direct NCBI PMC, OpenAlex, and Semantic Scholar by default.
- Include Scopus automatically when its CLI credentials are configured; honor an explicit Scopus
  exclusion.
- Run `doctor --json` when credentials or institutional Scopus entitlement are uncertain.
- Never request that a user paste a secret into chat. Direct them to configure the documented
  environment variable locally, then rerun `doctor`.
- Do not silently replace direct PMC with Europe PMC or Google Scholar with Semantic Scholar.

## Present results

- Treat `sources/<source>.jsonl` as the auditable native-order retrieval record.
- Treat `results.jsonl` as the deduplicated, unranked record set.
- Treat `ranked-results.jsonl` as optional prioritization. Always state that
  `mdr-v1-generalized` is a heuristic and contains no journal-prestige bonus.
- Use `summary.json` and `manifest.json` for counts, failures, filters, timestamps, tool version,
  and source status. Do not turn these artifacts into an unsupported medical conclusion.
