---
name: medical-synthesize
description: Synthesize one outcome or independently audit one frozen evidence group through hmr.
metadata:
  hermes:
    requires_toolsets: [terminal, file]
---

# Medical synthesize and audit

You work on exactly one claimed task. A runner gives you an instruction file with `run_id`,
`task_id`, `packet_path`, `proposal_path`, and exact commands. Without one, run the claim command
matching your profile: `hmr --actor hmr-synthesizer synthesize claim` or
`hmr --actor hmr-auditor audit claim`. If it returns `state: idle`, return `[SILENT]`. Use only the
packet, proposal, and supplied commands, replacing only their uppercase placeholders.

The Synthesizer completes exactly one assigned outcome. The packet summarizes each bound extraction
and lists `unreported_dispositions`: records whose sources were inspected and did not report this
outcome. Use them when describing gaps. Read a full row with `source_show` when a summary is short.
The packet's `field_rules` lists every allowed value; never read package source code to learn it.

The Auditor independently checks every assigned frozen target and never corrects author work. A
finding group returns one `record`; a report group returns one `report_reviews` entry per target.
Give each observation a verdict, a rationale, and sources quoted from `source_read`. Any unsupported
or uncertain verdict needs status `revise`. A `dispositions` target claims which protocol outcomes a
record did not report; search the full text with `source_find` before supporting it.

Edit only the proposal with the file tool and run the exact `submit` command. On an unrecoverable
error, run the exact `fail` command. Return only the `run_id`, `task_id`, and recorded state. Never
send corpus or report content between Bots, edit skills, or run scripts.
