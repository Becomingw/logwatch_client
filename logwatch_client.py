#!/usr/bin/env python3
"""
lw - LogWatch 客户端
包裹任意命令，捕获输出并上传到日志监控服务器。

使用方式:
    lw python train.py
    lw --name "resnet-v2" python train.py
    lw --server http://your-server.com python train.py
    lw --init  # 生成配置文件模板

配置文件 (~/.lwconfig):
    server=http://your-server.com:8000
    machine=my-gpu-server  # 可选，默认用 hostname
    user_id=alice  # 可选，用于鉴权/多用户隔离
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import pty
import select
import sqlite3
import shutil
import signal
import smtplib
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional
import requests


# ── 配置 ──────────────────────────────────────────────

DEFAULT_SERVER = "http://127.0.0.1:8000"
CONFIG_PATH = Path.home() / ".lwconfig"
LOG_DIR = Path.home() / ".lw_logs"
UPLOAD_INTERVAL = 2  # 秒（实时上传）
LOG_RETENTION_DAYS = 7  # 本地日志保留天数
LOG_MAX_FILES = 1000  # 本地日志最大文件数
BATCH_SIZE = 100
BATCH_INTERVAL_MS = 5000
COMPRESSION_LEVEL = 6
UPLOAD_CIRCUIT_BREAK_MAX = 3  # 连续失败达到该值后进入离线模式
UPLOAD_TIMEOUT_SECONDS = 5
RETRY_BACKOFF_BASE_SECONDS = 1
RETRY_BACKOFF_MAX_SECONDS = 60
PUBLISH_GRACE_SECONDS = 1  # 发布前等待窗口（秒）
QUEUE_DB_PATH = LOG_DIR / "queue.db"

POST_OK = "ok"
POST_RETRYABLE_FAIL = "retryable_fail"
POST_TASK_DELETED = "task_deleted"


# ── 邮件配置 ──────────────────────────────────────────

def load_email_config(config: dict) -> Optional[dict]:
    """从配置中加载邮件设置，返回 None 表示未配置或禁用"""
    smtp_host = config.get("smtp_host", "").strip()
    if not smtp_host:
        return None

    notify_on = config.get("email_notify_on", "all").lower().strip()
    if notify_on not in ("all", "failed", "success"):
        notify_on = "all"

    return {
        "enabled": config.get("email_enabled", "true").lower() == "true",
        "smtp_host": smtp_host,
        "smtp_port": int(config.get("smtp_port", "465") or "465"),
        "smtp_user": config.get("smtp_user", "").strip(),
        "smtp_pass": config.get("smtp_pass", "").strip(),
        "smtp_use_tls": config.get("smtp_use_tls", "true").lower() == "true",
        "from": config.get("email_from", "").strip(),
        "to": config.get("email_to", "").strip(),
        "notify_on": notify_on,  # all, failed, success
        "notify_on_start": config.get("email_notify_on_start", "false").lower() == "true",
    }


def send_email(subject: str, body: str, email_config: dict, html_body: Optional[str] = None) -> tuple[bool, str]:
    """
    发送邮件，支持 HTML 格式
    返回: (成功与否, 错误信息或空字符串)
    """
    if not email_config or not email_config.get("enabled", False):
        return False, "邮件未启用"

    recipient = email_config.get("to", "")
    sender = email_config.get("from", "")
    if not recipient or not sender:
        return False, "收件人或发件人未配置"

    try:
        if html_body:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = recipient
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = recipient

        port = email_config.get("smtp_port", 465)
        use_tls = email_config.get("smtp_use_tls", True)
        smtp_user = email_config.get("smtp_user", "")
        smtp_pass = email_config.get("smtp_pass", "")

        if port == 465:
            with smtplib.SMTP_SSL(email_config["smtp_host"], port, timeout=10) as server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(email_config["smtp_host"], port, timeout=10) as server:
                if use_tls:
                    server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)

        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP 认证失败，请检查用户名和密码"
    except smtplib.SMTPConnectError:
        return False, "无法连接 SMTP 服务器"
    except smtplib.SMTPException as e:
        return False, f"SMTP 错误: {e}"
    except socket.timeout:
        return False, "SMTP 连接超时"
    except Exception as e:
        return False, f"发送失败: {e}"


# ── 邮件模板 ──────────────────────────────────────────

def _format_duration(seconds: int) -> str:
    """格式化时长"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}h {mins}m"


def build_task_email(
    task_name: str,
    machine: str,
    command: str,
    status: str = "success",  # start, success, failed
    exit_code: Optional[int] = None,
    elapsed_seconds: Optional[int] = None,
    tail_logs: Optional[str] = None,
) -> tuple[str, str, str]:
    """
    构建任务通知邮件
    status: start=开始执行, success=执行成功, failed=执行失败
    返回: (subject, plain_body, html_body)
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 状态配置
    status_map = {
        "start": ("开始执行", "🚀", "#007aff"),
        "success": ("执行成功", "✅", "#34c759"),
        "failed": ("执行失败", "❌", "#ff3b30"),
    }
    status_text, status_emoji, status_color = status_map.get(status, status_map["success"])

    subject = f"[LogWatch] {task_name} - {status_text}"

    # 纯文本版本
    plain_body = f"""LogWatch 任务通知
{'=' * 40}

状态: {status_emoji} {status_text}
任务: {task_name}
机器: {machine}
命令: {command}"""

    if exit_code is not None:
        plain_body += f"\n退出码: {exit_code}"
    if elapsed_seconds is not None:
        plain_body += f"\n耗时: {_format_duration(elapsed_seconds)}"
    plain_body += f"\n时间: {now}"

    if tail_logs:
        log_lines = tail_logs.strip().split('\n')[-15:]
        plain_body += "\n\n--- 日志尾部 ---\n" + '\n'.join(log_lines)

    plain_body += f"\n{'=' * 40}\n此邮件由 LogWatch 客户端离线模式发送"

    # HTML 版本 - 额外信息行
    extra_html = ""
    if exit_code is not None or elapsed_seconds is not None:
        exit_html = f'<div style="flex: 1; padding: 10px 16px; border-right: 1px solid #e5e5e5;"><div style="font-size: 11px; color: #86868b;">退出码</div><div style="font-size: 14px; font-weight: 600; color: #1d1d1f;">{exit_code if exit_code is not None else "-"}</div></div>' if exit_code is not None else ""
        duration_html = f'<div style="flex: 1; padding: 10px 16px;"><div style="font-size: 11px; color: #86868b;">耗时</div><div style="font-size: 14px; color: #1d1d1f;">{_format_duration(elapsed_seconds) if elapsed_seconds else "-"}</div></div>' if elapsed_seconds is not None else ""
        if exit_html or duration_html:
            extra_html = f'<div style="display: flex; border-bottom: 1px solid #e5e5e5;">{exit_html}{duration_html}</div>'

    logs_html = ""
    if tail_logs:
        log_lines = tail_logs.strip().split('\n')[-15:]
        escaped_logs = '\n'.join(log_lines).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        logs_html = f'<div style="margin-top: 16px;"><div style="font-size: 12px; color: #86868b; margin-bottom: 8px;">日志尾部</div><pre style="background: #2d2d2d; color: #d4d4d4; padding: 12px; border-radius: 8px; font-size: 11px; overflow-x: auto; white-space: pre-wrap; word-break: break-all;">{escaped_logs}</pre></div>'

    html_body = f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7;">
<div style="max-width: 500px; margin: 0 auto; background: #fff; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
    <div style="padding: 20px; text-align: center;"><div style="font-size: 18px; font-weight: 600; color: #1d1d1f;">LogWatch</div></div>
    <div style="padding: 0 20px 20px;">
        <div style="text-align: center; margin-bottom: 16px;"><span style="display: inline-block; background: {status_color}; color: #fff; padding: 6px 16px; border-radius: 16px; font-size: 13px; font-weight: 600;">{status_text}</span></div>
        <div style="border: 1px solid #e5e5e5; border-radius: 8px; overflow: hidden;">
            <div style="padding: 12px 16px; border-bottom: 1px solid #e5e5e5;"><div style="font-size: 15px; font-weight: 600; color: #1d1d1f;">{task_name}</div><div style="font-size: 12px; color: #86868b; margin-top: 2px;">{machine}</div></div>
            {extra_html}
            <div style="padding: 10px 16px; background: #fafafa;"><div style="font-size: 11px; color: #86868b;">命令</div><div style="font-size: 12px; color: #1d1d1f; font-family: monospace; word-break: break-all;">{command[:100]}{"..." if len(command) > 100 else ""}</div></div>
        </div>
        {logs_html}
    </div>
    <div style="padding: 12px 20px; background: #f5f5f7; text-align: center;"><div style="font-size: 11px; color: #86868b;">LogWatch 客户端离线模式 · {now}</div></div>
</div>
</body>
</html>'''

    return subject, plain_body, html_body


def send_task_notification_email(
    email_config: Optional[dict],
    task_name: str,
    machine: str,
    command: str,
    exit_code: int,
    elapsed_seconds: int,
    log_file: Path,
) -> None:
    """发送任务完成的邮件通知（离线模式使用）"""
    if not email_config or not email_config.get("enabled", False):
        return

    # 根据 notify_on 配置过滤
    notify_on = email_config.get("notify_on", "all")
    if notify_on == "failed" and exit_code == 0:
        return
    if notify_on == "success" and exit_code != 0:
        return

    # 读取日志尾部
    tail_logs = None
    try:
        if log_file.exists():
            content = log_file.read_text(errors="replace")
            if content:
                tail_logs = content
    except Exception:
        pass

    status = "success" if exit_code == 0 else "failed"
    subject, plain_body, html_body = build_task_email(
        task_name=task_name,
        machine=machine,
        command=command,
        status=status,
        exit_code=exit_code,
        elapsed_seconds=elapsed_seconds,
        tail_logs=tail_logs,
    )

    success, error = send_email(subject, plain_body, email_config, html_body=html_body)
    if success:
        print_lw_message("邮件通知已发送", color="32")
    else:
        print_lw_message(f"邮件发送失败: {error}", color="33")


def send_task_start_email(
    email_config: Optional[dict],
    task_name: str,
    machine: str,
    command: str,
) -> None:
    """发送任务开始的邮件通知（离线模式使用）"""
    if not email_config or not email_config.get("enabled", False):
        return
    if not email_config.get("notify_on_start", False):
        return

    subject, plain_body, html_body = build_task_email(
        task_name=task_name,
        machine=machine,
        command=command,
        status="start",
    )

    success, error = send_email(subject, plain_body, email_config, html_body=html_body)
    if success:
        print_lw_message("开始邮件已发送", color="32")
    else:
        print_lw_message(f"开始邮件发送失败: {error}", color="33")


def load_config() -> dict:
    """从 ~/.lwconfig 读取配置"""
    config = {}
    if CONFIG_PATH.exists():
        for line in CONFIG_PATH.read_text().strip().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                config[k.strip()] = v.strip()
    return config


def init_config():
    """生成配置文件模板"""
    if CONFIG_PATH.exists():
        print(f"配置文件已存在: {CONFIG_PATH}")
        print("当前内容:")
        print(CONFIG_PATH.read_text())
        return

    template = """# LogWatch 客户端配置
# 服务器地址（必填）
server=http://your-server.com:8000

# 机器标识（可选，默认使用 hostname）
# machine=my-gpu-server

# 用户 ID（可选，用于鉴权/多用户隔离）
# user_id=alice

# 日志上传间隔（秒，可选，默认 2 秒）
# 控制读取本地日志文件的频率
# upload_interval_seconds=2

# 批量上传每批条数（可选，默认 100）
# batch_size=100

# 批量上传最大等待时间（毫秒，可选，默认 5000）
# batch_interval_ms=5000

# gzip 压缩等级（1-9，可选，默认 6）
# compression_level=6

# 发布前等待窗口（秒，可选，默认 1 秒）
# 等待程序稳定后再开始上传，避免瞬间退出的程序产生无效日志
# publish_grace_seconds=1

# 本地日志保留天数（可选）
# log_retention_days=7

# 本地日志最大文件数（可选，超过则删除最旧的）
# log_max_files=1000

# 连续失败达到该值后进入离线模式（可选）
# upload_circuit_break_max=3

# ── 离线邮件通知配置（可选）──────────────────────────
# 离线模式下，任务完成后会通过邮件通知
# 如果不需要邮件通知，保持以下配置注释即可

# 强制始终使用离线模式（不上传到服务器，仅本地记录+邮件通知）
# force_offline=false

# 是否启用邮件通知（true/false）
# email_enabled=true

# 邮件通知类型：all=全部, failed=仅失败, success=仅成功
# email_notify_on=all

# 任务开始时是否发送邮件通知（true/false）
# 注意：遵循 publish_grace_seconds 等待窗口，瞬间退出的程序不会发送
# email_notify_on_start=false

# SMTP 服务器地址（必填，启用邮件通知时）
# smtp_host=smtp.example.com

# SMTP 端口（可选，默认 465）
# 465: SSL 加密, 587: STARTTLS, 25: 明文
# smtp_port=465

# SMTP 用户名（通常是邮箱地址）
# smtp_user=your-email@example.com

# SMTP 密码或授权码
# smtp_pass=your-password-or-auth-code

# 是否使用 TLS（可选，默认 true）
# smtp_use_tls=true

# 发件人地址
# email_from=your-email@example.com

# 收件人地址（接收通知的邮箱）
# email_to=notify@example.com
"""
    CONFIG_PATH.write_text(template)
    print(f"配置文件已生成: {CONFIG_PATH}")
    print("请编辑该文件，设置服务器地址。")


# ── HTTP 工具 ──────────────────────────────────────────

def _normalized_compression_level(level: int) -> int:
    if level < 1:
        return 1
    if level > 9:
        return 9
    return level


def post_json_status_with_response(
    url: str,
    data: dict,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
    gzip_min_bytes: int = 0,
    compression_level: int = COMPRESSION_LEVEL,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> tuple[str, Optional[dict], int]:
    """POST JSON 到服务端，返回请求状态、JSON 响应和 HTTP 状态码。"""
    own_session = session is None
    http = session or requests.Session()
    try:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if gzip_min_bytes > 0 and len(body) >= gzip_min_bytes:
            body = gzip.compress(body, compresslevel=_normalized_compression_level(compression_level))
            headers["Content-Encoding"] = "gzip"

        if request_lock:
            with request_lock:
                resp = http.post(url, data=body, headers=headers, timeout=timeout)
        else:
            resp = http.post(url, data=body, headers=headers, timeout=timeout)
        if resp.status_code == 409:
            return POST_TASK_DELETED, None, resp.status_code
        if 200 <= resp.status_code < 300:
            try:
                return POST_OK, resp.json(), resp.status_code
            except ValueError:
                return POST_OK, None, resp.status_code
        return POST_RETRYABLE_FAIL, None, resp.status_code
    except requests.RequestException:
        return POST_RETRYABLE_FAIL, None, 0
    finally:
        if own_session:
            http.close()


def post_json_status(
    url: str,
    data: dict,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
    gzip_min_bytes: int = 0,
    compression_level: int = COMPRESSION_LEVEL,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> str:
    status, _payload, _code = post_json_status_with_response(
        url=url,
        data=data,
        timeout=timeout,
        gzip_min_bytes=gzip_min_bytes,
        compression_level=compression_level,
        session=session,
        request_lock=request_lock,
    )
    return status


def post_json(
    url: str,
    data: dict,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
    gzip_min_bytes: int = 0,
    compression_level: int = COMPRESSION_LEVEL,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> bool:
    """POST JSON 到服务端，失败时静默返回 False"""
    return post_json_status(
        url=url,
        data=data,
        timeout=timeout,
        gzip_min_bytes=gzip_min_bytes,
        compression_level=compression_level,
        session=session,
        request_lock=request_lock,
    ) == POST_OK


def get_json_status(
    url: str,
    params: Optional[dict] = None,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> tuple[str, Optional[dict], int]:
    own_session = session is None
    http = session or requests.Session()
    try:
        if request_lock:
            with request_lock:
                resp = http.get(url, params=params, timeout=timeout)
        else:
            resp = http.get(url, params=params, timeout=timeout)
        if resp.status_code == 409:
            return POST_TASK_DELETED, None, resp.status_code
        if 200 <= resp.status_code < 300:
            try:
                return POST_OK, resp.json(), resp.status_code
            except ValueError:
                return POST_OK, None, resp.status_code
        return POST_RETRYABLE_FAIL, None, resp.status_code
    except requests.RequestException:
        return POST_RETRYABLE_FAIL, None, 0
    finally:
        if own_session:
            http.close()


def check_server_connectivity(server: str) -> bool:
    """检查服务端是否可达（使用心跳接口，无需鉴权）"""
    status = post_json_status(
        f"{server.rstrip('/')}/api/heartbeat",
        {"task_id": "health-check", "timestamp": datetime.now(timezone.utc).isoformat()},
        timeout=3,
    )
    return status in (POST_OK, POST_TASK_DELETED)


# ── 本地持久化队列 ────────────────────────────────────

class LogQueueStore:
    """SQLite WAL 本地队列。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    def _init_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS log_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                user_id TEXT NOT NULL DEFAULT '',
                client_seq INTEGER NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, client_seq)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_log_queue_task_status_seq ON log_queue(task_id, status, client_seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_log_queue_task_seq ON log_queue(task_id, client_seq)"
        )
        conn.commit()
        conn.close()

    def get_next_seq(self, task_id: str, min_value: int = 1) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT COALESCE(MAX(client_seq), 0) AS max_seq FROM log_queue WHERE task_id=?",
            (task_id,),
        ).fetchone()
        conn.close()
        next_seq = int(row["max_seq"]) + 1 if row else 1
        return max(next_seq, min_value)

    def enqueue(
        self,
        task_id: str,
        user_id: str,
        client_seq: int,
        content: str,
        timestamp: str,
        status: str = "pending",
    ):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO log_queue
            (task_id, user_id, client_seq, content, timestamp, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, user_id, client_seq, content, timestamp, status, now, now),
        )
        conn.commit()
        conn.close()

    def reconcile_with_server_ack(self, task_id: str, last_ack_seq: int):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.execute(
            "UPDATE log_queue SET status='sent', updated_at=? WHERE task_id=? AND client_seq<=?",
            (now, task_id, last_ack_seq),
        )
        conn.execute(
            "UPDATE log_queue SET status='pending', updated_at=? WHERE task_id=? AND client_seq>? AND status='sent'",
            (now, task_id, last_ack_seq),
        )
        conn.commit()
        conn.close()

    def get_pending_count(self, task_id: str) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM log_queue WHERE task_id=? AND status='pending'",
            (task_id,),
        ).fetchone()
        conn.close()
        return int(row["cnt"]) if row else 0

    def get_pending_batch(self, task_id: str, limit: int) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT client_seq, content, timestamp
            FROM log_queue
            WHERE task_id=? AND status='pending'
            ORDER BY client_seq
            LIMIT ?
            """,
            (task_id, max(1, limit)),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_unsent_count(self, task_id: str) -> int:
        conn = self._connect()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM log_queue WHERE task_id=? AND status IN ('pending', 'failed')",
            (task_id,),
        ).fetchone()
        conn.close()
        return int(row["cnt"]) if row else 0

    def reset_failed_to_pending(self, task_id: str):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.execute(
            "UPDATE log_queue SET status='pending', updated_at=? WHERE task_id=? AND status='failed'",
            (now, task_id),
        )
        conn.commit()
        conn.close()

    def mark_sent_up_to(self, task_id: str, ack_seq: int):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.execute(
            """
            UPDATE log_queue
            SET status='sent', last_error=NULL, updated_at=?
            WHERE task_id=? AND client_seq<=? AND status IN ('pending', 'failed', 'sent')
            """,
            (now, task_id, ack_seq),
        )
        conn.commit()
        conn.close()

    def mark_failed(self, task_id: str, client_seqs: list[int], error: str):
        if not client_seqs:
            return
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.executemany(
            """
            UPDATE log_queue
            SET status='failed', retry_count=retry_count+1, last_error=?, updated_at=?
            WHERE task_id=? AND client_seq=? AND status='pending'
            """,
            [(error, now, task_id, seq) for seq in client_seqs],
        )
        conn.commit()
        conn.close()

    def archive_task(self, task_id: str, reason: str):
        now = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        conn.execute(
            """
            UPDATE log_queue
            SET status='archived', last_error=?, updated_at=?
            WHERE task_id=? AND status IN ('pending', 'failed', 'sent')
            """,
            (reason, now, task_id),
        )
        conn.commit()
        conn.close()


# ── 日志上传线程 ──────────────────────────────────────

class LogUploader:
    """后台线程：本地 WAL 队列 + 批量压缩上传 + 批量 ACK。"""

    def __init__(self, server: str, task_id: str, log_file: Path, user_id: str, config: dict):
        self.server = server.rstrip("/")
        self.task_id = task_id
        self.log_file = log_file
        self.user_id = user_id
        self._offset = 0
        self._stop = threading.Event()
        self._thread = None
        self._heartbeat_thread = None
        self._request_lock = threading.Lock()
        self._session: Optional[requests.Session] = None
        self._queue = LogQueueStore(QUEUE_DB_PATH)
        self._offline = threading.Event()
        self._task_deleted = threading.Event()
        self._upload_interval = max(1, _get_int_config(config, "upload_interval_seconds", UPLOAD_INTERVAL))
        self._heartbeat_interval = 30
        self._batch_size = max(1, _get_int_config(config, "batch_size", BATCH_SIZE))
        self._batch_interval_ms = max(100, _get_int_config(config, "batch_interval_ms", BATCH_INTERVAL_MS))
        self._compression_level = _normalized_compression_level(
            _get_int_config(config, "compression_level", COMPRESSION_LEVEL)
        )
        self._circuit_max = max(1, _get_int_config(config, "upload_circuit_break_max", UPLOAD_CIRCUIT_BREAK_MAX))
        self._circuit_count = 0
        self._last_heartbeat = 0.0
        self._pending_since = 0.0
        self._next_retry_at = 0.0
        self._retry_backoff_seconds = RETRY_BACKOFF_BASE_SECONDS
        self._last_ack_seq = 0
        self._next_seq = 1

    def get_http_session(self) -> Optional[requests.Session]:
        return self._session

    def get_request_lock(self) -> threading.Lock:
        return self._request_lock

    def start(self):
        """启动上传线程和心跳线程。"""
        if self._session is None:
            self._session = requests.Session()
        self._resume_from_server_ack()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._heartbeat_thread = threading.Thread(target=self._run_heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def stop(self):
        """停止上传线程和心跳线程，并尽量完成最后一次批量上传。"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        self._collect_new_logs()
        for _ in range(20):
            if self._offline.is_set() or self._task_deleted.is_set():
                break
            self._queue.reset_failed_to_pending(self.task_id)
            if self._queue.get_unsent_count(self.task_id) <= 0:
                break
            self._flush_batch(force=True)
        # 退出前把 failed 统一重置回 pending，便于下次继续上传
        self._queue.reset_failed_to_pending(self.task_id)
        if self._session:
            self._session.close()
            self._session = None

    def _run(self):
        loop_interval = max(0.2, min(float(self._upload_interval), 1.0))
        while not self._stop.is_set():
            self._collect_new_logs()
            if not self._offline.is_set() and not self._task_deleted.is_set():
                if time.time() >= self._next_retry_at:
                    self._queue.reset_failed_to_pending(self.task_id)
                    self._flush_batch(force=False)
            self._stop.wait(loop_interval)

    def _run_heartbeat(self):
        while not self._stop.wait(self._heartbeat_interval):
            self._send_heartbeat()

    def _resume_from_server_ack(self):
        """启动时查询服务端 ACK，确保断点续传从 last_ack_seq + 1 开始。"""
        if self._offline.is_set() or self._task_deleted.is_set():
            return
        if not self._session:
            return

        status, payload, code = get_json_status(
            f"{self.server}/api/log/last-ack",
            params={"task_id": self.task_id, "user_id": self.user_id},
            timeout=UPLOAD_TIMEOUT_SECONDS,
            session=self._session,
            request_lock=self._request_lock,
        )
        if status == POST_TASK_DELETED:
            self._abandon_task_push("续传 ACK 查询")
            return
        if status == POST_OK:
            try:
                self._last_ack_seq = int((payload or {}).get("last_ack_seq", 0) or 0)
            except (TypeError, ValueError):
                self._last_ack_seq = 0
        elif code != 404:
            # 网络失败或其他异常状态：保持本地序列继续，不阻塞任务执行
            pass

        self._queue.reconcile_with_server_ack(self.task_id, self._last_ack_seq)
        self._next_seq = self._queue.get_next_seq(self.task_id, min_value=self._last_ack_seq + 1)

    def _send_heartbeat(self):
        if self._offline.is_set():
            return
        status = post_json_status(
            f"{self.server}/api/heartbeat",
            {
                "task_id": self.task_id,
                "user_id": self.user_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            timeout=UPLOAD_TIMEOUT_SECONDS,
            session=self._session,
            request_lock=self._request_lock,
        )
        if status == POST_OK:
            self._last_heartbeat = time.time()
        elif status == POST_TASK_DELETED:
            self._abandon_task_push("心跳")

    def _enter_offline(self):
        if self._task_deleted.is_set():
            return
        if not self._offline.is_set():
            self._offline.set()
            print_lw_message("上传连续失败，进入离线模式（日志保留在本地 WAL 队列）", color="33")

    def _abandon_task_push(self, source: str):
        if self._task_deleted.is_set():
            return
        self._task_deleted.set()
        self._offline.set()
        self._queue.archive_task(self.task_id, reason=f"task deleted: {source}")
        print_lw_message(
            f"任务已被服务端删除（{source}收到 HTTP 409），后续日志转归档状态",
            color="33",
        )

    def mark_task_deleted(self, source: str):
        self._abandon_task_push(source)

    def is_task_deleted(self) -> bool:
        return self._task_deleted.is_set()

    def is_offline(self) -> bool:
        return self._offline.is_set()

    def _collect_new_logs(self):
        try:
            with open(self.log_file, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
            if not chunk:
                return
        except FileNotFoundError:
            return
        except Exception:
            return

        try:
            content = chunk.decode("utf-8", errors="replace")
        except Exception:
            content = chunk.decode("latin-1")
        timestamp = datetime.now(timezone.utc).isoformat()
        row_status = "archived" if self._task_deleted.is_set() else "pending"
        client_seq = self._next_seq
        self._queue.enqueue(
            task_id=self.task_id,
            user_id=self.user_id,
            client_seq=client_seq,
            content=content,
            timestamp=timestamp,
            status=row_status,
        )
        self._next_seq += 1
        self._offset += len(chunk)
        if row_status == "pending" and self._pending_since <= 0:
            self._pending_since = time.time()

    def _flush_batch(self, force: bool = False):
        if self._offline.is_set() or self._task_deleted.is_set():
            return
        now = time.time()
        if not force and now < self._next_retry_at:
            return

        pending_count = self._queue.get_pending_count(self.task_id)
        if pending_count <= 0:
            self._pending_since = 0.0
            return

        if not force and pending_count < self._batch_size:
            if self._pending_since <= 0:
                self._pending_since = now
            if (now - self._pending_since) * 1000 < self._batch_interval_ms:
                return

        batch = self._queue.get_pending_batch(self.task_id, self._batch_size)
        if not batch:
            return

        status, payload, _code = post_json_status_with_response(
            f"{self.server}/api/log/batch",
            {
                "task_id": self.task_id,
                "user_id": self.user_id,
                "logs": batch,
            },
            timeout=UPLOAD_TIMEOUT_SECONDS,
            gzip_min_bytes=1,
            compression_level=self._compression_level,
            session=self._session,
            request_lock=self._request_lock,
        )
        if status == POST_OK:
            try:
                ack_seq = int((payload or {}).get("ack_seq", batch[-1]["client_seq"]) or 0)
            except (TypeError, ValueError):
                ack_seq = int(batch[-1]["client_seq"])
            self._queue.mark_sent_up_to(self.task_id, ack_seq)
            self._last_ack_seq = max(self._last_ack_seq, ack_seq)
            self._circuit_count = 0
            self._next_retry_at = 0.0
            self._retry_backoff_seconds = RETRY_BACKOFF_BASE_SECONDS
            if self._queue.get_pending_count(self.task_id) > 0:
                self._pending_since = time.time()
            else:
                self._pending_since = 0.0
            return

        if status == POST_TASK_DELETED:
            self._abandon_task_push("批量日志上报")
            return

        self._queue.mark_failed(
            self.task_id,
            [int(item["client_seq"]) for item in batch],
            "batch upload failed",
        )
        self._circuit_count += 1
        self._next_retry_at = time.time() + self._retry_backoff_seconds
        self._retry_backoff_seconds = min(self._retry_backoff_seconds * 2, RETRY_BACKOFF_MAX_SECONDS)
        if self._circuit_count >= self._circuit_max:
            self._enter_offline()


# ── 事件上报 ──────────────────────────────────────────

def send_event(server: str, task_id: str, user_id: str, event_type: str,
               name: str, machine: str, command: str,
               exit_code: Optional[int] = None, heartbeat_interval: Optional[int] = None,
               retries: int = 3, uploader: Optional[LogUploader] = None) -> bool:
    """上报任务事件（开始/结束/失败），支持重试"""
    data = {
        "task_id": task_id,
        "user_id": user_id,
        "type": event_type,
        "name": name,
        "machine": machine,
        "command": command,
        "exit_code": exit_code,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if heartbeat_interval is not None:
        data["heartbeat_interval"] = heartbeat_interval
    url = f"{server.rstrip('/')}/api/event"
    session = uploader.get_http_session() if uploader else None
    request_lock = uploader.get_request_lock() if uploader else None

    for i in range(retries):
        status = post_json_status(
            url,
            data,
            timeout=UPLOAD_TIMEOUT_SECONDS,
            session=session,
            request_lock=request_lock,
        )
        if status == POST_OK:
            return True
        if status == POST_TASK_DELETED:
            if uploader:
                uploader.mark_task_deleted("事件上报")
            else:
                print_lw_message("任务已被服务端删除（事件上报收到 HTTP 409），停止该任务后续上报", color="33")
            return False
        if i < retries - 1:
            time.sleep(1)
    return False

# ── 本地日志清理 ──────────────────────────────────────

def _get_int_config(config: dict, key: str, default: int) -> int:
    value = config.get(key, "")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def cleanup_old_logs(config: dict):
    """清理本地日志：先按天数，再按最大文件数"""
    if not LOG_DIR.exists():
        return

    retention_days = _get_int_config(config, "log_retention_days", LOG_RETENTION_DAYS)
    max_files = _get_int_config(config, "log_max_files", LOG_MAX_FILES)
    cutoff = time.time() - retention_days * 24 * 3600
    cleaned = 0

    for log_file in LOG_DIR.glob("*.log"):
        try:
            if log_file.stat().st_mtime < cutoff:
                log_file.unlink()
                cleaned += 1
        except OSError:
            pass

    if max_files > 0:
        try:
            files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime)
        except OSError:
            files = []
        while len(files) > max_files:
            log_file = files.pop(0)
            try:
                log_file.unlink()
                cleaned += 1
            except OSError:
                pass

    return cleaned



# ── 主入口 ────────────────────────────────────────────

def get_machine_name(config: dict) -> str:
    """获取机器标识：优先使用配置文件中的 machine，否则用 hostname"""
    return config.get("machine", socket.gethostname())


def get_user_id(config: dict) -> str | None:
    """获取用户 ID：优先使用配置文件，其次环境变量"""
    return config.get("user_id") or os.environ.get("LW_USER_ID")


def print_lw_message(msg: str, color: str = "90", file=sys.stderr):
    """打印 lw 自身的消息到 stderr，避免与程序输出混淆"""
    print(f"\033[{color}m[lw] {msg}\033[0m", file=file)


def prompt_offline_mode() -> bool:
    """询问是否进入离线模式（交互式）"""
    try:
        answer = input("无法连接服务器，是否离线模式继续？[y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def precheck_command(command: list[str]) -> int:
    """预检查命令是否存在且可执行，返回建议退出码（0 表示通过）"""
    cmd = command[0]
    has_sep = os.path.sep in cmd or (os.path.altsep and os.path.altsep in cmd)
    if has_sep:
        path = Path(cmd)
        if not path.exists():
            print_lw_message(f"命令不存在: {cmd}", color="31")
            return 127
        if path.is_dir() or not os.access(path, os.X_OK):
            print_lw_message(f"没有执行权限: {cmd}", color="31")
            return 126
        return 0

    resolved = shutil.which(cmd)
    if not resolved:
        print_lw_message(f"命令不存在: {cmd}", color="31")
        return 127
    if not os.access(resolved, os.X_OK):
        print_lw_message(f"没有执行权限: {resolved}", color="31")
        return 126
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="lw",
        description="LogWatch - 包裹命令并上传日志到监控服务器",
        usage="lw [OPTIONS] COMMAND [ARGS...]",
    )
    parser.add_argument("--name", "-n", help="任务名称（默认自动生成）")
    parser.add_argument("--server", "-s", help="服务器地址（默认读取 ~/.lwconfig）")
    parser.add_argument("--machine", "-m", help="机器标识（默认使用 hostname）")
    parser.add_argument("--user-id", "-u", help="用户 ID（默认读取 ~/.lwconfig）")
    parser.add_argument("--init", action="store_true", help="生成配置文件模板")
    parser.add_argument("--no-check", action="store_true", help="跳过服务器连通性检查")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令")

    args = parser.parse_args()

    # 处理 --init
    if args.init:
        init_config()
        sys.exit(0)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # 处理 -- 分隔符
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.print_help()
        sys.exit(1)

    # 配置：命令行 > 配置文件 > 默认值
    config = load_config()

    # server: 命令行 > 配置文件 > 默认值
    server = args.server or config.get("server") or DEFAULT_SERVER

    # machine: 命令行 > 配置文件 > hostname
    machine = args.machine or config.get("machine") or socket.gethostname()

    # user_id: 命令行 > 配置文件 > 环境变量
    user_id = getattr(args, 'user_id', None) or get_user_id(config)
    if not user_id:
        print_lw_message("错误: 未设置 user_id", color="31")
        print_lw_message("请使用以下方式之一设置:", color="31")
        print_lw_message("  1. 命令行参数: lw --user-id YOUR_ID ...", color="31")
        print_lw_message("  2. 配置文件 ~/.lwconfig: user_id=YOUR_ID", color="31")
        print_lw_message("  3. 环境变量: export LW_USER_ID=YOUR_ID", color="31")
        sys.exit(1)

    task_id = str(uuid.uuid4())
    task_name = args.name or f"{machine}-{datetime.now().strftime('%m%d-%H%M%S')}"
    command_str = " ".join(command)
    publish_grace_seconds = _get_int_config(config, "publish_grace_seconds", PUBLISH_GRACE_SECONDS)

    # 预检查命令
    precheck_code = precheck_command(command)
    if precheck_code != 0:
        sys.exit(precheck_code)

    # 检查是否强制离线模式
    force_offline = config.get("force_offline", "false").lower() == "true"
    offline_mode = force_offline

    # 检查服务器连通性（可选，非强制离线时）
    if not offline_mode and not args.no_check:
        if not check_server_connectivity(server):
            if sys.stdin.isatty():
                offline_mode = prompt_offline_mode()
                if not offline_mode:
                    print_lw_message("无法连接服务器，已退出", color="31")
                    sys.exit(2)
                print_lw_message("进入离线模式，仅记录本地日志", color="33")
            else:
                print_lw_message("无法连接服务器（非交互环境），已退出", color="31")
                sys.exit(2)

    if force_offline:
        print_lw_message("强制离线模式", color="33")

    # 清理旧日志（静默执行）
    try:
        cleanup_old_logs(config)
    except Exception:
        pass

    # 本地日志目录
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{task_id}.log"

    # 打印启动信息（到 stderr）
    print_lw_message(f"任务: {task_name} | ID: {task_id[:8]}...")
    print_lw_message(f"服务器: {server}")
    print_lw_message(f"执行: {command_str}")
    print_lw_message("─" * 50)

    # 创建上传器（注意：先 fork 再启动上传线程）
    uploader = None if offline_mode else LogUploader(server, task_id, log_file, user_id, config)
    uploader_started = False
    published = False
    email_start_sent = False  # 离线模式开始邮件是否已发送
    email_config = load_email_config(config) if offline_mode else None

    # 执行命令
    start_time = time.time()
    exit_code = 1
    publish_deadline = start_time + max(0, publish_grace_seconds)

    try:
        # 先 fork 执行命令，在 fork 之后再启动线程和网络请求，避免线程+fork 问题

        # 打开日志文件
        log_fd = open(log_file, "wb")
        master_fd, slave_fd = pty.openpty()
        exec_r, exec_w = os.pipe()
        os.set_inheritable(exec_w, False)

        pid = os.fork()
        if pid == 0:
            # 子进程 - 执行命令
            os.close(master_fd)
            os.close(exec_r)
            log_fd.close()
            os.setsid()

            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)

            try:
                os.execvp(command[0], command)
            except OSError as e:
                try:
                    os.write(exec_w, str(e.errno).encode("ascii", errors="ignore"))
                except OSError:
                    pass
                sys.stderr.write(f"执行失败: {e}\n")
                try:
                    os.close(exec_w)
                except OSError:
                    pass
            os._exit(127)
        else:
            # 父进程 - fork 完成后再启动线程和网络请求
            os.close(slave_fd)
            os.close(exec_w)
            child_pid = pid
            child_terminated = False
            exec_checked = False
            exec_ok = False

            def maybe_publish():
                nonlocal published, uploader_started, email_start_sent
                if not exec_ok:
                    return
                if time.time() < publish_deadline:
                    return

                # 离线模式：发送开始邮件
                if offline_mode:
                    if not email_start_sent and email_config:
                        send_task_start_email(email_config, task_name, machine, command_str)
                        email_start_sent = True
                    published = True
                    return

                # 在线模式：上传到服务器
                if published:
                    return
                if uploader and not uploader_started:
                    uploader.start()
                    uploader_started = True
                if not send_event(server, task_id, user_id, "start", task_name, machine, command_str,
                                  heartbeat_interval=uploader._heartbeat_interval if uploader else 30,
                                  uploader=uploader):
                    print_lw_message("警告: 无法上报任务开始事件", color="33")
                published = True

            # 设置信号处理
            original_sigint = signal.getsignal(signal.SIGINT)
            original_sigterm = signal.getsignal(signal.SIGTERM)

            def handle_signal(signum, _frame):
                if child_pid and not child_terminated:
                    try:
                        os.kill(child_pid, signum)
                    except OSError:
                        pass

            def handle_winch(_signum, _frame):
                try:
                    import fcntl
                    import termios
                    if sys.stdout.isatty():
                        s = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\x00' * 8)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, s)
                except (OSError, ValueError):
                    pass

            signal.signal(signal.SIGINT, handle_signal)
            signal.signal(signal.SIGTERM, handle_signal)
            signal.signal(signal.SIGWINCH, handle_winch)
            handle_winch(None, None)

            # 读取输出
            try:
                while True:
                    try:
                        rlist = [master_fd]
                        if not exec_checked:
                            rlist.append(exec_r)
                        rlist, _, _ = select.select(rlist, [], [], 0.1)
                    except (ValueError, OSError, InterruptedError):
                        try:
                            wpid, status = os.waitpid(pid, os.WNOHANG)
                            if wpid != 0:
                                if os.WIFEXITED(status):
                                    exit_code = os.WEXITSTATUS(status)
                                elif os.WIFSIGNALED(status):
                                    exit_code = 128 + os.WTERMSIG(status)
                                child_terminated = True
                                break
                        except ChildProcessError:
                            break
                        continue

                    if not exec_checked and exec_r in rlist:
                        try:
                            data = os.read(exec_r, 16)
                        except OSError:
                            data = b""
                        if data:
                            exec_ok = False
                            exec_checked = True
                        else:
                            exec_ok = True
                            exec_checked = True
                        try:
                            os.close(exec_r)
                        except OSError:
                            pass

                    maybe_publish()

                    if master_fd in rlist:
                        try:
                            data = os.read(master_fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        try:
                            sys.stdout.buffer.write(data)
                            sys.stdout.buffer.flush()
                        except (BrokenPipeError, OSError):
                            pass
                        log_fd.write(data)
                        log_fd.flush()
            finally:
                os.close(master_fd)
                log_fd.close()
                signal.signal(signal.SIGINT, original_sigint)
                signal.signal(signal.SIGTERM, original_sigterm)

            # 等待子进程
            if not child_terminated:
                try:
                    _, status = os.waitpid(pid, 0)
                    child_terminated = True
                    if os.WIFEXITED(status):
                        exit_code = os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        exit_code = 128 + os.WTERMSIG(status)
                except ChildProcessError:
                    pass

    except Exception as e:
        print_lw_message(f"执行出错: {e}", color="31")
        exit_code = 1

    elapsed = time.time() - start_time

    if not published and not offline_mode and exec_ok and elapsed >= publish_grace_seconds:
        if uploader and not uploader_started:
            uploader.start()
            uploader_started = True
        if not send_event(server, task_id, user_id, "start", task_name, machine, command_str, uploader=uploader):
            print_lw_message("警告: 无法上报任务开始事件", color="33")
        published = True

    # 停止上传（会做最后一次上传）
    if uploader and uploader_started:
        uploader.stop()
        if uploader.is_offline():
            offline_mode = True
            # 运行中熔断进入离线模式，需要加载邮件配置
            if email_config is None:
                email_config = load_email_config(config)

    # 上报任务结束
    event_type = "success" if exit_code == 0 else "failed"
    if published and not offline_mode:
        if not send_event(server, task_id, user_id, event_type, task_name, machine, command_str, exit_code, uploader=uploader):
            print_lw_message("警告: 无法上报任务结束事件", color="33")

    # 离线模式下发送邮件通知
    if offline_mode and email_config and email_config.get("enabled", False):
        send_task_notification_email(
            email_config=email_config,
            task_name=task_name,
            machine=machine,
            command=command_str,
            exit_code=exit_code,
            elapsed_seconds=int(elapsed),
            log_file=log_file,
        )

    # 打印结束信息
    minutes, seconds = divmod(int(elapsed), 60)
    hours, minutes = divmod(minutes, 60)
    time_str = f"{hours}h{minutes}m{seconds}s" if hours else f"{minutes}m{seconds}s"

    print_lw_message("─" * 50)
    status_text = "完成" if exit_code == 0 else f"退出 (code={exit_code})"
    color = "32" if exit_code == 0 else "31"
    print_lw_message(f"{status_text} | 耗时: {time_str}", color=color)

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
