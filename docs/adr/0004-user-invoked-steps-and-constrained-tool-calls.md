# ADR 0004: User-invoked steps and constrained tool-call submissions

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

Two properties of the local model server shape the answer surface, both measured on the operator's
own gateway. First, llama.cpp compiles a real grammar only for non-string tool arguments, and a
string enum counts as a string: a nested `result` object with `decision` and `reason` inside it
binds, while a flat `decision` enum does not. Second, enforcement needs `tool_choice: required`,
because under `auto` the tool grammar stays lazy until the model opens a tool call, and a model that
answers in prose never opens one. A schema in `response_format` was the obvious alternative and does
not compose: the gateway returns HTTP 400 for tools plus `response_format`, and llama-server drops
the schema when tools are present. llama.cpp also ignores a schema while thinking is enabled.

## Decision

Work advances only when the operator runs a **Step**. A step claims and answers one stage's Tasks in
the active Review until that queue is empty, then stops. `hmr hermes commands --apply` installs one
quick command per step into the host Hermes config as a marked, digest-owned region. Because a quick
command has 30 seconds and no arguments, a step starts a detached worker and returns one line: the
active Review is a pointer at `<store>/steps/active.json` set by `hmr step use NAME`, and progress,
failures and the next command live in `<store>/steps/<review>/<step>/status.json`.

Inside a step every item is still one claim, one Lease and one `TaskEngine.submit`. Only the **Lane**
that produces the answer changes. `call` is one fresh tool-free session whose profile exposes exactly
that kind's MCP submit tool and pins `extra_body.tool_choice: required`; it covers screening,
coverage, study linking and synthesis. `agent` is one fresh session with the role's skill and shell
tools; it covers per-outcome assessment, audit and every audit correction. `none` calls no model at
all; it covers search and full-text acquisition. Synthesis falls back to `agent` when its packet was
shortened to fit.

A submit tool takes one argument, `result`, an object, and its schema uses only the keyword subset
the grammar converter handles predictably. The choice of a tool payload over `response_format` is
also a portability decision: a JSON-Schema `parameters` block is how tools work on OpenAI, vLLM,
SGLang and Ollama, so the design does not depend on one server. Reasoning is off in a constrained
call.

Authority does not move. The schemas buy shape only. Every value, identifier, digest and cross-field
rule is still checked by `validation` and `evidence` when the mapped proposal reaches
`TaskEngine.submit`, and a rejection comes back to the model as the validator's own message. Identity
and provenance fields are never taken from a payload. Two constrained attempts escalate to one
session with tools before the durable failure path runs.

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
- The constrained profiles are protocol-independent. A profile's `extra_body` and submit tool are
  fixed at bootstrap time, so a schema can never encode a Review's outcomes or record ids, and every
  protocol-specific rule stays in the validators.
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
- **A schema in `response_format`.** Measured on the operator's gateway: HTTP 400 for tools plus
  `response_format`, and llama-server drops the schema when tools are present. It also would not
  reach the store, since the submission still has to go through a validated submit path.
- **A flat enum argument on the submit tool.** llama.cpp compiles a grammar only for non-string tool
  arguments, and a string enum counts as a string, so a flat `decision` enum is unconstrained.
- **`tool_choice: auto` with a strong prompt.** The tool grammar stays lazy until the model opens a
  tool call. That is the configuration that produced 7 prose answers out of 170.
- **One long-lived session per stage.** It would amortize the prefix but keep context between items,
  which breaks the one-item-per-Task boundary that makes a receipt mean something. The prompt prefix
  is instead byte-identical across the items of one Cycle, so the server's own prompt cache does that
  work.
- **A constrained call for assessment and audit.** Both must read arbitrary full text through bounded
  source commands, which a single tool-free call cannot do.
