---
name: medical-select
description: Screen or select one supplied medical record through a bounded mdr task.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical select

For cron work, run `mdr --actor mdr-selector select claim`. If it returns `state: idle`, return
`[SILENT]`. Otherwise read only the returned bounded packet and proposal template. Use only the returned `source_list` and
`source_show` commands for additional detail. Complete every assigned decision, editing only the
proposal, and run the exact returned `submit` command. On an unrecoverable error, run the returned
`fail` command with a concise code and message. Return only the `run_id`, `task_id`, and recorded
state. Never inspect the artifact store directly. After an accepted submission, immediately run
`mdr --actor mdr-selector select claim` again and process the next returned task. Continue until the
claim is idle or 10 tasks have been accepted in this invocation. Each article still receives its
own bounded packet, decision, reason, and immutable receipt.

Pitfalls: screening-stage `decision` must be one of `include`, `exclude`, or `uncertain`, while
coverage-stage `selection` must be one of `selected`, `deferred`, or `unavailable`. If validation
returns the allowed values, correct the proposal and resubmit it with the same claim token.

For operator diagnostics only, an explicitly supplied `run_id` and `task_id` may be opened with
`mdr select next RUN_ID TASK_ID` when the Run is not cron-managed.
