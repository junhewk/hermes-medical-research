# Hermes Medical Research

Hermes Medical Research is a deterministic evidence workflow that an operator advances one step at a
time. A step drains one stage's queue for one Review. The `hmr` CLI owns the corpus, schemas,
citations, digests, state transitions, audit independence, and final completeness checks. No model
output has authority.

This is a normal Python package, not a Hermes, Codex, or Claude plugin.

## Install

Install directly from the GitHub repository with a Python tool installer:

```bash
uv tool install git+https://github.com/junhewk/hermes-medical-research.git@v0.6.0
# or
pipx install git+https://github.com/junhewk/hermes-medical-research.git@v0.6.0
```

The package exposes one executable, and the managed profiles call it by its resolved path:

```bash
hmr --version
```

## Architecture

| Layer | Responsibility |
| --- | --- |
| Step | One operator act. It claims and answers one stage's items until that queue is empty |
| CLI/core | Enforces schemas, exact scope, citations, digests, receipts, legal transitions, and completion |
| Hermes profile | Supplies role separation, model and provider choice, session identity, and either the answer schema or the tool surface of one lane |
| MCP submit tools | Carry one typed payload from a session into the normal submit path |
| Artifact store | Carries immutable results between roles; sessions exchange only `run_id` and `task_id` |

Three checked-in skills serve the five session profiles. `hmr-select` belongs to the Selector,
`hmr-extract` to the Extractor, and `hmr-synthesize` to the Synthesizer and the independent
Auditor. The four constrained profiles carry no skill and no shell toolset. The Coordinator uses its
own profile instructions and performs no specialist work.

The default artifact root is `$XDG_DATA_HOME/hermes-medical-research`, falling back to
`~/.local/share/hermes-medical-research`. Set `HMR_HOME` to an absolute path for an isolated store.

## Lanes

Every item is still one claim, one lease, and one validated submission. The lane decides only which
surface produces the answer.

| Task kind | Lane | Why |
| --- | --- | --- |
| `search` | `none` | Retrieval replays a frozen accepted plan, so there is nothing to decide |
| `screening` | `call` | One record, one packet of eligibility criteria, three allowed decisions |
| `coverage` | `call` | Eligibility is already settled; the answer is a selection plus outcome names the packet lists |
| `fulltext` | `none` | Acquisition is an HTTP fetch |
| `studies` | `call` | A link is allowed only on identity evidence the packet already shows |
| `assessment` | `agent` | Each protocol outcome must be decided from locations read in the full text |
| `synthesis` | `call` | One finding from the packet's extraction, appraisal, and unreported-outcome rows |
| `audit` | `agent` | Quotes and locators are checked against the stored sources |

A `call` is one fresh Hermes session with no tools at all. Its profile carries that kind's answer
schema in `extra_body.response_format`, so the model answers with one JSON object, and the step
runner writes only the fields the schema owns and submits them. An `agent` is one fresh session with
the role's skill, the terminal, file, and skills toolsets, and the role's typed submit tools. A
synthesis packet that was shortened to fit falls back to `agent`, because its rows are then
summaries. Every audit correction takes `agent`, because a correction re-mints the same packet and a
constrained call would repeat the rejected answer.

A `call` gets two constrained attempts and then one session with tools. An `agent` gets three
attempts. A rejected answer comes back as the validator's own message, appended after the record, so
the cached prompt prefix survives the retry. After the ladder the durable failure path runs: the item
retries after 5 and 30 minutes, and the third failure blocks the Cycle for `hmr step retry`.

## Why a step detaches

A `type: exec` Hermes quick command is killed after 30 seconds and receives no arguments. So a step
starts a detached worker and returns one line, and nothing about the work is passed as an argument:

- the active Review is `<store>/steps/active.json`, set by `hmr step use NAME`;
- progress, the last pace, recent failures, and the next command are in
  `<store>/steps/<review>/<step>/status.json`, which `hmr step status` reads;
- the worker's own output is in that step's `logs/JOB_ID.log`;
- the claim a step is working on is `<store>/steps/<step>/current.json`, owner-readable, and the
  tool server reads it to resolve its own work.

## Bootstrap Hermes profiles

Preview the nine-profile installation without changing anything, then apply it:

```bash
hmr hermes profiles
hmr hermes profiles --apply
hmr hermes profiles --profile hmr-screen --apply
```

Five profiles run sessions: `hmr-coordinator`, `hmr-selector`, `hmr-extractor`, `hmr-synthesizer`,
and `hmr-auditor`. Each keeps its skill and toolsets and hosts the `hmr-tasks` tool server for its
role. Four answer one constrained call: `hmr-screen`, `hmr-cover`, `hmr-link`, and `hmr-finding`. A
constrained profile enables no toolset, hosts no tool server, pins `extra_body.response_format` with
its kind's answer schema on the provider entry its `model.provider` names and on every
`custom_providers` entry, and caps `max_tokens` for that kind.

The two surfaces do not swap. A session that must read full text cannot carry a schema: the gateway
refuses tools plus a response format with HTTP 400. A decision that needs no tools is one request
with a schema on it, which is cheaper than a tool call the host proxies behind its own meta-tools.

Bootstrap creates profiles through Hermes's public profile command. It copies only the current model,
provider, and timezone selection. It refuses to overwrite an unmanaged profile or a managed file
edited after installation. `hmr-searcher` is removed when present, because the search step runs no
session. Bot Mode discovers profiles automatically from each connected gateway;
use **Reconnect gateway** to refresh a roster that was connected during installation.

A per-kind model or lane choice is the operator's, and it is applied before the schema is pinned, so
the schema lands on the provider entry the profile really selects:

```bash
hmr hermes profiles --set-model screening=MODEL@PROVIDER --apply
hmr hermes profiles --set-lane synthesis=agent --apply
hmr hermes profiles --show
```

`--set-model` takes `MODEL` or `MODEL@PROVIDER`. Choices are stored in
`<hermes_home>/hmr-steps.json` and cover the four constrained kinds only.

Each session has a model-turn ceiling set in its profile:

| Profile | Turn ceiling |
| --- | --- |
| `hmr-selector` | 16 |
| `hmr-extractor` | 60 |
| `hmr-synthesizer` | 40 |
| `hmr-auditor` | 48 |
| `hmr-screen`, `hmr-cover`, `hmr-link`, `hmr-finding` | 2 |

A constrained call is given 5 minutes and a session 75 minutes. The runner renews the claim every 10
minutes while a session is still working, so a slow session cannot outlive its 60-minute lease.

Verify profiles, the resolved `hmr`, each constrained profile's pinned answer schema and empty
toolset, the installed quick commands, per-kind assignments, and any leftover cron fleet with:

```bash
hmr hermes doctor
```

## Install the step commands

```bash
hmr hermes commands
hmr hermes commands --apply
hmr hermes commands --status
hmr hermes commands --apply --review REVIEW
hmr hermes commands --remove --apply
```

The edit is a marked region inside the host `config.yaml`, owned by a digest manifest. The file is
backed up once before the first change, and the result is re-parsed and compared against the
original, so an unmanaged quick command or any other setting cannot be changed. An entry edited
outside `hmr` is refused rather than overwritten. With `--review` the Review name is baked into every
command string; without it each command uses the stored active Review.

| Command | What it does |
| --- | --- |
| `/hmr-search` | Runs the search step |
| `/hmr-selector` | Screens and selects |
| `/hmr-extractor` | Acquires full text, links studies, and assesses |
| `/hmr-status` | Prints stage progress, recent failures, and what is next |
| `/hmr-stop` | Asks running steps to finish the item in flight and stop |

Five commands, one per thing you do plus one read and one brake. Extraction is where a Run is handed
over: what it records is an estimate, an appraisal, and a disposition for every protocol outcome,
each carrying a document, a locator, and a verbatim quote, which is the artifact a reviewer checks
and synthesizes from.

Synthesis and audit are not installed as commands. They still exist, and `hmr step synthesize` and
`hmr step audit` still run them, but measured on the 170-record production review synthesis took
about two hours for seven outcomes and audit about eight hours for 106 assertion groups. Neither is
work that can honestly be handed to someone as a command, so neither is offered as one.

## The tool server

```bash
hmr mcp serve --role ROLE
hmr mcp serve --role ROLE --kind KIND
```

The server speaks JSON-RPC 2.0 over newline-delimited stdio and is started by a session profile's
`mcp_servers` entry, which names only the role. The server lists that role's submit tools: the
Selector sees `submit_screening` and `submit_coverage`, the Extractor `submit_study_link`, and the
Synthesizer `submit_finding`. The Coordinator and the Auditor own no kind that one payload can
answer, so their listing is empty. Calling a tool costs a session fewer turns than editing
`proposal.json` and shelling out to a submit command. `--kind` narrows the listing and is an operator
diagnostic.

That entry is static: the server resolves its own claim from `<store>/steps/<step>/current.json`, so
no task id, path, or claim token is ever written into a config file or a prompt. With no current item
every tool call is refused. A submitted payload is mapped into the task's pre-filled proposal and put
through the same `submit` path as a hand-edited one, and a rejection comes back to the model as the
validator's own message.

## Workflow

For normal use, open `hmr-coordinator` in the Bot roster and describe the research request in
ordinary language. The Coordinator confirms the protocol, creates a human-named Review, and tells you
which command to run next.

```bash
hmr step use exercise-review
hmr step status
```

Then run one step at a time, in order: `/hmr-search`, `/hmr-selector`, and `/hmr-extractor`.
`/hmr-status` after each one reports what is done, what failed, and what is next. A step does nothing
until you run it. When extraction drains, the Run holds the recorded evidence and is ready to hand
over.

The same steps are available as CLI commands, with flags the quick commands cannot carry:

```bash
hmr step select
hmr step select --limit 10
hmr step select --foreground
hmr step extract --review exercise-review --max-wait 60
hmr step prompt select
hmr step stop --step select
hmr step retry --reason "Corrected the model endpoint"
hmr step finalize
hmr step use --clear
```

A step claims only the active Review. It holds a per-step lock, so a second runner is refused rather
than racing. A failed item is unavailable for 5 minutes, so the step waits that window out instead of
mistaking it for an empty queue. A single wait is bounded by `--max-wait`, 40 minutes by default, and
repeated empty rounds end the step as drained. An interrupted runner's claim is released on the
next start without spending one of its three attempts. A step ends as drained, stopped,
limit_reached, blocked, refused, or failed, and `hmr step prompt STEP` prints the prompt a
constrained call would send without sending it.

One-off and living Reviews use the same interface:

```bash
hmr review create --name exercise-review \
  --request examples/research-protocol.json --schedule once

hmr review create --name living-exercise \
  --request examples/research-protocol.json \
  --schedule "0 3 * * 1" --timezone Asia/Seoul

hmr review list
hmr review status living-exercise
hmr review pause living-exercise
hmr review resume living-exercise
hmr --actor hmr-coordinator review retry living-exercise \
  --reason "Corrected the model endpoint"
hmr --actor hmr-coordinator review cancel living-exercise \
  --reason "Replace a blocked or defective Cycle"
hmr review run-now living-exercise
```

Pausing a Review stops new claims and leaves its waiting Tasks alone; a Task that nobody claims
between two steps is normal and never blocks a Cycle. `review retry` reopens a blocked latest Cycle
in place: blocked Tasks return to the queue with fresh attempts and keep any partial proposal edits,
and all accepted work is kept. `review run-now` instead starts a new Cycle on a new Run. An immutable
protocol change uses `hmr review fork`; a standalone Run can be brought under human status management
with `hmr review adopt`. A living Review replays its accepted search plan. Every step first advances
routing, leases, finalization, and a coalesced catch-up Cycle. Nothing fires a schedule now that the
cron fleet is retired, so a refresh Cycle starts when you run `hmr review run-now`.

For scripted or diagnostic use, create a Run from a versioned PICO/PCC request:

```bash
hmr run create --request examples/research-protocol.json
```

## Operator diagnostics

These commands inspect and repair; normal work goes through steps.

```bash
hmr hermes doctor
hmr hermes commands --status
hmr hermes routines --status
hmr hermes routines --remove --apply
hmr step status --review exercise-review
hmr work notifications
hmr work fail CLAIM_ID --code CODE --message MESSAGE
hmr run status RUN_ID
hmr --actor hmr-coordinator run next RUN_ID
```

`hmr hermes routines` is removal-only. It exists to take a 0.5.x cron fleet off a host that still has
one, because a Routine that claims work on a timer would race the operator's own step runner.

A role can also claim directly, which claims across every active Review rather than the active one:

```bash
hmr search claim
hmr select claim
hmr extract claim
hmr synthesize claim
hmr audit claim
```

Each claim returns a bounded packet and proposal, a 60-minute session-bound token, and exact source,
submit, and failure commands that name the resolved `hmr` executable. The explicit-ID commands remain
operator diagnostics for non-managed Runs:

```bash
hmr search next RUN_ID TASK_ID
hmr search execute RUN_ID TASK_ID --from PROPOSAL.json
hmr search run RUN_ID TASK_ID

hmr select next RUN_ID TASK_ID
hmr select submit RUN_ID TASK_ID --from PROPOSAL.json

hmr extract next RUN_ID TASK_ID
hmr extract submit RUN_ID TASK_ID --from PROPOSAL.json

hmr synthesize next RUN_ID TASK_ID
hmr synthesize submit RUN_ID TASK_ID --from PROPOSAL.json

hmr audit next RUN_ID TASK_ID
hmr audit submit RUN_ID TASK_ID --from PROPOSAL.json
```

Hermes profiles normally provide actor and session identity. `--actor` exists for deterministic
testing and recovery; global options such as `--actor`, `--store`, and `--claim-token` may appear
before or after the subcommand. An active Task can read only allowed sources. `find` ranks the
locators of the Task's documents by search words with short snippets, and `read` returns one
locator's exact text for verbatim quotes. `show` pages a whole source, including logical rows, in
16 KiB pages:

```bash
hmr source find RUN_ID TASK_ID Mini-CEX satisfaction survey
hmr source read RUN_ID TASK_ID DOCUMENT_ID table:1
hmr source list RUN_ID TASK_ID --page 1
hmr source show RUN_ID TASK_ID SOURCE_ID --page 1
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
hmr run status RUN_ID
hmr --actor hmr-coordinator finalize RUN_ID
```

For review-preparation mode, supply explicit retrieval and full-text limits, or `all`. Search
strategies and all-results retrieval retain digest and token approval gates.

Report protocols must include PubMed or Europe PMC. At least one of those biomedical indexes must be
available and return a nonzero preflight count before retrieval proceeds. OpenAlex, Semantic Scholar,
registries, and other configured sources remain useful supplements; an unavailable supplement is
recorded in provenance without invalidating an otherwise viable report search. For high recall,
search plans use the required framework concepts, which are PICO population plus intervention, PECO
population plus exposure, or PCC population plus concept, and reserve comparison and outcome terms
for eligibility, synthesis, or a documented precision variant.

## Safety properties

- Run and Task IDs resolve only inside the configured artifact store.
- Packets are capped at 32 KiB and submissions must cover exactly their assigned targets.
- A constrained schema buys shape only. Every value, identifier, digest, and cross-field rule is
  checked when the mapped proposal reaches the normal submit path.
- Identity and provenance fields are never taken from a model payload: record, study, and finding
  ids, protocol outcomes, schema versions, base digests, and certainty origin stay as the Task minted
  them.
- Proposal base digests reject stale work; accepted retries are idempotent only when byte-equivalent
  JSON content has the same canonical digest.
- Search snapshots, evidence revisions, audit results, and exports are content-addressed.
- Parent search reservations and Task child links are committed together; interrupted child runs
  recreate a missing idempotent reservation before retrieval resumes.
- Audit receipts bind every reviewed finding and report target to an independent Auditor profile and
  to a digest of the target's assertion and every source it may be checked against. Report targets
  are audited in record-level groups; unchanged groups keep their receipts when other evidence
  changes.
- An audit verdict of `revise` returns one audit group at a time to the responsible specialist. A
  group still unresolved after two corrections halts the Run until an operator runs `review retry`.
- Citation document IDs, locators, and verbatim quotes are checked against the stored corpus.
- Deterministic checks establish traceability and consistency, not clinical truth; semantic review is
  still the responsibility of the isolated specialist profiles.

Living Reviews freeze and replay their accepted search plan. Corpus identity prefers DOI, then PMID,
PMCID, and source with source ID. Digest-identical work receives an immutable reuse receipt; changed
metadata or retraction state is rescreened. An unchanged refresh records a checkpoint referring to
the prior report and skips downstream inference. Changed audit targets always receive a fresh audit.

## v0.4 migration

Copy a v0.4 evidence-schema workspace into the shared store:

```bash
hmr run migrate /absolute/path/to/v0.4-run
```

The source is never modified. Existing native-review, completion, verification, and review artifacts
are archived under `provenance/v0.4`; a fresh independent audit under the current audit contract is
mandatory.

## Development

Local deterministic checks:

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run --extra dev pytest -q
uv build
```

Operational gates against a real Hermes host are recorded in [validation status](VALIDATION.md).

See [project context](CONTEXT.md), the
[CLI architecture decision](docs/adr/0001-hermes-skills-over-deterministic-cli.md), the
[cron architecture decision](docs/adr/0002-cron-backed-review-automation.md), the
[serial runner decision](docs/adr/0003-serial-runners-and-outcome-decisions.md), and the
[step and answer decision](docs/adr/0004-user-invoked-steps-schema-calls-and-submit-tools.md).
