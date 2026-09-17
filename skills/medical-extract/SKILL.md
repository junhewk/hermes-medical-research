---
name: medical-extract
description: Acquire, link, extract, and appraise one bounded medical evidence task.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical extract

You work on exactly one claimed task. A runner gives you an instruction file with `run_id`,
`task_id`, `packet_path`, `proposal_path`, and exact commands. Without one, run
`mdr --actor mdr-extractor extract claim`; if it returns `state: idle`, return `[SILENT]`. Finish
by returning only the `run_id`, `task_id`, and recorded state.

Rules:
- Use only the supplied commands and files. Replace uppercase placeholders such as SEARCH WORDS,
  DOCUMENT_ID, and LOCATOR; change nothing else in a command.
- Edit only the proposal file, with the file tool. Never edit skills, packets, or run files.
- Never run Python, heredocs, or scripts, and never page `source_show` to slice JSON.
- Quote verbatim from `source_read` text at the cited `document_id` and `locator`.
- On a validation error, fix the named fields and run the same `submit` command again.
- Run the exact `fail` command only when the task truly cannot be completed.

`fulltext` task: submit the proposal unchanged.

`studies` task: set `kind` (primary, systematic-review, guideline, other) and `basis`. Add the
record to an existing study only with explicit identity evidence such as the same registration.

`assessment` task: decide every entry of the packet's `outcome_checklist`.
1. Run `source_find` with the outcome's words and instrument names, and check
   `likely_locations`.
2. Run `source_read` on the best hits. Results are often in `table:N` or results paragraphs.
3. If reported, fill the prefilled extraction row whose `protocol_outcome` matches it and fill its
   appraisal. `outcome` is the paper's own label. For another estimand of the same outcome, copy
   the filled pair with a new `extraction_id` such as `result-...-o2b`.
4. Set that outcome's `dispositions` status to `extracted`. mdr fills `extraction_ids`.
5. If not reported, set status `not_reported`, or `not_applicable` when the design cannot measure
   it. Add a one-sentence rationale and `inspected_locations` with each `document_id` and
   `locator` you read. Leave that outcome's scaffold row unchanged; mdr removes it.

Single-arm or intervention-only surveys stay descriptive: use `effect.basis` group_summary or
qualitative, never between_group. Keep the prefilled appraisal method unless the design clearly
needs another method from the packet's `methods`.
