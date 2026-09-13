# Medical Deep Research Plugin

Evidence-based medical research for **Codex CLI, Claude Code, and Hermes Agent**, entirely from
a terminal. Two shared skills use a Python CLI to search literature, preserve source evidence,
record the host agent's assessments, and export a cited report.

- **medical-deep-research**: protocol → retrieval → screening → study links → full text →
  extraction → appraisal → outcome-level synthesis → verification → report.
- **medical-literature-search**: reproducible literature retrieval without report generation.

The host agent performs the reasoning. Version 0.4 adds source packets, atomic batch submissions,
a separate claim-review stage, and one-command finalization. The CLI validates references and quotes, manages budgets
and resumable artifacts, and renders the result. No MCP server or separate model API key is needed.
This replaces `hermes-medical-search`; the desktop
[medical-deep-research](https://github.com/junhewk/medical-deep-research) application remains separate.

## Install from a terminal

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git,
network access to the selected literature APIs, and a host client with plugin support.
Skills invoke the matching Git release through `uvx`.

**Codex CLI**

```bash
codex plugin marketplace add junhewk/medical-deep-research-plugin --ref v0.4.0
codex plugin add medical-deep-research-plugin@junhewk-medical-research
codex plugin list
```

**Claude Code**

```bash
claude plugin marketplace add junhewk/medical-deep-research-plugin
claude plugin install medical-deep-research-plugin@junhewk-medical-research
claude plugin list
```

Start a new Claude session and invoke
`/medical-deep-research-plugin:medical-deep-research` followed by the research question.

**Hermes Agent**

```bash
hermes plugins install junhewk/medical-deep-research-plugin --no-enable
hermes plugins enable medical-deep-research-plugin --no-allow-tool-override
hermes plugins list --user --json
```

For the usual short name and automatic skill selection, append the installed skills directory to
`skills.external_dirs` in the active profile's `config.yaml` (normally `~/.hermes/config.yaml`).
Preserve any existing directory entries:

```yaml
skills:
  external_dirs:
    - plugins/medical-deep-research-plugin/skills
```

The relative path resolves inside the active Hermes profile. This is Hermes's supported way to
include shared skills in its startup index, `skills_list`, `skill_view`, and slash commands. It reads
the installed package directly, so plugin updates also update these instructions.

In an existing chat, send:

```text
/reload-skills
```

Then invoke `/medical-deep-research` followed by the report request, or ask Hermes to load
`skill_view(name="medical-deep-research")`. The startup index includes the short name when the
session's prompt is next built; the slash command explicitly loads the instructions in the current
chat. A gateway restart is not needed for this external-directory setup.

Hermes also retains an internal portable-plugin identifier:

```text
agent-plugin-medical-deep-research-plugin-71b62b59:medical-deep-research
```

This generated namespace is normal. **Installing and enabling a portable plugin alone does not put
its skills in Hermes's startup skill index or expose bare names.** Without `external_dirs`, discover
the qualified name using `skills_list` and load it with `skill_view`; the bare name won't resolve.
The namespace can differ if the installed directory key differs. Portable-plugin registration is
cached by the hosting process; `/reload-skills` refreshes the configured skill directories, not that
plugin registry.

The external-directory setting activates the instructions independently of the plugin registry.
Remove its entry as well when deactivating these skills. Local skills with the same name take
precedence; an existing standalone `medical-literature-search` remains the bare-name version until
you intentionally update or remove that standalone copy.

If the short name is missing, check `skills.external_dirs` in the chat's active profile, the path's
existence, and skill-specific disabled settings. `plugins list` and Plugin Doctor check different
parts of setup. Doctor reporting **0 tools, 0 hooks is expected**: this package contributes skills.
Hermes reads `skills/*/SKILL.md`; adding a `skills` field to the portable manifest does not index them.

Version 0.4.0 fixes an installation-scan false positive caused by a local-file rejection test in
0.3.0. The test now uses a harmless temporary fixture and still rejects local file URLs. The actual
Hermes installation scan passes without a scanner override. For an existing, unpinned Git
installation, run `hermes plugins update medical-deep-research-plugin` to retrieve the fix.

The package includes portable, Codex, and Claude manifests and a Git marketplace catalog. Host docs:
[Codex](https://developers.openai.com/plugins/build/plugins),
[Claude Code](https://code.claude.com/docs/en/plugins-reference),
[Hermes](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/).

## Start a report

Load the skill explicitly before giving the research task:

- **Codex CLI:** select or mention `$medical-deep-research` from the installed plugin.
- **Claude Code:** invoke `/medical-deep-research-plugin:medical-deep-research`.
- **Hermes:** after the directory setup above, invoke `/medical-deep-research` or ask it to load
  `skill_view(name="medical-deep-research")`. A prompt mentioning a missing skill does not install
  or configure its package.

Then give the task and settings, for example:

> Use the loaded medical-deep-research skill. Create a medical evidence report on exercise for
> adults with hypertension. Compare blood-pressure effects and harms, explain disagreements,
> and export Markdown, HTML, and the evidence tables.
> Use report mode, a PICO protocol, English output, no publication-date restriction, the default
> available sources, up to 100 raw records per source, and up to 30 full-text attempts. Show the
> effective settings and source availability before retrieval. Save the run to
> `runs/exercise-hypertension`.

These are **per-report settings**, saved in the run's immutable protocol. There is no separate
plugin settings screen or persistent plugin-wide report configuration. Specify scope, sources,
and filters in the protocol JSON; set mode, language, budgets, and destination through
`research init` options. Credentials come from the host environment. The skill translates the
request into those inputs and reports unavailable sources. To change a saved protocol or budget,
initialize a new run.

Supported frameworks: **PICO** (interventions), **PECO** (exposures/harms), **DIAGNOSTIC**,
**PROGNOSIS**, and **PCC** (scoping). Search components are explicit: sensitive PICO retrieval
normally uses population and intervention, with comparison/outcome assessed during screening.
Synonyms are OR groups; separate concepts are AND groups. Validated MeSH supplements text.
Schema-3 searches have no hidden date restriction.

Report defaults: **100 raw retrieved records per source across all searches** and **30 unique
records attempted for full text**. Reserve some capacity for gap searches/citation chaining.
Failed full-text attempts count; retries of the same record reuse its slot.

For a local checkout:

```bash
uv sync --locked --extra dev
uv run medical-deep-research-plugin research init examples/research-protocol.json --output runs/example
uv run medical-deep-research-plugin doctor --json
uv run medical-deep-research-plugin plan runs/example/question.json --mode quick --sources europe-pmc --limit-per-source 75 --research-run runs/example --json
```

Replace `SEARCH` below with the `run_dir` returned by `plan`:

```bash
uv run medical-deep-research-plugin search SEARCH
uv run medical-deep-research-plugin research attach-search runs/example SEARCH
uv run medical-deep-research-plugin research status runs/example --stage records --limit 50
```

Follow the [evidence contracts](skills/medical-deep-research/references/evidence-records.md) to author
stage JSON after reading the sources. The CLI does not invent missing assessments.

```bash
uv run medical-deep-research-plugin research record runs/example --stage screening --input screening.json
uv run medical-deep-research-plugin research fulltext runs/example
uv run medical-deep-research-plugin research methods
# Record studies, extractions, appraisals, and synthesis using the same record command.
uv run medical-deep-research-plugin research verify runs/example
uv run medical-deep-research-plugin research export runs/example
```

Inspect segments with `research status RUN --stage documents`. Acquisition uses Europe PMC XML,
Unpaywall OA PDFs, and PMC OA packages. XML retains tables; PDF retains page locations. Scans report
that OCR is needed. Supply an accessible PDF with `research fulltext RUN --ids RECORD_ID --pdf paper.pdf`.

## Sources and credentials

| Source | Purpose | Configuration |
|---|---|---|
| PubMed, PMC | Literature, MeSH validation | `NCBI_EMAIL`; optional `NCBI_API_KEY` |
| Europe PMC | Literature, OA XML, references/citations | No key |
| OpenAlex | Broader scholarly discovery | Optional `OPENALEX_API_KEY` for higher quota |
| Semantic Scholar | Scholarly discovery | Optional `SEMANTIC_SCHOLAR_API_KEY` or `S2_API_KEY` |
| ClinicalTrials.gov | Registrations and posted results | No key |
| Scopus | Optional licensed discovery | `SCOPUS_API_KEY`; optional `SCOPUS_INSTTOKEN` |
| Crossref | DOI identity/bibliographic metadata | No key; used by verification |
| Unpaywall | OA PDF locations | Uses `NCBI_EMAIL` |

Research defaults to PubMed, PMC, Europe PMC, OpenAlex, and Semantic Scholar; clinical frameworks
also add ClinicalTrials.gov, and configured Scopus is included. Explicit `sources` overrides this.
Keep credentials in the environment. Doctor reports configuration/reachability; configured values
are redacted from persisted provider errors.

ClinicalTrials.gov has no publication-date equivalent. Split dated/language/type-filtered literature
from an undated registry strategy with the same original question. Registrations and papers stay
separate records, then link to a study. Crossref is a metadata lookup, not another searched database.
There is no direct CENTRAL, Embase, or Web of Science connector; a Cochrane review found in PubMed
is not a direct Cochrane Library search.

Citation chaining creates a normal child plan sharing the source budget:

```bash
uv run medical-deep-research-plugin research snowball RUN --record-id RECORD_ID --direction references --limit 25
```

Execute and attach its returned search directory. Use `--direction citations` for forward chaining.

## Appraisal and review preparation

Each extraction records scope, measure, interval, sample size, source location, and access level.
The host explains agreement, conflict, applicability, and weight for every contribution. Study groups
distinguish independent studies from repeated reports. This release performs narrative synthesis,
not statistical pooling.

[Appraisal guidance](skills/medical-deep-research/references/appraisal.md) covers RoB 2 and its variants,
ROBINS-I, ROBINS-E, QUADAS-3, QUIPS, and PROBAST+AI with explicit versions. The CLI checks domain records
and source locations; it does not implement the complete instruments or decision algorithms.
Outcome-level GRADE-informed judgments explain all five domains and the starting point. Scoping maps
can use descriptive certainty. Missing information stays unassessed; all agent assessments remain
provisional pending human review. Retrieval scores only prioritize reading and never establish certainty.

For systematic/scoping **review preparation**:

```bash
uv run medical-deep-research-plugin research init protocol.json --output runs/review --mode review-prep --records-per-source 500 --fulltexts 100
```

Child plans use `--mode review`. The exact strategy digest requires approval before preflight/search.
`all` requires explicit review budgets and confirmation tied to preflight counts. Follow the
[review preparation protocol](skills/medical-deep-research/references/review-preparation.md).
This produces an auditable dossier; independent screening and other formal review requirements
remain human work.

## Artifacts and resuming

Start or resume recorded evidence work with `research next RUN`; read its packet and edit the
provided input file. Submit it with `research record RUN --batch --input FILE`.
`research check RUN [--input FILE]` reports errors without committing changes;
`research finalize RUN [--input FILE]` checks, verifies and exports. See the
[v2 record contract](skills/medical-deep-research/references/evidence-records.md).

Exports: `report.md`, self-contained `report.html`, `report.json`, `evidence.csv`, `screening.csv`,
`studies.csv`, `findings.csv`, `appraisals.csv`, `coverage.csv`, `selection-counts.json`, and
`references.ris`. `completion.json` records output paths/checksums and `resume.json` identifies the
latest work packet. Reports include search history, selection
counts, aligned findings, provisional certainty, source locations, limitations, and references.
HTML needs no server or scripts and can be copied off a headless machine.

`research.json` is the manifest; `revisions/` contains content-addressed stages, `searches/` preserves
snapshots, and `fulltext/` preserves originals. Research mutations are locked. Stage submissions replace
the whole payload. Upstream changes mark dependent assessments stale and block export until reviewed
and resubmitted. Interrupted searches and full-text attempts resume within the original budgets.

Verification checks quote locations and online identity/available retraction metadata. The host
separately checks semantic support. Mismatched identities block export; inaccessible services or
explicitly skipped checks stay visible limitations. `--offline` deliberately skips online checks.

## Development

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run pytest
uv run python scripts/validate_bundle.py
uv build
uv run python scripts/package_plugin.py
```

Hermes integration check (Git, a committed plugin checkout, and a separate Hermes source checkout):

```bash
uv run --with rich --with python-dotenv --with ruamel-yaml python scripts/validate_hermes.py --hermes-source /path/to/hermes-agent
```

This clones committed source locally, scans the complete tree including tests, checks disabled
state, enables the package without tool-override privileges, and uses fresh processes to list/read
both qualified skills and their references. It also adds the documented external-directory setting
and verifies short-name listing/loading, the startup index, reference access, and slash invocation
in the same process. It changes only temporary Hermes profiles. CI pins the Hermes source revision;
it does not run a model or generate a clinical report.

Optional public API check: `uv run python scripts/live_smoke.py`. CI tests use offline fixtures.
The reproducible release ZIP excludes credentials, runs, environments, and session files.
The wheel installs both CLI names. Existing `hermes-medical-search` commands and schema-1/2 inputs
remain supported. Legacy quick searches retain their visible three-year default; schema-3 research
searches do not. Existing schema-2 search artifacts retain their digest format.

MIT applies to project code and original instructions. Referenced instruments and downloaded papers
retain their own licenses; full appraisal instruments are not redistributed here.
