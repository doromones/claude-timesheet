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
| grouped by project / repo                      | `--by project` |
| by project, listing each project's sessions    | `--by project --group session` |
| activity intervals / when was I working / a timeline of work blocks | `--by interval` |
| those intervals grouped under each session     | `--by interval --group session` |
| per session (the default)                     | `--by session` |
| a specific project                            | `--project <substring>` |
| custom idle threshold                         | `--cap <minutes>` |

Combine freely — "how much on the login branch this week" →
`--branch login --week`.

**Default when the user gives no specifics** — a bare `/timesheet:timesheet`, or
a vague "my timesheet" / "покажи час" with no period, grouping or filter named —
run `--today --by project --group session` (today, projects with their sessions
listed). As soon as the user names a period (week, a date range, all history), a
different grouping (by branch / day / interval), or a filter, use what they said
instead of this default.

## Reading the numbers

- Sessions are grouped by their git **branch**; a session is not necessarily
  tied to any issue, and a branch may just be `main`/`develop`/`feature/x`.
- `Active` — real working time; idle gaps longer than the cap (default 20 min)
  are dropped.
- `Span` — first to last event of the day; includes idle, so always larger.
- `--by day` shows `Active (real)` (overlapping sessions merged into real
  wall-clock) versus `Active (sum)` (plain sum across sessions).
- `--by interval` lists each activity block (a continuous run of work with no
  idle gap over the cap) with its start–end time, duration and branch. Default
  `--group day` lays them on a per-day timeline; `--group session` lists each
  session's blocks under its own header. Parallel sessions show as overlapping
  rows.
- The grand-total row plus the "Real working time…" line give the whole-selection
  totals; per-session rows can sum to more than the real total when sessions ran
  in parallel.

The engine is dependency-free Python 3 (stdlib only); no setup required.
