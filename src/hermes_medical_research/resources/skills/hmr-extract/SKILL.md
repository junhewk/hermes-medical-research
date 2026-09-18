---
name: hmr-extract
description: Acquire full text, link studies, extract one record's results, and appraise them, using only the bounded task packet and commands.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical extract

A step runner supplies an instruction-file path for one already claimed task. Read that file first.

When it lists `read_tools`, every read is a tool call: `packet_read` for the packet, `source_find`
to rank locators by search words, `source_read` for one locator's exact text, `source_list` for the
document ids. They resolve the claimed task themselves, so they take no run or task id. Do not read
the packet or the proposal as files, and do not run a shell command to read a source: the typed
tools own the proposal, so reading it only spends a turn.

When it lists no `read_tools`, use only the instruction file's own `source_find`, `source_read`,
`source_list`, and `source_show` commands, replacing only their uppercase placeholders.

Never run Python, heredocs, scripts, or a code cell, and never edit a skill.

## Assessment: record one outcome at a time

Call `record_study_appraisal` once with the study's risk of bias, then one call per protocol outcome
in `outcome_checklist`:

- `record_outcome_extracted` when the record reports that outcome. Give the estimate, the quote that
  states it, and the document and locator you read.
- `record_outcome_missing` when it does not, with the status, one sentence of rationale, and the
  locations you inspected.

Each call is checked on its own and its reply lists the outcomes that remain. Trust that list: an
outcome it no longer names is recorded, so recording it again only spends a turn. The task submits
itself when every outcome is decided. Do not rewrite the proposal file to do this work: a rejected
call costs one short answer, a rejected file costs the whole file.

## Acquisition and linking

A `fulltext` task is submitted by the runner with no model work. For `studies`, set the study kind
and basis, and link to an existing study only on explicit identity evidence such as a shared
registration number. Use `submit_study_link` when the instruction file names it.

Learn every allowed value from the packet's `field_rules`, never from source code or `--help`. On an
unrecoverable error run the exact `fail` command. Return only the `run_id`, `task_id`, and recorded
state.
