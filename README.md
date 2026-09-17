# Hermes Medical Research

Hermes Medical Research is a deterministic evidence workflow operated by isolated Hermes bots. The
bots perform bounded semantic tasks; the `mdr` CLI owns the corpus, schemas, citations, digests,
state transitions, audit independence, and final completeness checks.

This is a normal Python package, not a Hermes, Codex, or Claude plugin.

## Install

Install directly from the GitHub repository with a Python tool installer:

```bash
uv tool install git+https://github.com/junhewk/hermes-medical-research.git@v0.5.10
# or
pipx install git+https://github.com/junhewk/hermes-medical-research.git@v0.5.10
```

The package exposes one executable:

```bash
mdr --version
```

## Architecture

| Layer | Responsibility |
| --- | --- |
| Hermes skill | Tells one role what bounded task to perform and which `mdr` commands to call |
| CLI/core | Enforces schemas, exact scope, citations, digests, receipts, legal transitions, and completion |
| Bot profile | Supplies role separation, model/provider choice, memory, configuration, and session identity |
| Artifact store | Carries immutable results between roles; bots exchange only `run_id` and `task_id` |

Four checked-in skills serve six profiles:

- `medical-search` → Searcher
- `medical-select` → Selector
- `medical-extract` → Extractor
- `medical-synthesize` → Synthesizer and independent Auditor
- Coordinator uses its narrow profile instructions only; it does not install or perform specialist
  skills.

The default artifact root is `$XDG_DATA_HOME/hermes-medical-research`, falling back to
`~/.local/share/hermes-medical-research`. Set `MDR_HOME` to an absolute path for an isolated store.

## Bootstrap Hermes profiles

Preview the six-profile installation without changing anything:

```bash
mdr hermes bootstrap
```

Apply it explicitly:

```bash
mdr hermes bootstrap --apply
```

To update only the bounded Searcher profile while leaving other managed profiles untouched:

```bash
mdr hermes bootstrap --profile mdr-searcher --apply
```

Bootstrap creates clean `mdr-coordinator`, `mdr-searcher`, `mdr-selector`, `mdr-extractor`,
`mdr-synthesizer`, and `mdr-auditor` profiles through Hermes's public profile command. It copies only
the current model/provider/timezone selection and enables the terminal, file, and skills toolsets. It
refuses to overwrite unmanaged profiles or managed files edited after installation.

Bot Mode discovers profiles automatically from each connected Hermes gateway. If profiles were
added while Hermes Desktop was connected, use **Reconnect gateway** to refresh its roster. Preview
the cron fleet, then apply it explicitly after enabling gateway profile multiplexing:

```bash
mdr hermes routines
mdr hermes routines --apply
```

This creates six base Routines, all script-only: a Coordinator tick and one serial runner per
specialist (`mdr hermes drain --role ROLE`). A runner claims exactly one Task, opens one fresh Hermes
session for it with an instruction file of exact commands, and claims the next Task immediately after
acceptance until its queue is empty. A session that cannot finish its Task is failed with backoff and
the runner moves on. Full-text acquisition has no semantic choice and is submitted without a model
session. Each living Review also gets one visible, script-only Routine at its actual cadence. Stable
idle minutes use zero model calls.

Each session has a model-turn ceiling set in its profile:

| Profile | Turn ceiling |
| --- | --- |
| `mdr-searcher` | 8 |
| `mdr-selector` | 16 |
| `mdr-extractor` | 60 |
| `mdr-synthesizer` | 40 |
| `mdr-auditor` | 48 |

Verify profiles, `mdr`, multiplexing, cron schedulers, managed scripts/jobs, cron health, and paused
Routines with:

```bash
mdr hermes doctor
```

Hermes keeps its own pause state for each Routine. Inspect or change all managed Routines at once:

```bash
mdr hermes routines --status
mdr hermes routines --pause-all
mdr hermes routines --resume-all
```

## Workflow

For normal use, open `mdr-coordinator` in the Bot roster and describe the research request in ordinary
language. The Coordinator confirms the protocol and creates a human-named Review. Cron workers claim
bounded Tasks directly; no Bot or human relays IDs.

One-off and living Reviews use the same interface:

```bash
mdr review create --name exercise-review \
  --request examples/research-protocol.json --schedule once

mdr review create --name living-exercise \
  --request examples/research-protocol.json \
  --schedule "0 3 * * 1" --timezone Asia/Seoul

mdr review list
mdr review status living-exercise
mdr review pause living-exercise
mdr review resume living-exercise
mdr --actor mdr-coordinator review retry living-exercise \
  --reason "Workers were restarted after an outage"
mdr --actor mdr-coordinator review cancel living-exercise \
  --reason "Replace a blocked or defective Cycle"
mdr review run-now living-exercise
```

Pausing a Review stops new claims and never marks its waiting Tasks abandoned; resuming restarts that
clock. A pending Task that no worker claims within 95 active minutes blocks the Cycle. `review retry`
reopens a blocked latest Cycle in place: blocked Tasks return to the queue with fresh attempts and
keep any partial proposal edits, and all accepted work is kept. `review run-now` instead starts a new
Cycle on a new Run. An immutable protocol change uses `mdr review fork`; a standalone Run can be
brought under human status management with `mdr review adopt`. A living Review starts its first
Cycle immediately. Schedule fires during an active Cycle coalesce into one catch-up Cycle.

For scripted or diagnostic use, create a Run from a versioned PICO/PCC request:

```bash
mdr run create --request examples/research-protocol.json
```

The Coordinator can inspect a standalone Run diagnostically:

```bash
mdr --actor mdr-coordinator run next RUN_ID
```

Serial runners claim work for their sessions. For diagnostics, a worker profile can claim directly:

```bash
mdr search claim
mdr select claim
mdr extract claim
mdr synthesize claim
mdr audit claim
```

Each claim returns a bounded packet/proposal, a 60-minute session-bound token, and exact source,
submit, and failure commands that name the resolved `mdr` executable. Failures retry after 5 and 30
minutes; the third blocks the Cycle. Accepting any Task routes the next Task at once, so the
Coordinator tick is a safety net rather than a one-Task-per-minute throttle. The Searcher claim also
returns one `search execute` command that records the initial plan and performs retrieval in one
bounded operation. A retry resumes the materialized child search with the same frozen plan; changing
it requires a new Review fork.

The older explicit-ID commands remain operator diagnostics for non-managed Runs:

```bash
mdr search next RUN_ID TASK_ID
mdr search execute RUN_ID TASK_ID --from PROPOSAL.json
mdr search run RUN_ID TASK_ID

mdr select next RUN_ID TASK_ID
mdr select submit RUN_ID TASK_ID --from PROPOSAL.json

mdr extract next RUN_ID TASK_ID
mdr extract submit RUN_ID TASK_ID --from PROPOSAL.json

mdr synthesize next RUN_ID TASK_ID
mdr synthesize submit RUN_ID TASK_ID --from PROPOSAL.json

mdr audit next RUN_ID TASK_ID
mdr audit submit RUN_ID TASK_ID --from PROPOSAL.json
```

Hermes profiles normally provide actor/session identity. `--actor` exists for deterministic testing
and recovery; global options such as `--actor`, `--store`, and `--claim-token` may appear before or
after the subcommand. An active Task can read only allowed sources. `find` ranks the locators of the
Task's documents by search words with short snippets, and `read` returns one locator's exact text for
verbatim quotes. `show` pages a whole source, including logical rows, in 16 KiB pages:

```bash
mdr source find RUN_ID TASK_ID Mini-CEX satisfaction survey
mdr source read RUN_ID TASK_ID DOCUMENT_ID table:1
mdr source list RUN_ID TASK_ID --page 1
mdr source show RUN_ID TASK_ID SOURCE_ID --page 1
```

## Outcome decisions

Every selected record must decide every protocol outcome. An assessment Task seeds one extraction and
appraisal scaffold per protocol outcome, bound by `protocol_outcome`, plus one `dispositions` row
with an `outcome_checklist` of likely source locations. The Extractor fills the rows for outcomes the
record reports and marks each other outcome `not_reported` or `not_applicable` with a rationale and
the locations it inspected. Submissions that leave any outcome undecided are rejected, untouched
scaffolds are removed, synthesis links evidence by `protocol_outcome`, and each disposition row is
audited against the full text. Exports include `dispositions.csv` and a per-outcome decision table.
Runs that recorded extractions before 0.5.8 keep validating, with an explicit warning that
per-outcome completeness was not verified.

After all audit groups pass:

```bash
mdr run status RUN_ID
mdr --actor mdr-coordinator finalize RUN_ID
```

For review-preparation mode, supply explicit retrieval and full-text limits (or `all`). Search
strategies and all-results retrieval retain digest/token approval gates.

Report protocols must include PubMed or Europe PMC. At least one of those biomedical indexes must be
available and return a nonzero preflight count before retrieval proceeds. OpenAlex, Semantic Scholar,
registries, and other configured sources remain useful supplements; an unavailable supplement is
recorded in provenance without invalidating an otherwise viable report search. For high recall,
search plans use the required framework concepts (PICO population plus intervention, PECO population
plus exposure, or PCC population plus concept) and reserve comparison and outcome terms for
eligibility, synthesis, or a documented precision variant.

## Safety properties

- Run and Task IDs resolve only inside the configured artifact store.
- Packets are capped at 32 KiB and submissions must cover exactly their assigned targets.
- Proposal base digests reject stale work; accepted retries are idempotent only when byte-equivalent
  JSON content has the same canonical digest.
- Search snapshots, evidence revisions, audit results, and exports are content-addressed.
- Parent search reservations and Task child links are committed together; interrupted child runs
  recreate a missing idempotent reservation before retrieval resumes.
- Audit receipts bind every reviewed finding/report target to an independent Auditor profile and to
  a digest of the target's assertion and every source it may be checked against. Report targets are
  audited in record-level groups; unchanged groups keep their receipts when other evidence changes.
- An audit verdict of `revise` returns one audit group at a time to the responsible specialist. A
  group still unresolved after two corrections halts the Run until an operator runs `review retry`.
- Citation document IDs, locators, and verbatim quotes are checked against the stored corpus.
- Deterministic checks establish traceability and consistency, not clinical truth; semantic review is
  still the responsibility of the isolated specialist profiles.

Living Reviews freeze and replay their accepted search plan. Corpus identity prefers DOI, then PMID,
PMCID, and source/source-ID. Digest-identical work receives an immutable reuse receipt; changed
metadata or retraction state is rescreened. An unchanged refresh records a checkpoint referring to
the prior report and skips downstream inference. Changed audit targets always receive a fresh audit.

## v0.4 migration

Copy a v0.4 evidence-schema workspace into the shared store:

```bash
mdr run migrate /absolute/path/to/v0.4-run
```

The source is never modified. Existing native-review, completion, verification, and review artifacts
are archived under `provenance/v0.4`; a fresh independent audit under the current audit contract is
mandatory.

## Qualification and development

The first operational gate is deliberately small: two fresh Selector bot chats each inspect and
record one synthetic article decision.

```bash
uv run python scripts/qualify_hermes.py \
  --hermes-home /path/to/isolated-hermes-home
```

Only after that passes should the three-run full qualification be attempted:

```bash
uv run python scripts/qualify_hermes.py \
  --hermes-home /path/to/isolated-hermes-home \
  --full-runs 3 \
  --output qualification.json
```

Local deterministic checks:

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run pytest
uv build
```

See [project context](CONTEXT.md), the
[CLI architecture decision](docs/adr/0001-hermes-skills-over-deterministic-cli.md), the
[cron architecture decision](docs/adr/0002-cron-backed-review-automation.md), the
[serial runner decision](docs/adr/0003-serial-runners-and-outcome-decisions.md), and
[validation status](VALIDATION.md).
