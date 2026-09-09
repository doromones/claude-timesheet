# timesheet — time tracking for Claude Code

A Claude Code plugin that reports how much time you actually spent working,
derived from your local session transcripts under `~/.claude/projects`. No
logging to set up: it reads history that Claude Code already keeps.

The plugin ships as a **skill**, so you just ask in plain language — no flags to
memorize:

> how much did I work today
> hours this week per day
> time spent on the login branch

Claude runs the report and shows it as a table. You can also invoke the skill
explicitly as `/timesheet:timesheet` and describe what you want. The script
prints English; when you chat in another language, Claude presents the table's
labels in that language.

## What it measures

Each session transcript carries a timestamp on every event. From that the
report derives two numbers:

- **Active** — the sum of gaps between consecutive events, **dropping any gap
  longer than the idle cap** (default 20 min). This approximates real time at
  the keyboard and ignores lunch / overnight pauses.
- **Span** — first to last event of the day. Simpler, but counts idle time, so
  it is always an upper bound.

Days are bucketed by **local** time, not UTC.

Sessions are grouped by their git **branch** — a session is not necessarily tied
to any issue, and a branch may just be `main` / `develop` / `feature/x`. When a
branch follows a `KEY-123-description` convention, it is collapsed to `KEY-123`.
Each event carries the branch checked out at that moment, so a session that
switched branches has its time **split across them**: `--by branch` and `--by
day` attribute each interval to the branch that was active during it, and the
per-session row is labelled by the branch where most of its time was spent
(`+N` when it touched others).

Every report ends with a whole-selection total. When sessions ran in parallel,
per-session rows can add up to more than the real total, so `--by day` shows
both **Active (real)** — overlapping intervals merged into true wall-clock — and
**Active (sum)** — the plain per-session sum.

## Options (what the skill maps to, and how to run it directly)

The skill drives a dependency-free Python 3 script — run it yourself if you
prefer explicit flags:

```
python3 skills/timesheet/timesheet.py                     # per session, all history
python3 skills/timesheet/timesheet.py --week              # last 7 days
python3 skills/timesheet/timesheet.py --today
python3 skills/timesheet/timesheet.py --since 2026-01-01 --until 2026-01-31
python3 skills/timesheet/timesheet.py --branch login      # sessions on branches matching "login"
python3 skills/timesheet/timesheet.py --project myapp      # one project (substring)
python3 skills/timesheet/timesheet.py --by branch          # roll sessions up per branch
python3 skills/timesheet/timesheet.py --by project         # roll sessions up per project/repo
python3 skills/timesheet/timesheet.py --by project --group session  # sessions under each project
python3 skills/timesheet/timesheet.py --by day             # per day (real + sum)
python3 skills/timesheet/timesheet.py --by interval        # timeline of activity blocks
python3 skills/timesheet/timesheet.py --by interval --group session  # blocks under each session
python3 skills/timesheet/timesheet.py --cap 30             # 30-minute idle cap
```

Grouping modes:

| `--by`    | One row per…       | Columns                                        |
|-----------|--------------------|------------------------------------------------|
| `session` | session (default)  | branch, session, title, active, span, per day  |
| `branch`  | git branch         | branch, title, sessions, active, span, per day |
| `project` | project / repo     | project, sessions, active, span, per day       |
| `day`     | calendar day       | active (real), active (sum), span, sessions    |
| `interval`| activity block     | start–end time, duration, branch, session      |

## Install

In a terminal `claude` session (the interactive `/plugin` command is not
available in every surface, but the CLI is):

```
claude plugin marketplace add doromones/claude-timesheet
claude plugin install timesheet@claude-timesheet
```

Then restart Claude Code so the skill loads. The config is shared, so the
desktop app picks it up too.

## Tests

```
python3 tests/test_timesheet.py
```

## License

MIT
