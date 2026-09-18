---
name: hmr-synthesize
description: Synthesize one protocol outcome from its bound extractions through hmr.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Synthesize one outcome

You work on exactly one claimed task. A runner gives you an instruction file with `run_id`,
`task_id`, `packet_path`, `proposal_path`, and exact commands. Without one, run
`hmr --actor hmr-synthesizer synthesize claim`. If it returns `state: idle`, return `[SILENT]`. Use
only the packet, proposal, and supplied commands, replacing only their uppercase placeholders.

The Synthesizer completes exactly one assigned outcome. The packet summarizes each bound extraction
and lists `unreported_dispositions`: records whose sources were inspected and did not report this
outcome. Use them when describing gaps. Read a full row with `source_show` when a summary is short.
The packet's `field_rules` lists every allowed value; never read package source code to learn it.

## Rules the submission enforces

These are what a real run was rejected for. Satisfy them before submitting, not after.

- `conclusion` is capped at 600 characters. Count it.
- `limitations` is a required array even when the proposal template left it out.
- With `comparator_type: mixed`, an extraction whose own comparator differs from the finding's,
  including an `inactive` one such as a conventional lecture, must use `use: indirect`, not
  `direct`. `direct` requires the same comparator type. `relationship: supports` is unaffected.
- `overlap.status: mapped` needs source-grounded mappings; a quotation inside a rationale does not
  satisfy it. When every contributing extraction comes from a distinct record, use
  `not_applicable` and say in the rationale that the records are distinct.
- If the contributing extraction's appraisal is `completion: limited`, or all of its domains are
  `not_assessed`, the only acceptable certainty rating is `not-assessable`. Say why in
  `rating_explanation`.

Edit only the proposal with the file tool and run the exact `submit` command. On an unrecoverable
error, run the exact `fail` command. Return only the `run_id`, `task_id`, and recorded state. Never
send corpus or report content between Bots, edit skills, or run scripts. A skill file is read-only
for you even when it is writable: what you learn belongs in your answer, not in these instructions.

When the instruction file names a typed submit tool, call it with one `result` object instead of editing the proposal and running a command. It is checked the same way and costs fewer turns.
