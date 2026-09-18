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
`hmr --actor hmr-extractor extract claim`; if it returns `state: idle`, return `[SILENT]`. Finish
by returning only the `run_id`, `task_id`, and recorded state.

Rules:
- Use only the supplied commands and files. Replace uppercase placeholders such as SEARCH WORDS,
  DOCUMENT_ID, and LOCATOR; change nothing else in a command.
- The packet's `field_rules` lists every allowed value and cross-field rule. Never read package
  source code or `--help` output to learn the schema.
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
3. If reported, fill the prefilled extraction row whose `protocol_outcome` matches it. `outcome` is
   the paper's own label. For another estimand of the same outcome, copy the filled row with a new
   `extraction_id` such as `result-...-o2b` and add `{extraction_id, same_as}` for it.
   Fill the one full `study-appraisal-...` row once; outcome rows copy it through `same_as`. Write
   a full appraisal for an outcome only when its risk of bias differs.
4. Set that outcome's `dispositions` status to `extracted`. hmr fills `extraction_ids`.
5. If the study did not measure it, set status `not_reported`, or `not_applicable` when the design
   cannot measure it. Discussion or limitation remarks are not a measured outcome. Add a one-sentence rationale and `inspected_locations` with each `document_id` and
   `locator` you read. Leave that outcome's scaffold row unchanged; hmr removes it.

Single-arm or intervention-only surveys stay descriptive: use `effect.basis` group_summary or
qualitative, never between_group. Keep the prefilled appraisal method unless the design clearly
needs another method from the packet's `methods`.
