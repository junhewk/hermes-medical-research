# Validation status

## Deterministic qualification

The current design is covered by the repository test suite. Its architecture cases verify:

- creation and resolution of opaque Run and Task IDs inside an isolated artifact root;
- one-record Selector routing, bounded source access, exact target coverage, and CLI-recorded state;
- idempotent replay from a fresh Selector session;
- rejection of extra records, wrong roles, changed packets/results, and stale submissions;
- independent Auditor identity, exhaustive finding/report targets, quote/locator validation, and
  immutable audit receipts;
- copy-on-import v0.4 migration and provenance archiving;
- dry-run Hermes bootstrap, clean profile contents, least toolsets, automatic Bot Mode discovery,
  unmanaged-profile refusal, and locally edited-file refusal;
- human-named Review/Cycle creation, stable zero-idle-cost probes, session-bound claim tokens,
  bounded 5/30-minute retry and third-attempt blocking, and notification Outbox state;
- dry-run cron fleet planning with six base jobs and a real-cadence job for each living Review;
- atomic search reservation/task linkage, crash-safe reservation repair, immutable materialized
  plans, the report-mode biomedical-index and zero-hit gates, and explicit optional-source outages;
- anonymous and API-key PubMed access, explicit any/all concept semantics across query compilation
  and ranking, and regression coverage for alternative intervention families;
- an eight-turn Searcher profile, one-command search execution, immediate one-article Selector
  continuation through fresh serial host sessions, absolute Routine entrypoints, targeted profile
  bootstrap, managed Routine editing through Hermes's public CLI, and auditable active-Cycle
  cancellation;
- frozen living-review strategy validation, cumulative canonical corpus merge, source-digest reuse
  receipts, changed-source invalidation, and predecessor-linked no-change checkpoints;
- relevance-ranked screening order after corpus attachment (0.5.7);
- per-outcome decisions (0.5.8): scaffolds bound to each protocol outcome, rejection of undecided
  outcomes and of extractions bound to unreported outcomes, scaffold pruning, all-unreported
  records, routing that reopens a record without decisions, synthesis linkage by
  `protocol_outcome`, disposition audit targets and corrections, legacy-run warnings, bounded
  `source find`/`source read` access, and an end-to-end audited export with three findings;
- orchestration (0.5.8): continuation into the next role without a tick, paused Reviews that are
  unclaimable and never abandoned, in-place `review retry` that keeps partial proposals, runner
  failure handling that does not stop the queue, deterministic full-text submission, per-record
  audit groups with screening batches, content-bound audit reuse after a changed record, correction
  halts, and routine pause controls through Hermes's public cron CLI;
- steps (0.6.0): one constrained call per record with one decision each, a call that does not answer
  retried twice and then escalated to the session lane, an exhausted ladder that fails the claim with
  its reason, a single failure that does not end the step, search and full-text acquisition answered
  with no model at all, the deterministic lane of every kind, a step that claims only its own Review,
  the active-Review pointer and the message it raises when none is set, a paused Review reported
  instead of worked, a stop request honored between items, `--limit` leaving the rest claimable, an
  interrupted claim released without spending an attempt, a second runner refused while the step lock
  is held, status that reports progress, failures and the next command, a detached worker that
  inherits none of the quick command's pipes, a second start that reports the running job instead of
  racing it, and a bounded backoff wait that never blocks a Cycle;
- constrained answers (0.6.0): every schema restricted to the grammar-safe keyword subset, no payload
  that is a bare string or enum at the top level, a response format that carries the schema under a
  stable digest, an answer read through a code fence or a stray preface, a prompt prefix that is
  identical across records and carries no record text, a retry hint confined to the tail, an answer
  contract rendered from the schema, off-schema answers rejected with an actionable message, identity
  fields and digests untouched when a payload is applied, and coverage outcomes, study merges and
  finding citations checked against the packet that was shown;
- the tool server (0.6.0): one submit tool per kind a role owns, an object payload with the enums
  inside it, the MCP handshake and tool listing, newline-delimited serving until stdin closes, a
  submitted decision accepted through the normal submit path, an off-schema answer returned to the
  model and never written, a validation rejection returned as the validator's own message, every call
  refused when no task is claimed, a tool that does not match the claimed kind refused, and a claim
  token that is never written into a config file or a prompt;
- quick commands (0.6.0): a dry run that writes nothing, an apply that keeps comments, key order and
  foreign quick commands, self-contained commands with no arguments, a quoted baked-in Review name,
  refusal of an unmanaged name and of a managed entry edited outside the package, removal that
  restores the original bytes, and refusal of a config that is not a mapping;
- managed profiles (0.6.0): a constrained profile that carries its kind's answer schema on the
  provider entry it selects, with no toolset and no tool server, a session profile that keeps its
  toolsets and hosts the submit tools instead, a per-kind assignment that overrides only the model
  and provider, removal that deletes only the files the package installed, the retired
  Searcher profile reported and removed, and doctor reporting a legacy cron fleet or missing quick
  commands as not ready;
- the read-only manifest view (0.6.0): a claim that retries a busy Run lock instead of ending the
  runner;
- the retained retrieval, evidence, appraisal, synthesis, verification, and export behavior.

Run locally with:

```bash
uv sync --locked --extra dev
uv run ruff check .
uv run --extra dev pytest -q
uv build
```

The 0.6.0 local run on 2026-09-18 collected and passed 274 tests, and Ruff and `git diff --check`
passed with it. The final count is updated with each release commit.

## Hermes operational gates

Hermes itself is not available in every development environment, and the constrained lane depends on
whether a provider's `extra_body` reaches the model server. Four host facts are therefore verified by
hand before the step commands are installed on a host:

1. The cron delete verb. Confirm that `hermes -p PROFILE cron delete JOB_ID` removes a job on the
   installed Hermes version, because that is how `hmr hermes routines --remove --apply` takes a
   0.5.x fleet off the host.
2. That a provider `extra_body` reaches the wire. Run one tool-free session on `hmr-screen` with
   reasoning off and confirm the answer is a single JSON object in the shape of the pinned schema.
3. That a session profile's stdio tool server starts. Open a session on `hmr-selector` and confirm
   the server starts and lists `submit_screening` and `submit_coverage`.
4. That the schema holds against an adversarial prompt. Send one screening packet whose text demands
   an out-of-enum decision and a long reason, and confirm the answer still arrives as one object
   whose `decision` is inside the enum.

Only then re-screen the recorded 170-record corpus of `ai-med-ed-evidence-report-v4` in an isolated
store and compare every decision with the recorded one. Its gates are:

- every record decided;
- zero unparseable or off-schema final answers, down from 7 of 170;
- agreement with the recorded bot decisions at or above 152 of 163;
- a mean under 15 seconds per record.

The thresholds come from the one-shot screening measurement taken on that corpus on 2026-09-18: 170
of 170 records answered in 20.2 minutes, 7.1 seconds mean, 7 answers prose rather than JSON, and 152
of the 163 parseable answers agreeing with the recorded bot decisions. That run asked for the answer
shape instead of constraining it, which is why 7 answers were prose, and it is not one of the gates.

Facts one, two and four were verified on `jkworkstation` on 2026-09-18. `hermes -p PROFILE cron
delete JOB_ID` removed a job. A tool-free `hmr-screen` session with reasoning off answered one
screening record as a single schema-shaped JSON object, in one model call and 13 seconds, and it held
the decision enum against a prompt that demanded an out-of-enum value and a 300-word reason. The same
record was also answered through a submit tool for comparison: Hermes proxies an MCP tool behind its
own meta-tools, so the session called `tool_describe` and then `tool_call`, about three model turns,
and it reached for skill tools as well. That measurement is why the `call` lane carries a schema and
the submit tools stay with the sessions.

Fact three was verified standalone: `hmr mcp serve --role selector` started and listed its submit
tools. It has not been checked from inside a session profile on the host.

The re-screening comparison has not been run. `scripts/qualify_hermes.py` is the 0.5.x harness and
still installs the cron fleet, so it does not run it. A failure of a host fact is a stop condition:
do not weaken the CLI boundary, and do not accept an unconstrained answer shape as a fallback.

### Current Hermes result (2026-09-15)

On `jkworkstation`, Hermes Agent 0.21.2 accepted the public creation of all six isolated profiles.
The earlier two-session Selector terminal gate passed: both fresh chats returned the assigned `run_id` and
`task_id` with state `accepted`, and the harness independently confirmed one accepted receipt in each
Run. The retained artifact is
`/tmp/hermes-medical-research-v0.5-codex-20260915/selector-qualification-2.json` on that host.

On 2026-09-16, the six profiles were also installed into the live `~/.hermes` registry on
`jkworkstation`, used by Hermes Agent 0.21.3 and the macOS remote gateway. Bot Mode discovered them automatically, and a
manual Selector task submitted through the macOS Bot roster produced one accepted CLI receipt. This
result qualifies terminal access but predates ADR 0002. The cron fleet and its three-full-Review and
living-review gates are retired with 0.6.0, and gateway profile multiplexing is now reported by
doctor without gating readiness, because a step starts its own session.

### Extractor qualification (2026-09-16, 0.5.7)

A bounded linked Run on `jkworkstation` acquired three full texts and recorded eight extraction and
appraisal pairs. A separate reviewer verified all 8 extraction quotes, 31 appraisal quotes, locators,
and numerical transcriptions, but found that one trial's Mini-CEX and satisfaction outcomes were never
extracted. That gap motivated the 0.5.8 outcome-decision contract. The run is retained unchanged as
provenance; it is not a qualification of 0.5.8.

### 0.5.8 host smoke test (2026-09-17)

In an isolated store on `jkworkstation`, the three extractor-qualification articles were seeded
with screening, coverage, and study links, and the 0.5.8 Extractor runner was started. The runner
acquired all three full texts deterministically without a model session and routed straight into
the first assessment. The session followed the per-outcome procedure: it searched every protocol
outcome with `source find` and read the exam paragraph and both Mini-CEX and satisfaction tables
with `source read`. It then spent most of 30 turns reading installed package source to learn allowed
field values, and single model turns took 10 to 60 seconds. The attempt was stopped before
submission. These findings produced 0.5.9: packet `field_rules`, and claim renewal during long
sessions.

### 0.5.9 host smoke test (2026-09-17)

The same seeded store was rerun on 0.5.9. The Extractor session read `field_rules` instead of
package source and decided all seven protocol outcomes for PMID 42700004: knowledge test score,
Mini-CEX clinical skills, and satisfaction were extracted, which closes the qualification gap;
cognitive load was marked not reported with eight inspected locations; and one untouched scaffold
was pruned. The first submission was rejected for one missing `effect.missing_reason`, which
cascaded into eight misleading dependent-stage errors; the session patched the row and the second
submission was accepted in one attempt, after about 32 minutes with a shared model endpoint.
Review of the stored rows found a chi-square statistic recorded as a between-group effect and a
discussion remark extracted as an outcome. These findings produced 0.5.10.

The production refresh Cycle for `ai-med-ed-evidence-report-v4` reused 30 of 46 prior screening
decisions; the other 16 differed only in citation counts and source result positions, which 0.5.10
treats as volatile for future refreshes.

## Repository and release record

On 2026-09-17 the Hermes line split from the Claude Code/Codex plugin line. GitHub
`junhewk/medical-deep-research-plugin` was renamed `junhewk/hermes-medical-research`; the 0.4.0
plugin line remains on branch `legacy/claude-codex-0.4` and tag `v0.4.0`. Releases are Git tags
installed with `uv tool install` or `pipx install` from the repository. There is no PyPI or
plugin-ZIP release in this design. The 0.6.0 host facts are recorded above, the tool-server listing
standalone; the re-screening comparison remains open until it is recorded here.
