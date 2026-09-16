---
name: medical-synthesize
description: Synthesize one outcome or independently audit one frozen evidence group through mdr.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical synthesize and audit

Run the claim command matching your profile: `mdr --actor mdr-synthesizer synthesize claim` or
`mdr --actor mdr-auditor audit claim`. If it returns `state: idle`, return `[SILENT]`. Otherwise use
only the returned packet, proposal, and exact source commands.

The Synthesizer completes exactly one assigned outcome. The Auditor independently checks every
assigned frozen target and never corrects author work. Edit only the proposal and run the exact
returned `submit` command. On an unrecoverable error, run the returned `fail` command. Return only
the `run_id`, `task_id`, and recorded state. Never send corpus or report content between Bots.
