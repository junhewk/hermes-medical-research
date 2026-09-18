---
name: medical-select
description: Screen or select one supplied medical record through a bounded hmr task.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical select

The serial runner supplies an instruction-file path for one already claimed article. Read that file,
then read only its bounded packet and proposal template. For more detail, use only its
`source_find` (search words), `source_read` (one document locator), `source_list`, and `source_show`
commands, replacing only their uppercase placeholders. Complete the assigned decision, editing only
the proposal with the file tool, and run the exact `submit` command. On an unrecoverable error, run
the exact `fail` command. Return only the `run_id`, `task_id`, and recorded state. Do not claim or
process another task in this session, never inspect the artifact store directly, and never edit
skills or run scripts. The host runner starts a fresh Selector session for the next article
immediately after this one is accepted.

Outside the serial runner, claim one article with `hmr --actor hmr-selector select claim`. If it
returns `state: idle`, return `[SILENT]`; otherwise follow the same one-article boundary.

Pitfalls: screening-stage `decision` must be one of `include`, `exclude`, or `uncertain`, while
coverage-stage `selection` must be one of `selected`, `deferred`, or `unavailable`. Coverage
`protocol_outcomes` is only a hint for later assessment; list the outcomes the record appears to
report. If validation returns the allowed values, correct the proposal and resubmit it with the same
claim token.

For operator diagnostics only, an explicitly supplied `run_id` and `task_id` may be opened with
`hmr select next RUN_ID TASK_ID` when the Run is not cron-managed.
