#!/usr/bin/env python3
"""Monitor and inspect LogWatch tasks via user-level APIs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_SERVER = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 10
SEARCH_FIELD_WEIGHTS = {
    "name": 6,
    "command": 5,
    "cwd": 3,
    "machine": 2,
    "id": 2,
    "runtime_status": 1,
    "status": 1,
    "python_version": 1,
}
SEARCH_ALIASES = {
    "nnunet": ["nnunet", "nn-unet", "nnunetv2", "nnunetv1", "nnunet_train", "nnunetv2_train"],
    "nnunetv2": ["nnunetv2", "nnunet", "nn-unet", "nnunetv2_train"],
}


def _mask_token(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return token[:8] + "..."


def _headers(user_id: str, user_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {user_token}",
        "X-User-Id": user_id,
        "Accept": "application/json",
    }


def _request_json(
    method: str,
    server: str,
    path: str,
    user_id: str | None,
    user_token: str | None,
    params: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, Any]:
    base = server.rstrip("/")
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    url = f"{base}{path}{query}"

    headers = {"Accept": "application/json"}
    if user_id and user_token:
        headers.update(_headers(user_id, user_token))

    req = urllib.request.Request(url=url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return resp.status, None
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, body
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc)}


def check_health(server: str, timeout: int) -> bool:
    code, payload = _request_json("GET", server, "/api/health", None, None, timeout=timeout)
    if code == 200 and isinstance(payload, dict) and payload.get("ok") is True:
        return True
    return False


def cmd_summary(args: argparse.Namespace) -> int:
    code, payload = _request_json(
        "GET", args.server, "/api/tasks", args.user_id, args.user_token, timeout=args.timeout
    )
    if code != 200 or not isinstance(payload, list):
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2

    counts: dict[str, int] = {}
    for task in payload:
        status = str(task.get("runtime_status") or task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1

    top = payload[: max(1, args.limit)]
    result = {
        "ok": True,
        "server": args.server,
        "user_id": args.user_id,
        "token": _mask_token(args.user_token),
        "total": len(payload),
        "counts": counts,
        "tasks": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "runtime_status": t.get("runtime_status") or t.get("status"),
                "machine": t.get("machine"),
                "started_at": t.get("started_at"),
                "last_heartbeat": t.get("last_heartbeat"),
            }
            for t in top
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    path = f"/api/tasks/{urllib.parse.quote(args.task_id, safe='')}"
    code, payload = _request_json(
        "GET", args.server, path, args.user_id, args.user_token, timeout=args.timeout
    )
    if code != 200 or not isinstance(payload, dict):
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2

    keys = [
        "id",
        "name",
        "user_id",
        "machine",
        "status",
        "runtime_status",
        "command",
        "cwd",
        "pid",
        "python_version",
        "started_at",
        "ended_at",
        "exit_code",
        "last_heartbeat",
        "heartbeat_interval",
    ]
    result = {k: payload.get(k) for k in keys}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    path = f"/api/tasks/{urllib.parse.quote(args.task_id, safe='')}/log"
    params: dict[str, Any] = {}
    if args.latest and args.latest > 0:
        params["latest"] = args.latest
    else:
        params["after_id"] = args.after_id
        params["limit"] = args.limit
    code, payload = _request_json(
        "GET",
        args.server,
        path,
        args.user_id,
        args.user_token,
        params=params,
        timeout=args.timeout,
    )
    if code != 200 or not isinstance(payload, dict):
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2

    log_text = payload.get("log") or ""
    excerpt = log_text[-args.tail_chars :]
    result = {
        "ok": True,
        "task_id": payload.get("task_id"),
        "status": payload.get("status"),
        "runtime_status": payload.get("runtime_status"),
        "connectivity_status": payload.get("connectivity_status"),
        "last_heartbeat": payload.get("last_heartbeat"),
        "last_id": payload.get("last_id"),
        "first_id": payload.get("first_id"),
        "returned_entries": payload.get("returned_entries"),
        "query_mode": (payload.get("query") or {}).get("mode"),
        "log_size": len(log_text),
        "log_excerpt": excerpt,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    code, payload = _request_json(
        "GET", args.server, "/api/dashboard/summary", args.user_id, args.user_token, timeout=args.timeout
    )
    if code != 200 or not isinstance(payload, dict):
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2

    current = payload.get("current") or {}
    cumulative = payload.get("cumulative") or {}
    insights = payload.get("insights") or {}
    recent = payload.get("recent_tasks") or []

    result = {
        "ok": True,
        "current": current,
        "cumulative": cumulative,
        "insights": {
            "success_rate": insights.get("success_rate"),
            "active_days": insights.get("active_days"),
            "current_streak": insights.get("current_streak"),
            "longest_streak": insights.get("longest_streak"),
            "best_day_date": insights.get("best_day_date"),
            "best_day_count": insights.get("best_day_count"),
            "last_active_date": insights.get("last_active_date"),
            "deleted_total": insights.get("deleted_total"),
        },
        "recent_tasks": [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "machine": t.get("machine"),
                "started_at": t.get("started_at"),
                "display_status": t.get("display_status"),
                "is_deleted": t.get("is_deleted"),
            }
            for t in recent
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    path = f"/api/tasks/{urllib.parse.quote(args.task_id, safe='')}/history"
    params: dict[str, Any] = {}
    if args.limit:
        params["limit"] = args.limit
    code, payload = _request_json(
        "GET", args.server, path, args.user_id, args.user_token, params=params or None, timeout=args.timeout
    )
    if code != 200 or not isinstance(payload, dict):
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2

    result = {
        "ok": True,
        "task_id": payload.get("task_id"),
        "history": payload.get("history") or [],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    path = f"/api/tasks/{urllib.parse.quote(args.task_id, safe='')}"
    code, payload = _request_json(
        "DELETE", args.server, path, args.user_id, args.user_token, timeout=args.timeout
    )
    if code != 200:
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "task_id": args.task_id, "message": "task deleted"}, ensure_ascii=False, indent=2))
    return 0


def _status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("runtime_status") or task.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _normalize_text(value: Any) -> str:
    text = str(value or "").lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize_query(query: str) -> list[str]:
    normalized = _normalize_text(query)
    if not normalized:
        return []
    tokens = [t for t in normalized.split(" ") if t]
    deduped: list[str] = []
    seen = set()
    for token in tokens:
        if token not in seen:
            seen.add(token)
            deduped.append(token)
    return deduped


def _query_variants(token: str) -> list[str]:
    variants = [token]
    if token in SEARCH_ALIASES:
        variants.extend(SEARCH_ALIASES[token])
    compact = token.replace(" ", "")
    if compact and compact != token:
        variants.append(compact)
    # 去重并规范
    result: list[str] = []
    seen = set()
    for var in variants:
        n = _normalize_text(var)
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    return result


def _field_match_score(text: str, variants: list[str]) -> int:
    """Return score for one field and one query token variants."""
    best = 0
    compact_text = text.replace(" ", "")
    for var in variants:
        if not var:
            continue
        compact_var = var.replace(" ", "")
        if text == var or compact_text == compact_var:
            best = max(best, 40)
        elif text.startswith(var) or compact_text.startswith(compact_var):
            best = max(best, 20)
        elif var in text or compact_var in compact_text:
            best = max(best, 10)
    return best


def _task_search_score(task: dict[str, Any], tokens: list[str]) -> tuple[int, list[str]]:
    if not tokens:
        return 0, []

    fields = {
        "name": _normalize_text(task.get("name")),
        "command": _normalize_text(task.get("command")),
        "cwd": _normalize_text(task.get("cwd")),
        "machine": _normalize_text(task.get("machine")),
        "id": _normalize_text(task.get("id")),
        "runtime_status": _normalize_text(task.get("runtime_status")),
        "status": _normalize_text(task.get("status")),
        "python_version": _normalize_text(task.get("python_version")),
    }
    matched_tokens: list[str] = []
    score = 0

    for token in tokens:
        variants = _query_variants(token)
        token_best = 0
        token_best_field = ""
        for field_name, text in fields.items():
            if not text:
                continue
            field_score = _field_match_score(text, variants)
            if field_score > 0:
                weighted = field_score * SEARCH_FIELD_WEIGHTS.get(field_name, 1)
                if weighted > token_best:
                    token_best = weighted
                    token_best_field = field_name
        if token_best > 0:
            score += token_best
            matched_tokens.append(f"{token}@{token_best_field}")

    # 所有关键词都命中时额外加分
    if len(matched_tokens) == len(tokens):
        score += 30

    runtime_status = str(task.get("runtime_status") or task.get("status") or "")
    if runtime_status == "running":
        score += 8
    elif runtime_status == "lost":
        score += 5

    return score, matched_tokens


def _log_match_bonus(log_text: str, tokens: list[str]) -> int:
    text = _normalize_text(log_text)
    if not text:
        return 0
    bonus = 0
    for token in tokens:
        variants = _query_variants(token)
        raw = _field_match_score(text, variants)
        if raw > 0:
            bonus += raw * 2
    return bonus


def cmd_search(args: argparse.Namespace) -> int:
    tokens = _tokenize_query(args.query)
    if not tokens:
        print(json.dumps({"ok": False, "error": "empty query"}, ensure_ascii=False))
        return 2

    code, payload = _request_json(
        "GET", args.server, "/api/tasks", args.user_id, args.user_token, timeout=args.timeout
    )
    if code != 200 or not isinstance(payload, list):
        print(json.dumps({"ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        return 2

    ranked: list[dict[str, Any]] = []
    for task in payload:
        score, matched_tokens = _task_search_score(task, tokens)
        if score <= 0:
            continue
        ranked.append(
            {
                "task": task,
                "score": score,
                "matched_tokens": matched_tokens,
                "log_bonus": 0,
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)

    # 可选：对前 N 个候选补充日志匹配加分
    if args.with_log and ranked:
        probe_top = ranked[: max(1, args.log_probe_top)]
        for item in probe_top:
            task_id = item["task"].get("id")
            if not task_id:
                continue
            path = f"/api/tasks/{urllib.parse.quote(str(task_id), safe='')}/log"
            l_code, l_payload = _request_json(
                "GET",
                args.server,
                path,
                args.user_id,
                args.user_token,
                params={"after_id": 0, "limit": args.log_limit},
                timeout=args.timeout,
            )
            if l_code == 200 and isinstance(l_payload, dict):
                bonus = _log_match_bonus(str(l_payload.get("log") or ""), tokens)
                item["log_bonus"] = bonus
                item["score"] += bonus
        ranked.sort(key=lambda item: item["score"], reverse=True)

    limited = ranked[: max(1, args.limit)]
    result = {
        "ok": True,
        "query": args.query,
        "tokens": tokens,
        "total_tasks": len(payload),
        "matched_tasks": len(ranked),
        "results": [
            {
                "id": item["task"].get("id"),
                "name": item["task"].get("name"),
                "runtime_status": item["task"].get("runtime_status") or item["task"].get("status"),
                "machine": item["task"].get("machine"),
                "started_at": item["task"].get("started_at"),
                "ended_at": item["task"].get("ended_at"),
                "score": item["score"],
                "log_bonus": item["log_bonus"],
                "matched_tokens": item["matched_tokens"],
                "command": item["task"].get("command"),
            }
            for item in limited
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    prev: dict[str, int] | None = None
    iteration = 0
    while True:
        iteration += 1
        code, payload = _request_json(
            "GET", args.server, "/api/tasks", args.user_id, args.user_token, timeout=args.timeout
        )
        now = int(time.time())
        if code != 200 or not isinstance(payload, list):
            print(json.dumps({"ts": now, "ok": False, "status": code, "payload": payload}, ensure_ascii=False))
        else:
            counts = _status_counts(payload)
            changed = counts != prev
            output = {
                "ts": now,
                "ok": True,
                "iteration": iteration,
                "changed": changed,
                "total": len(payload),
                "counts": counts,
            }
            if args.task_id:
                task = next((t for t in payload if t.get("id") == args.task_id), None)
                output["task"] = {
                    "id": args.task_id,
                    "exists": bool(task),
                    "runtime_status": (task or {}).get("runtime_status") if task else None,
                    "status": (task or {}).get("status") if task else None,
                    "last_heartbeat": (task or {}).get("last_heartbeat") if task else None,
                }
            print(json.dumps(output, ensure_ascii=False))
            prev = counts

        if args.iterations > 0 and iteration >= args.iterations:
            return 0
        time.sleep(max(1, args.interval))


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default=DEFAULT_SERVER, help="LogWatch server base URL")
    parser.add_argument("--user-id", required=True, help="user_id for auth")
    parser.add_argument("--user-token", required=True, help="user_token for auth")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout seconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LogWatch monitor helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_summary = sub.add_parser("summary", help="summarize task runtime status")
    _common(p_summary)
    p_summary.add_argument("--limit", type=int, default=10, help="max tasks to include")
    p_summary.set_defaults(func=cmd_summary)

    p_task = sub.add_parser("task", help="show one task details")
    _common(p_task)
    p_task.add_argument("--task-id", required=True, help="task id")
    p_task.set_defaults(func=cmd_task)

    p_log = sub.add_parser("log", help="fetch incremental or latest task log")
    _common(p_log)
    p_log.add_argument("--task-id", required=True, help="task id")
    p_log.add_argument("--after-id", type=int, default=0, help="log cursor id (incremental mode)")
    p_log.add_argument("--limit", type=int, default=500, help="max log rows (incremental mode)")
    p_log.add_argument("--latest", type=int, default=0, help="fetch latest N entries instead of incremental")
    p_log.add_argument("--tail-chars", type=int, default=2000, help="tail chars in output")
    p_log.set_defaults(func=cmd_log)

    p_dashboard = sub.add_parser("dashboard", help="show dashboard summary with insights and activity")
    _common(p_dashboard)
    p_dashboard.set_defaults(func=cmd_dashboard)

    p_history = sub.add_parser("history", help="show task status transition history")
    _common(p_history)
    p_history.add_argument("--task-id", required=True, help="task id")
    p_history.add_argument("--limit", type=int, default=0, help="max history entries (0=all)")
    p_history.set_defaults(func=cmd_history)

    p_delete = sub.add_parser("delete", help="delete a task")
    _common(p_delete)
    p_delete.add_argument("--task-id", required=True, help="task id to delete")
    p_delete.set_defaults(func=cmd_delete)

    p_search = sub.add_parser("search", help="intelligent search tasks by query")
    _common(p_search)
    p_search.add_argument("--query", required=True, help="search text, e.g. nnunet liver")
    p_search.add_argument("--limit", type=int, default=10, help="max matched tasks to return")
    p_search.add_argument("--with-log", action="store_true", help="use log text to improve ranking")
    p_search.add_argument("--log-probe-top", type=int, default=5, help="probe top-N candidates for log matching")
    p_search.add_argument("--log-limit", type=int, default=200, help="log row limit when probing")
    p_search.set_defaults(func=cmd_search)

    p_watch = sub.add_parser("watch", help="poll task summary")
    _common(p_watch)
    p_watch.add_argument("--interval", type=int, default=10, help="polling interval seconds")
    p_watch.add_argument("--iterations", type=int, default=0, help="0 means infinite")
    p_watch.add_argument("--task-id", default="", help="optional task focus")
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not check_health(args.server, args.timeout):
        print(json.dumps({"ok": False, "error": "server health check failed"}, ensure_ascii=False))
        return 2

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
