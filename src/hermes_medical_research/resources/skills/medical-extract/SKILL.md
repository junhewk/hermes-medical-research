---
name: medical-extract
description: Acquire, link, extract, and appraise one bounded medical evidence task.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical extract

For cron work, run `mdr --actor mdr-extractor extract claim`. If it returns `state: idle`, return
`[SILENT]`. Otherwise use only its packet, proposal, and the returned source commands. Fill every assigned field, preserve
verbatim quotes and precise source locations, and explicitly mark missing information. Edit only the
proposal and run the exact returned `submit` command. On an unrecoverable error, run the returned
`fail` command. Return only the `run_id`, `task_id`, and recorded state.

For an assessment task, treat the initial extraction and appraisal rows as scaffolds, not a one-row
limit. Inspect the full assigned document for every selected protocol outcome. Add a distinct
extraction row and a matching appraisal row for each relevant reported estimand, including usable
secondary and structured learner-experience outcomes. Keep noncomparative surveys descriptive and
do not pool them as comparative effects.
