---
name: medical-literature-search
description: Structures simple or compound PICO/PCC questions, compiles transparent database-specific strategies, and retrieves reproducible medical-literature result sets from PubMed, PMC, OpenAlex, Semantic Scholar, and configured Scopus. Use when a user asks for medical or biomedical article discovery, compound concept-group planning, MeSH-aware database queries, multi-database evidence retrieval, or an approval-gated review search without report generation.
---

# Medical Literature Search

Use the pinned, on-demand CLI. Do not start an MCP server:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search --help
```

Read [question-schema.md](references/question-schema.md) when converting a question to JSON. Read
[artifacts-and-sources.md](references/artifacts-and-sources.md) when selecting sources, explaining
degradations, obtaining review approval, resuming work, or interpreting artifacts.

Check access before a first run or after any source failure; it needs no run directory:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search doctor --json
```

## Choose the workflow

- Use `quick` for bounded discovery. Automatically split compound components into required groups.
- Use `review` for systematic/scoping-review preparation or strategy inspection. Never retrieve
  before the user approves the exact grouped, per-source strategy.
- Never call retrieval a completed systematic review. Do not screen, assess bias, apply GRADE, or
  generate a report.

## Structure the question

1. Preserve the original question verbatim and emit schema version 2.
2. Choose PICO for an intervention/exposure question and PCC for a scoping concept question.
3. Represent each component as labeled groups. AND groups together; treat terms inside one group as
   interchangeable OR alternatives. Split technology, task, and other distinct facets into separate
   groups. Do not use PCC Context as a spare topical facet; reserve it for setting or environment.
4. Add only true synonyms to a group. Omit ambiguous bare acronyms unless the user supplied or
   explicitly approved them. Treat candidate MeSH headings as unvalidated until the CLI resolves them.
5. Keep PICO comparison/outcome and PCC context as optional precision components.

## Run a quick search

Run from the chosen working directory:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search run question.json
```

Quick mode defaults to 20 retained records per source and a visible three-year date bound. Continue
after provider failure, but report the run directory and every omitted or failed source.

## Run an approval-gated review search

1. Require an explicit per-source limit or `all`. Generate but do not retrieve:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search plan question.json --mode review --limit-per-source 100 --json
```

2. Show the grouped question, exact source queries, selected per-source variants, filters,
   degradations, and strategy digest. If revision is requested, edit the question and create a new run.
3. After explicit approval, bind it to the displayed digest:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search approve <run-dir> --strategy-digest <sha256>
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search preflight <run-dir>
```

4. Stop if any source is unavailable. Otherwise retrieve:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search search <run-dir>
```

5. For `all`, show the preflight counts and obtain a second explicit confirmation first, then pass
   the preflight token:

```bash
uvx --from git+https://github.com/junhewk/medical-deep-research-plugin.git@v0.4.0 hermes-medical-search search <run-dir> --confirm-all TOKEN
```

6. Re-run the same search command to resume a checkpointed retrieval; never edit `strategy.json`.

Select sources with `--sources`/`--exclude`, choose a per-source variant with repeatable
`--variant SOURCE=sensitivity|precision`, set the run directory with `--output`, and skip online
MeSH validation with `--no-mesh`.

## Present results

- Use `sources/<source>.jsonl` for native provider order and `results.jsonl` for deduplicated records.
- Treat `ranked-results.jsonl` as optional `mdr-v2-grouped` prioritization, never evidence quality.
- Use `summary.json`, `manifest.json`, and `approval.json` for counts, failures, state, and provenance.
- Report `records_filtered_by_source` whenever it is non-zero: those records matched the query and
  were then dropped by a language or publication-type filter.
