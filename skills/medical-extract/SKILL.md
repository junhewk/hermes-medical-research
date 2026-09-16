---
name: medical-extract
description: Acquire, link, extract, and appraise one bounded medical evidence task.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical extract

For cron work, run `mdr extract claim`. If it returns `state: idle`, return `[SILENT]`. Otherwise use
only its packet, proposal, and the returned source commands. Fill every assigned field, preserve
verbatim quotes and precise source locations, and explicitly mark missing information. Edit only the
proposal and run the exact returned `submit` command. On an unrecoverable error, run the returned
`fail` command. Return only the `run_id`, `task_id`, and recorded state.
