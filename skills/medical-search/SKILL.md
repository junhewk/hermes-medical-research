---
name: medical-search
description: Perform one deterministic medical-search task through the mdr CLI.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical search

For cron work, run `mdr search claim`. If it returns `state: idle`, return `[SILENT]`. Otherwise
inspect only the returned packet and proposal template, and modify only that proposal. Preserve a
prefilled frozen plan exactly on refresh Cycles. Run the exact returned `submit` command and then the
same claim-scoped `search run` command. If approval or all-results confirmation is requested, pass
the exact returned digest or token. On an unrecoverable error, run the returned `fail` command.
Return only the `run_id`, `task_id`, and recorded state. Never edit run artifacts or pass corpus text
in Bot messages.
