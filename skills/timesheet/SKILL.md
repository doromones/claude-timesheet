---
name: timesheet
description: Report how much time the user spent working in Claude Code — today, this week, a date range, per session, per git branch, or totals per day. Trigger on any natural-language ask about time spent or hours worked in Claude sessions, in any language (e.g. "how much did I work today", "hours this week", "time on the login branch", "time per day this month", "скільки я працював сьогодні", "сколько времени за неделю").
---

# Timesheet

Reports active working time derived from local Claude Code session transcripts
(`~/.claude/projects`). The report engine is `timesheet.py`, located in THIS
skill's own directory — its absolute path is given to you as the skill's base
directory when this skill loads. Do not compute times yourself; run the script.

## Steps

1. Translate the user's request into flags using the table below.
2. Run with Bash: `python3 "<this skill's base dir>/timesheet.py" <flags>`
   (use the absolute base-dir path you were given, not `$CLAUDE_PLUGIN_ROOT`).
3. The script prints a **Markdown table** (header row, a grand-total row, and a
   legend) in **English**. Present it to the user, **translating the labels into
   the language the user is chatting in** — column headers, the "Total" row
   label, the "Real working time…" line, and the legend. Keep everything else
   verbatim: branch names, session ids, session titles, dates and durations.
4. Show the table as-is otherwise; add at most a one-line takeaway.

## Request → flags

| User asks (any language)                     | Flags |
|----------------------------------------------|-------|
| today                                         | `--today` |
| this week / last 7 days                       | `--week` |
| a date range (from X to Y)                    | `--since YYYY-MM-DD --until YYYY-MM-DD` |
| a specific branch (names or implies a branch) | `--branch <substring>` |
| totals per day                                | `--by day` |
| grouped by branch                             | `--by branch` |
| per session (the default)                     | `--by session` |
| a specific project                            | `--project <substring>` |
| custom idle threshold                         | `--cap <minutes>` |

Combine freely — "how much on the login branch this week" →
`--branch login --week`. With no flags the script reports a per-session
breakdown across all sessions and projects. If grouping is ambiguous, default to
`--by session`; if the period is unstated, run without a date filter (all
history) and say so in one line.

## Reading the numbers

- Sessions are grouped by their git **branch**; a session is not necessarily
  tied to any issue, and a branch may just be `main`/`develop`/`feature/x`.
- `Active` — real working time; idle gaps longer than the cap (default 20 min)
  are dropped.
- `Span` — first to last event of the day; includes idle, so always larger.
- `--by day` shows `Active (real)` (overlapping sessions merged into real
  wall-clock) versus `Active (sum)` (plain sum across sessions).
- The grand-total row plus the "Real working time…" line give the whole-selection
  totals; per-session rows can sum to more than the real total when sessions ran
  in parallel.

The engine is dependency-free Python 3 (stdlib only); no setup required.
