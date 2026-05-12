---
name: logwatch-monitor
description: Monitor LogWatch user-scoped tasks and inspect specific run details via authenticated APIs. Use when asked to periodically check task runtime_status, query a task by id, fetch incremental/latest logs, view dashboard insights, inspect status transition history, or troubleshoot user task transitions (running/lost/success/failed) in LogWatch.
---

# Logwatch Monitor

## Overview

Use this skill to monitor LogWatch task state for a single user, inspect one task in detail, fetch incremental or latest logs, view dashboard summaries with activity insights, and manage tasks through user-level APIs.

## Workflow

1. Confirm monitoring target.
- Ask for `server`, `user_id`, and `user_token` when missing.
- Default `server` to `http://127.0.0.1:8000` only if the user has local deployment context.

2. Validate connectivity first.
- Call `GET /api/health` before user-level queries.
- If health fails, report server unavailability and stop API diagnosis.

3. Choose query mode.
- Use **dashboard mode** when user asks "整体概况 / 成功率 / 活跃度 / 连续天数 / 仪表盘".
- Use **summary mode** when user asks "overall status / 任务看板 / 定时巡检".
- Use **task mode** when user provides `task_id` or asks for a specific run.
- Use **log mode** when user asks for "最新日志 / 增量日志 / 续传游标".
  - Use `--latest N` for quick tail view; use `--after-id` for incremental polling.
- Use **history mode** when user asks "状态变迁 / 什么时候断连 / 连接历史".
- Use **search mode** when user asks by project keyword (e.g. `nnunet`, `liver`, `fold0`) without `task_id`.
- Use **delete mode** when user explicitly requests task deletion.

4. Execute with unified auth headers.
- Always send:
  - `Authorization: Bearer <user_token>`
  - `X-User-Id: <user_id>`
- Never print full token in output; mask except prefix.

5. Report concise results.
- For dashboard: return current/cumulative counts, success rate, streaks, and recent tasks.
- For summary: return counts by `runtime_status` and list key tasks.
- For task: return lifecycle fields and runtime health fields.
- For logs: return `last_id`, `runtime_status`, and log excerpt size.
- For history: return status transitions with timestamps and notes.
- For search: return scored candidates, matched fields/tokens, and top task conclusion.
- For delete: confirm deletion result.

## Commands

Use the bundled script for deterministic querying:

```bash
# Dashboard overview with insights and activity
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py dashboard \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx
```

```bash
# Task summary (list with status counts)
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py summary \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx
```

```bash
# Single task detail
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py task \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --task-id <task_id>
```

```bash
# Incremental log fetch
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py log \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --task-id <task_id> --after-id 0 --limit 200
```

```bash
# Latest N log entries (quick tail)
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py log \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --task-id <task_id> --latest 50
```

```bash
# Task status transition history
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py history \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --task-id <task_id>
```

```bash
# Delete a task
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py delete \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --task-id <task_id>
```

```bash
# Continuous watch polling
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py watch \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --interval 10 --iterations 0
```

```bash
# Smart search by keyword
python3 skills/logwatch-monitor/scripts/logwatch_api_monitor.py search \
  --server http://127.0.0.1:8000 --user-id 104698 --user-token ut_xxx \
  --query "nnunet liver" --with-log --limit 5
```

## References

- Endpoint and field reference: `references/api-reference.md`
