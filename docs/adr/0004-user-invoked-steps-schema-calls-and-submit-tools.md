# ADR 0004: User-invoked steps, schema-constrained calls, and typed submit tools

- Status: accepted
- Date: 2026-09-18
- Supersedes: ADR 0002's cron Routines as the work surface; ADR 0003's serial worker runners and its
  three-clean-run cron qualification gate

## Context

0.5.11 drove six persistent Hermes bot profiles through cron Routines. Measured on the real
170-record review on the operator's host, that design was unusable. Screening took about 90 seconds
per record, an extracted article took about 15 minutes, and 9 hours of running did not finish the
Cycle. The cost was structural: every item was a multi-turn agent session that read an instruction
file, hand-edited `proposal.json`, and shelled out to a submit command, so the model spent most of
its turns on plumbing rather than on the judgement the Task asked for.

One tool-free one-shot per record screened all 170 records in 20.2 minutes, 7.1 seconds mean, and
agreed with the bot Selector on 152 of the 163 answers that could be parsed. The remaining 7 of 170
answers were prose rather than JSON. Asking for a shape is therefore not enough; the shape has to be
enforced by the server.

Two host constraints shape the operator surface. A `type: exec` Hermes quick command is killed after
30 seconds on the CLI and on the messaging path, and it receives no arguments. So a command cannot
carry a Review name, and it cannot hold a stage's worth of work open.

Three facts about the host, all measured on the operator's own gateway, shape the answer surface.
First, Hermes proxies an MCP tool behind its own meta-tools: the probe session called `tool_describe`
and then `tool_call`, about three model turns for one record instead of one, and it also reached for
skill tools. Second, a tool-free session whose profile carried the kind's schema in
`extra_body.response_format` answered the same record in one call and 13 seconds, and its decision
enum held against a prompt that demanded an out-of-enum value and a 300-word reason. Third, the two
do not compose: the gateway returns HTTP 400 for tools plus a response format, llama-server drops the
schema when tools are present, and llama.cpp ignores a schema while thinking is enabled.

Hermes also sends only the tools it already knows about. There is no way to hand it a tool schema per
invocation, so a typed submit tool has to be an MCP server or an in-process plugin.

## Decision

Work advances only when the operator runs a **Step**. A step claims and answers one stage's Tasks in
the active Review until that queue is empty, then stops. `hmr hermes commands --apply` installs one
quick command per step into the host Hermes config as a marked, digest-owned region. Because a quick
command has 30 seconds and no arguments, a step starts a detached worker and returns one line: the
active Review is a pointer at `<store>/steps/active.json` set by `hmr step use NAME`, and progress,
failures and the next command live in `<store>/steps/<review>/<step>/status.json`.

Inside a step every item is still one claim, one Lease and one `TaskEngine.submit`. Only the **Lane**
that produces the answer changes. `call` is one fresh session with no tools, whose profile pins that
kind's answer schema in `extra_body.response_format` on the provider entry it selects; the model
returns one JSON object, and the step runner writes only the fields the schema owns. It covers
screening, coverage, study linking and synthesis. `agent` is one fresh session with the role's skill,
shell tools and the role's typed submit tools; it covers per-outcome assessment, audit and every
audit correction. `none` calls no model at all; it covers search and full-text acquisition. Synthesis
falls back to `agent` when its packet was shortened to fit.

The two answer surfaces are split on the measured proxy overhead. A decision that needs no tools is
one request with a schema on it, and that is both cheaper than three turns through `tool_describe`
and `tool_call` and simpler, because nothing has to be proxied. A schema in `response_format` is also
what OpenAI-compatible servers accept, so the `call` lane does not depend on one server. The session
Lane cannot be constrained the same way: tools plus a response format is refused with HTTP 400, so a
role that must read arbitrary full text keeps its tools and records its answer by calling a typed
submit tool whose single `result` argument is the same schema. Both shapes come from one table in
`answers`, so they cannot drift apart. Reasoning is off in a constrained call.

Authority does not move. The schemas buy shape only. Every value, identifier, digest and cross-field
rule is still checked by `validation` and `evidence` when the mapped proposal reaches
`TaskEngine.submit`, and a rejection comes back to the model as the validator's own message, appended
after the record so the cached prompt prefix survives the retry. Identity and provenance fields are
never taken from a payload. Two constrained attempts escalate to one session with tools before the
durable failure path runs.

Cron holds no workflow authority. `hmr hermes routines` is removal-only, for migrating a host that
still has the 0.5.x fleet.

## Consequences

- A finding written in the `call` lane attests support from the packet's extraction and appraisal
  rows, not from a fresh read of the full text. That is why per-outcome assessment and audit keep
  sessions with tools, and why a shortened synthesis packet falls back to a session: the rows are
  then summaries, and the claim needs source reads a single call cannot make.
- Nothing runs unless an operator asks, so an unclaimed Task between two commands is normal.
  Abandonment blocking is gone, a step waits out the 5-minute and 30-minute backoff instead of
  reporting an empty queue, and an interrupted runner releases its claim without spending an
  attempt.
- A step claims only inside the Review it was pointed at, so one command cannot pull work into a
  Review the operator is not driving.
- Every answer shape is now read in two places, as a `response_format` for the `call` lane and as a
  tool `inputSchema` for the session Lane. Both are generated from the same table, and
  `hmr hermes doctor` reports each constrained profile's kind, schema digest, response format and
  provider entry, and flags a constrained profile that has any toolset.
- The constrained profiles are protocol-independent. A profile's `extra_body` is fixed at bootstrap
  time, so a schema can never encode a Review's outcomes or record ids, and every protocol-specific
  rule stays in the validators.
- An operator's per-kind model assignment has to be applied before the schema is pinned, because the
  schema goes on the provider entry the profile ends up selecting.
- The host surface is now wider than cron: a managed quick-command region and a profile-hosted stdio
  MCP server. Both are digest-owned, both refuse an unmanaged or edited entry, and the tool server
  resolves its own claim from the store, so no task id, path or token is written into a config file
  or a prompt.
- Three clean cron Reviews are no longer the qualification gate. The gates are the four host facts
  and the re-screening comparison recorded in `VALIDATION.md`.

## Alternatives considered

- **Keep the cron fleet and shorten the sessions.** The measured cost was in the session itself, not
  in the scheduler. A 90-second screening record does not become a 7-second one by trimming an
  instruction file.
- **A forced tool call for every constrained kind.** This was the accepted design until the host
  probe. It costs about three model turns per record, because Hermes proxies the tool behind
  `tool_describe` and `tool_call`, and the session also reached for skill tools. Had it been kept,
  the payload would have had to be a nested object: llama.cpp compiles a real grammar only for
  non-string tool arguments, and a string enum counts as a string, so `result` with `decision` and
  `reason` inside it binds while a flat `decision` enum does not. Enforcement would also have needed
  `tool_choice: required`, because under `auto` the tool grammar stays lazy until the model opens a
  tool call. The same nested payload is what the session Lane's submit tools take, for the same
  reason.
- **An in-process plugin instead of an MCP server.** Hermes sends only the tools it knows about, so
  the choice is between a plugin and a server; a plugin is not one of the public surfaces this
  project integrates through, and an MCP server keeps the package's dependency set unchanged.
- **A schema in `response_format` for the session Lane too.** The gateway returns HTTP 400 for tools
  plus `response_format`, and llama-server drops the schema when tools are present. A role that must
  read full text cannot give up its tools, so it cannot carry a schema.
- **One long-lived session per stage.** It would amortize the prefix but keep context between items,
  which breaks the one-item-per-Task boundary that makes a receipt mean something. The prompt prefix
  is instead byte-identical across the items of one Cycle, so the server's own prompt cache does that
  work.
- **A constrained call for assessment and audit.** Both must read arbitrary full text through bounded
  source commands, which a single tool-free call cannot do.
