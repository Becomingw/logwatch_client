# LogWatch User API Reference (for Monitoring)

## Auth (required for user-level API)

- `Authorization: Bearer <user_token>`
- `X-User-Id: <user_id>`

## Health

- `GET /api/health`
- No auth required.
- Returns: `{"ok": true, "message": "healthy"}`

## Public Config

- `GET /api/config`
- No auth required.
- Returns: `{"lost_grace_seconds": 3, "code_cooldown_seconds": 60}`

## Task Monitoring

1. `GET /api/tasks`
- Return all tasks for current user.
- Key fields: `id`, `name`, `status`, `runtime_status`, `connectivity_status`, `machine`, `started_at`, `ended_at`, `exit_code`, `last_heartbeat`, `heartbeat_interval`, `command`, `cwd`, `pid`, `python_version`, `is_pinned`.

2. `GET /api/tasks/{task_id}`
- Return one task with same core fields.

3. `GET /api/tasks/{task_id}/log?after_id=<n>&limit=<m>`
- Incremental mode: fetch logs after cursor `after_id`, up to `limit` rows.
- Key fields: `task_id`, `log`, `last_id`, `first_id`, `returned_entries`, `query`, `status`, `runtime_status`, `connectivity_status`, `last_heartbeat`, `ended_at`.

4. `GET /api/tasks/{task_id}/log?latest=<n>`
- Latest mode: fetch the most recent `n` log entries.
- Same response shape as incremental mode, with `query.mode = "latest"`.

5. `GET /api/tasks/{task_id}/history?limit=<n>`
- Return status transition history (connectivity online/lost changes).
- Returns: `{"task_id": "...", "history": [{"status_category": "connectivity", "status_value": "lost", "changed_at": "...", "note": "..."}]}`.

6. `GET /api/log/last-ack?task_id=<id>&user_id=<user_id>`
- Return `last_ack_seq` for client-side resume.

## Dashboard

- `GET /api/dashboard/summary`
- Rich overview with current counts, cumulative stats, activity heatmap, insights, and recent tasks.
- Key fields:
  - `current`: `{total, running, success, failed}`
  - `cumulative`: `{total, success, failed}`
  - `activity`: `{days: [{date, count, success, failed}], max_count}`
  - `insights`: `{active_days, current_streak, longest_streak, best_day_date, best_day_count, last_active_date, success_rate, deleted_total}`
  - `recent_tasks`: `[{id, name, machine, started_at, display_status, is_deleted, can_purge}]`

## Task Management

1. `DELETE /api/tasks/{task_id}`
- Delete a task. Admin can delete any; regular user can only delete own.

2. `POST /api/tasks/{task_id}/pin`
- Pin (置顶) a task.

3. `DELETE /api/tasks/{task_id}/pin`
- Unpin a task.

4. `DELETE /api/tasks/{task_id}/purge`
- Permanently purge a deleted task's data.

## Interpretation Notes

- Prefer `runtime_status` over `status` when both exist.
- `runtime_status` values: `running`, `lost`, `success`, `failed`.
- `connectivity_status` values: `online`, `lost`, `ended`.
- Typical status path: `running -> lost -> running` (recover) or `running/lost -> failed`.
- `409` in push paths usually means task has been deleted server-side.

## Smart Search Strategy (Skill Script)

- Command: `search --query "..."`
- Match fields: `name`, `command`, `cwd`, `machine`, `id`, `runtime_status`, `status`, `python_version`
- Weighted ranking: `name` and `command` are higher weight
- Alias expansion example:
  - `nnunet` -> `nnunet`, `nn-unet`, `nnunetv2`, `nnunetv1`, `nnunet_train`, `nnunetv2_train`
- Optional log-aware rerank:
  - `--with-log` probes top candidates and applies log-content bonus score
