---
name: medical-select
description: Screen or select one supplied medical record through a bounded mdr task.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical select

The serial runner supplies an instruction-file path for one already claimed article. Read that file,
then read only its bounded packet and proposal template. Use only its `source_list` and `source_show`
commands for additional detail. Complete the assigned decision, editing only the proposal, and run
the exact `submit` command. On an unrecoverable error, run the exact `fail` command. Return only the
`run_id`, `task_id`, and recorded state. Do not claim or process another task in this session, and
never inspect the artifact store directly. The host runner starts a fresh Selector session for the
next article immediately after this one is accepted.

Outside the serial runner, claim one article with `mdr --actor mdr-selector select claim`. If it
returns `state: idle`, return `[SILENT]`; otherwise follow the same one-article boundary.

Pitfalls: screening-stage `decision` must be one of `include`, `exclude`, or `uncertain`, while
coverage-stage `selection` must be one of `selected`, `deferred`, or `unavailable`. If validation
returns the allowed values, correct the proposal and resubmit it with the same claim token.

For operator diagnostics only, an explicitly supplied `run_id` and `task_id` may be opened with
`mdr select next RUN_ID TASK_ID` when the Run is not cron-managed.
