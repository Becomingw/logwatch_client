#!/usr/bin/env python3
"""
lw - LogWatch 客户端
包裹任意命令，捕获输出并上传到日志监控服务器。

使用方式:
    lw python train.py
    lw --name "resnet-v2" python train.py
    lw --server http://your-server.com python train.py
    lw --setup  # 交互式配置向导
    lw --health  # 健康检查（连通性/队列/邮件）

配置文件 (~/.lwconfig):
    server=http://your-server.com:8000
    machine=my-gpu-server  # 可选，默认用 hostname
    user_id=alice  # 必填，用于鉴权/多用户隔离
    user_token=ut_xxx  # 必填，用户设置中创建的 API Token
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
from enum import Enum
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
UPLOAD_CIRCUIT_BREAK_MAX = 5  # 连续重连失败达到该值后停止重连并进入完全离线
UPLOAD_TIMEOUT_SECONDS = 5
RETRY_BACKOFF_BASE_SECONDS = 5
RETRY_BACKOFF_MAX_SECONDS = 60
PUBLISH_GRACE_SECONDS = 1  # 发布前等待窗口（秒）
QUEUE_DB_PATH = LOG_DIR / "queue.db"

POST_OK = "ok"
POST_RETRYABLE_FAIL = "retryable_fail"
POST_TASK_DELETED = "task_deleted"


class TransportState(str, Enum):
    ONLINE = "online"
    RETRYING = "retrying"
    OFFLINE_GIVEUP = "offline_giveup"
    TASK_DELETED = "task_deleted"


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


def _escape_html(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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
        "start": ("任务已开始", "#007aff"),   # Apple Blue
        "success": ("执行成功", "#34c759"),  # Apple Green
        "failed": ("执行失败", "#ff3b30"),   # Apple Red
    }
    status_text, status_color = status_map.get(status, status_map["success"])
    
    subject = f"[LogWatch] {task_name} - {status_text}"

    # 纯文本版本
    plain_body = f"""状态: {status_text}
任务: {task_name}
机器: {machine}
触发时间: {now}
"""

    duration_str = "-"
    if elapsed_seconds is not None:
        duration_str = _format_duration(elapsed_seconds)
    plain_body += f"耗时: {duration_str}\n"

    exit_code_str = str(exit_code) if exit_code is not None else "-"
    plain_body += f"退出码: {exit_code_str}\n"

    plain_body += f"命令: {command}\n"

    if tail_logs:
        log_lines = tail_logs.strip().split('\n')[-15:]
        plain_body += "\n\n─── 最新日志 ───\n" + '\n'.join(log_lines)

    plain_body += f"\n\n此邮件由 LogWatch 客户端离线模式发送"

    safe_task_name = _escape_html(task_name)
    safe_machine = _escape_html(machine)
    command_preview = command[:200] + ("..." if len(command) > 200 else "")
    safe_command = _escape_html(command_preview)

    metrics_rows = ""
    if exit_code is not None:
        metrics_rows += f"""
                                    <tr>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea;">
                                            <span style="font-size: 14px; color: #86868b; font-weight: 500;">退出码</span>
                                        </td>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea; text-align: right;">
                                            <span style="font-size: 15px; color: #1d1d1f; font-weight: 600;">{exit_code}</span>
                                        </td>
                                    </tr>"""
    if elapsed_seconds is not None:
        metrics_rows += f"""
                                    <tr>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea;">
                                            <span style="font-size: 14px; color: #86868b; font-weight: 500;">耗时</span>
                                        </td>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea; text-align: right;">
                                            <span style="font-size: 15px; color: #1d1d1f;">{_format_duration(elapsed_seconds)}</span>
                                        </td>
                                    </tr>"""

    logs_html = ""
    if tail_logs:
        log_lines = tail_logs.strip().split("\n")[-15:]
        escaped_logs = _escape_html("\n".join(log_lines))
        logs_html = f"""
                                <div style="margin-top: 32px;">
                                    <h3 style="margin: 0 0 12px; font-size: 15px; color: #1d1d1f; font-weight: 600;">最新日志</h3>
                                    <div style="padding: 16px; background-color: #f5f5f7; border-radius: 8px; overflow-x: auto;">
                                        <pre style="margin: 0; font-family: -apple-system, BlinkMacSystemFont, monospace; font-size: 12px; color: #1d1d1f; white-space: pre-wrap; word-break: break-all; line-height: 1.5;">{escaped_logs}</pre>
                                    </div>
                                </div>"""

    html_body = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{subject}}</title>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f5f5f7; color: #1d1d1f; -webkit-font-smoothing: antialiased;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width: 100%; height: 100%; background-color: #f5f5f7;">
        <tr>
            <td align="center" style="padding: 40px 20px;">
                <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width: 100%; max-width: 600px; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.04);">
                    <tr>
                        <td style="padding: 40px 40px 20px; text-align: center;">
                            <h1 style="margin: 0; font-size: 28px; font-weight: 600; color: #1d1d1f; letter-spacing: -0.5px;">LogWatch</h1>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 0 40px 40px;">
                            <div>
                                <h2 style="margin: 0 0 8px; font-size: 24px; font-weight: 600; color: {status_color}; text-align: center;">{status_text}</h2>
                                <p style="margin: 0 0 32px; font-size: 17px; color: #1d1d1f; text-align: center;">{safe_task_name}</p>
                                
                                <table role="presentation" cellpadding="0" cellspacing="0" style="width: 100%; border-collapse: collapse;">
                                    <tr>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea; width: 80px;">
                                            <span style="font-size: 14px; color: #86868b; font-weight: 500;">机器</span>
                                        </td>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea; text-align: right;">
                                            <span style="font-size: 15px; color: #1d1d1f;">{safe_machine}</span>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea;">
                                            <span style="font-size: 14px; color: #86868b; font-weight: 500;">触发时间</span>
                                        </td>
                                        <td style="padding: 12px 0; border-bottom: 1px solid #e5e5ea; text-align: right;">
                                            <span style="font-size: 15px; color: #1d1d1f;">{now}</span>
                                        </td>
                                    </tr>
{metrics_rows}
                                    <tr>
                                        <td style="padding: 16px 0 0;" colspan="2">
                                            <span style="font-size: 14px; color: #86868b; font-weight: 500; display: block; margin-bottom: 8px;">执行命令</span>
                                            <div style="font-size: 13px; color: #1d1d1f; font-family: -apple-system, BlinkMacSystemFont, monospace; background-color: #f5f5f7; padding: 12px; border-radius: 6px; word-break: break-all;">{safe_command}</div>
                                        </td>
                                    </tr>
                                </table>
{logs_html}
                            </div>
                        </td>
                    </tr>
                    <tr>
                        <td style="padding: 20px 40px 30px; background-color: #fafafa; text-align: center; border-top: 1px solid #f0f0f0;">
                            <p style="margin: 0; font-size: 12px; color: #86868b; line-height: 1.5;">由 LogWatch 客户端离线模式发送</p>
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
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


def _config_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() == "true"


def _queue_health() -> tuple[bool, str]:
    if not QUEUE_DB_PATH.exists():
        return True, f"queue=absent path={QUEUE_DB_PATH}"
    try:
        conn = sqlite3.connect(str(QUEUE_DB_PATH), timeout=5)
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM log_queue GROUP BY status"
        ).fetchall()
        conn.close()
        counts = {str(status): int(cnt) for status, cnt in rows}
        total = sum(counts.values())
        detail = ", ".join([f"{k}:{v}" for k, v in sorted(counts.items())]) or "empty"
        return True, f"queue=ok path={QUEUE_DB_PATH} total={total} ({detail})"
    except Exception as exc:
        return False, f"queue=error path={QUEUE_DB_PATH} error={exc}"


def _smtp_health(email_config: dict) -> tuple[bool, str]:
    recipient = (email_config.get("to") or "").strip()
    sender = (email_config.get("from") or "").strip()
    smtp_host = (email_config.get("smtp_host") or "").strip()
    if not smtp_host:
        return False, "email=error smtp_host 未配置"
    if not recipient or not sender:
        return False, "email=error email_to 或 email_from 未配置"

    port = int(email_config.get("smtp_port", 465))
    use_tls = bool(email_config.get("smtp_use_tls", True))
    smtp_user = (email_config.get("smtp_user") or "").strip()
    smtp_pass = (email_config.get("smtp_pass") or "").strip()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(smtp_host, port, timeout=10) as server:
                server.ehlo()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
        else:
            with smtplib.SMTP(smtp_host, port, timeout=10) as server:
                server.ehlo()
                if use_tls:
                    server.starttls()
                    server.ehlo()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
        return True, f"email=ok smtp={smtp_host}:{port} tls={'on' if use_tls else 'off'}"
    except Exception as exc:
        return False, f"email=error smtp={smtp_host}:{port} detail={exc}"


def run_health_check(args: argparse.Namespace) -> int:
    config = load_config()
    server = args.server or config.get("server") or DEFAULT_SERVER
    machine = args.machine or config.get("machine") or socket.gethostname()
    user_id = getattr(args, "user_id", None) or get_user_id(config) or ""
    user_token = getattr(args, "user_token", None) or get_user_token(config) or ""
    server_ok = check_server_connectivity(server)

    checks: list[tuple[str, bool, str]] = []
    checks.append(("config", CONFIG_PATH.exists(), f"config={'ok' if CONFIG_PATH.exists() else 'missing'} path={CONFIG_PATH}"))
    checks.append(("auth_user_id", bool(user_id), f"auth_user_id={'ok' if user_id else 'missing'}"))
    checks.append(("auth_user_token", bool(user_token), f"auth_user_token={'ok' if user_token else 'missing'}"))
    checks.append(("server", server_ok, f"server={'ok' if server_ok else 'fail'} url={server}"))
    queue_ok, queue_msg = _queue_health()
    checks.append(("queue", queue_ok, queue_msg))

    email_enabled = _config_bool(config.get("email_enabled", "false"), default=False)
    if email_enabled:
        email_config = load_email_config(config)
        if not email_config:
            checks.append(("email", False, "email=error 启用了 email_enabled 但邮箱配置不完整"))
        else:
            mail_ok, mail_msg = _smtp_health(email_config)
            checks.append(("email", mail_ok, mail_msg))
    else:
        checks.append(("email", True, "email=skipped 离线邮件未启用"))

    print_lw_message(f"health target server={server} machine={machine} user_id={user_id or '-'}", color="90")
    all_ok = True
    for _, ok, msg in checks:
        print_lw_message(msg, color="32" if ok else "31")
        all_ok = all_ok and ok
    print_lw_message(f"health result={'PASS' if all_ok else 'FAIL'}", color="32" if all_ok else "31")
    return 0 if all_ok else 2


def _prompt_text(label: str, default: str = "", required: bool = False) -> str:
    prompt = f"{label}"
    if default:
        prompt += f" [{default}]"
    prompt += ": "
    while True:
        try:
            value = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not value:
            value = default
        if required and not value:
            print("该项不能为空，请重试。")
            continue
        return value


def _prompt_yes_no(label: str, default: bool = True) -> bool:
    prompt = f"{label} [{'Y/n' if default else 'y/N'}]: "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def _write_config(config: dict) -> None:
    ordered_keys = [
        "server",
        "machine",
        "user_id",
        "user_token",
        "upload_interval_seconds",
        "batch_size",
        "batch_interval_ms",
        "compression_level",
        "publish_grace_seconds",
        "log_retention_days",
        "log_max_files",
        "upload_circuit_break_max",
        "force_offline",
        "email_enabled",
        "email_notify_on",
        "email_notify_on_start",
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_pass",
        "smtp_use_tls",
        "email_from",
        "email_to",
    ]
    lines = []
    emitted = set()
    for key in ordered_keys:
        if key not in config:
            continue
        value = str(config[key]).strip()
        if value == "":
            continue
        lines.append(f"{key}={value}")
        emitted.add(key)
    for key in sorted(config.keys()):
        if key in emitted:
            continue
        value = str(config[key]).strip()
        if value == "":
            continue
        lines.append(f"{key}={value}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n")


def setup_config():
    """交互式配置向导：引导完成基础配置，并执行连通性与上报测试。"""
    existing = load_config()
    print(f"LogWatch Setup 向导（配置文件: {CONFIG_PATH}）")

    server_default = existing.get("server", DEFAULT_SERVER)
    machine_default = existing.get("machine", socket.gethostname())
    user_default = existing.get("user_id", os.environ.get("LW_USER_ID", ""))
    token_default = existing.get("user_token", os.environ.get("LW_USER_TOKEN", ""))

    server = _prompt_text("服务端地址", server_default, required=True)
    user_id = _prompt_text("用户 ID", user_default, required=True)
    user_token = _prompt_text("用户 Token（user settings 中创建）", token_default, required=True)
    machine = _prompt_text("机器标识", machine_default, required=True)

    config = dict(existing)
    config["server"] = server
    config["user_id"] = user_id
    config["user_token"] = user_token
    config["machine"] = machine

    email_default = str(existing.get("email_enabled", "false")).lower() == "true"
    if _prompt_yes_no("是否配置离线邮件通知", email_default):
        config["email_enabled"] = "true"
        config["email_notify_on"] = _prompt_text(
            "邮件通知类型(all/failed/success)",
            existing.get("email_notify_on", "all"),
            required=True,
        ).lower()
        if config["email_notify_on"] not in ("all", "failed", "success"):
            config["email_notify_on"] = "all"
        config["email_notify_on_start"] = "true" if _prompt_yes_no(
            "任务开始时发送邮件",
            str(existing.get("email_notify_on_start", "false")).lower() == "true",
        ) else "false"
        config["smtp_host"] = _prompt_text("SMTP 主机", existing.get("smtp_host", ""), required=True)
        config["smtp_port"] = _prompt_text("SMTP 端口", existing.get("smtp_port", "465"), required=True)
        config["smtp_user"] = _prompt_text("SMTP 用户名", existing.get("smtp_user", ""))
        config["smtp_pass"] = _prompt_text("SMTP 密码/授权码", existing.get("smtp_pass", ""))
        config["smtp_use_tls"] = "true" if _prompt_yes_no(
            "启用 TLS(非 465 端口建议开启)",
            str(existing.get("smtp_use_tls", "true")).lower() == "true",
        ) else "false"
        config["email_from"] = _prompt_text("发件人邮箱", existing.get("email_from", ""), required=True)
        config["email_to"] = _prompt_text("收件人邮箱", existing.get("email_to", ""), required=True)
    else:
        config["email_enabled"] = "false"

    _write_config(config)
    print(f"配置已写入: {CONFIG_PATH}")

    connectivity_ok = check_server_connectivity(server)
    if connectivity_ok:
        print("连通性测试: OK")
    else:
        print("连通性测试: FAIL（服务端暂不可达）")

    if not _prompt_yes_no("是否执行进阶测试（上报 start/heartbeat/success）", connectivity_ok):
        return

    setup_task_id = f"setup-{uuid.uuid4()}"
    setup_name = "lw-setup-test"
    setup_command = "lw --setup"
    setup_cwd = str(Path.cwd().resolve())
    setup_pid = os.getpid()
    setup_pyver = sys.version.split()[0]
    start_ok = send_event(
        server=server,
        task_id=setup_task_id,
        user_id=user_id,
        user_token=user_token,
        event_type="start",
        name=setup_name,
        machine=machine,
        command=setup_command,
        heartbeat_interval=30,
        retries=2,
        cwd=setup_cwd,
        pid=setup_pid,
        python_version=setup_pyver,
    )
    heartbeat_status = post_json_status(
        f"{server.rstrip('/')}/api/heartbeat",
        {"task_id": setup_task_id, "user_id": user_id, "timestamp": datetime.now(timezone.utc).isoformat()},
        timeout=UPLOAD_TIMEOUT_SECONDS,
        auth_headers=build_user_auth_headers(user_id=user_id, user_token=user_token),
    )
    finish_ok = send_event(
        server=server,
        task_id=setup_task_id,
        user_id=user_id,
        user_token=user_token,
        event_type="success",
        name=setup_name,
        machine=machine,
        command=setup_command,
        exit_code=0,
        retries=2,
        cwd=setup_cwd,
        pid=setup_pid,
        python_version=setup_pyver,
    )
    print(
        f"进阶测试结果: start={'OK' if start_ok else 'FAIL'}, "
        f"heartbeat={'OK' if heartbeat_status == POST_OK else 'FAIL'}, "
        f"finish={'OK' if finish_ok else 'FAIL'}"
    )


# ── HTTP 工具 ──────────────────────────────────────────

def _normalized_compression_level(level: int) -> int:
    if level < 1:
        return 1
    if level > 9:
        return 9
    return level


def build_user_auth_headers(user_id: str, user_token: str) -> dict:
    headers = {}
    if user_token:
        headers["Authorization"] = f"Bearer {user_token}"
    if user_id:
        headers["X-User-Id"] = user_id
    return headers


def post_json_status_with_response(
    url: str,
    data: dict,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
    gzip_min_bytes: int = 0,
    compression_level: int = COMPRESSION_LEVEL,
    auth_headers: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> tuple[str, Optional[dict], int]:
    """POST JSON 到服务端，返回请求状态、JSON 响应和 HTTP 状态码。"""
    own_session = session is None
    http = session or requests.Session()
    try:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth_headers:
            headers.update(auth_headers)
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
    auth_headers: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> str:
    status, _payload, _code = post_json_status_with_response(
        url=url,
        data=data,
        timeout=timeout,
        gzip_min_bytes=gzip_min_bytes,
        compression_level=compression_level,
        auth_headers=auth_headers,
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
    auth_headers: Optional[dict] = None,
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
        auth_headers=auth_headers,
        session=session,
        request_lock=request_lock,
    ) == POST_OK


def get_json_status(
    url: str,
    params: Optional[dict] = None,
    timeout: float = UPLOAD_TIMEOUT_SECONDS,
    auth_headers: Optional[dict] = None,
    session: Optional[requests.Session] = None,
    request_lock: Optional[threading.Lock] = None,
) -> tuple[str, Optional[dict], int]:
    own_session = session is None
    http = session or requests.Session()
    try:
        if request_lock:
            with request_lock:
                resp = http.get(url, params=params, headers=auth_headers, timeout=timeout)
        else:
            resp = http.get(url, params=params, headers=auth_headers, timeout=timeout)
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
    """检查服务端是否可达（使用公开健康检查接口）。"""
    status, _payload, _code = get_json_status(
        f"{server.rstrip('/')}/api/health",
        timeout=3,
    )
    return status == POST_OK


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

    def __init__(
        self,
        server: str,
        task_id: str,
        log_file: Path,
        user_id: str,
        user_token: str,
        config: dict,
    ):
        self.server = server.rstrip("/")
        self.task_id = task_id
        self.log_file = log_file
        self.user_id = user_id
        self.user_token = user_token
        self._auth_headers = build_user_auth_headers(user_id=user_id, user_token=user_token)
        self._offset = 0
        self._stop = threading.Event()
        self._thread = None
        self._heartbeat_thread = None
        self._request_lock = threading.Lock()
        self._session: Optional[requests.Session] = None
        self._queue = LogQueueStore(QUEUE_DB_PATH)
        self._transient_offline = threading.Event()
        self._offline = threading.Event()
        self._task_deleted = threading.Event()
        self._state_lock = threading.Lock()
        self._transport_state = TransportState.ONLINE
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

    def _get_transport_state(self) -> TransportState:
        with self._state_lock:
            return self._transport_state

    def _set_transport_state(self, new_state: TransportState) -> tuple[TransportState, bool]:
        with self._state_lock:
            old_state = self._transport_state
            if old_state == TransportState.TASK_DELETED and new_state != TransportState.TASK_DELETED:
                return old_state, False
            if old_state == new_state:
                return old_state, False
            self._transport_state = new_state
            return old_state, True

    def _sync_state_events(self, state: TransportState):
        if state == TransportState.ONLINE:
            self._transient_offline.clear()
            self._offline.clear()
            self._task_deleted.clear()
        elif state == TransportState.RETRYING:
            self._transient_offline.set()
            self._offline.clear()
            self._task_deleted.clear()
        elif state == TransportState.OFFLINE_GIVEUP:
            self._transient_offline.set()
            self._offline.set()
            self._task_deleted.clear()
        else:
            self._transient_offline.set()
            self._offline.set()
            self._task_deleted.set()

    def _mark_transport_success(self, source: str):
        old_state, changed = self._set_transport_state(TransportState.ONLINE)
        self._sync_state_events(TransportState.ONLINE)
        self._circuit_count = 0
        self._next_retry_at = 0.0
        self._retry_backoff_seconds = RETRY_BACKOFF_BASE_SECONDS
        if changed and old_state == TransportState.RETRYING:
            print_lw_message(f"{source}恢复，已重新连接服务端", color="32")

    def _mark_retryable_failure(self, source: str, count_towards_giveup: bool = True):
        state = self._get_transport_state()
        if state in (TransportState.OFFLINE_GIVEUP, TransportState.TASK_DELETED):
            return
        old_state, changed = self._set_transport_state(TransportState.RETRYING)
        self._sync_state_events(TransportState.RETRYING)
        if changed and old_state == TransportState.ONLINE:
            print_lw_message("上传连续失败，进入离线模式（日志保留在本地 WAL 队列）", color="33")
        self._next_retry_at = time.time() + self._retry_backoff_seconds
        self._retry_backoff_seconds = min(self._retry_backoff_seconds * 2, RETRY_BACKOFF_MAX_SECONDS)
        if not count_towards_giveup:
            return
        self._circuit_count += 1
        if self._circuit_count >= self._circuit_max:
            self._enter_offline()

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
            auth_headers=self._auth_headers,
            session=self._session,
            request_lock=self._request_lock,
        )
        if status == POST_TASK_DELETED:
            self._abandon_task_push("续传 ACK 查询")
            return
        if status == POST_OK:
            self._mark_transport_success("续传 ACK 查询")
            try:
                self._last_ack_seq = int((payload or {}).get("last_ack_seq", 0) or 0)
            except (TypeError, ValueError):
                self._last_ack_seq = 0
        elif code != 404:
            # 网络失败或其他异常状态：保持本地序列继续，不阻塞任务执行
            self._mark_retryable_failure("续传 ACK 查询")

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
            auth_headers=self._auth_headers,
            session=self._session,
            request_lock=self._request_lock,
        )
        if status == POST_OK:
            self._last_heartbeat = time.time()
            self._mark_transport_success("心跳")
        elif status == POST_TASK_DELETED:
            self._abandon_task_push("心跳")
        else:
            self._mark_retryable_failure("心跳")

    def _enter_offline(self):
        old_state, changed = self._set_transport_state(TransportState.OFFLINE_GIVEUP)
        if not changed:
            return
        self._sync_state_events(TransportState.OFFLINE_GIVEUP)
        if old_state != TransportState.TASK_DELETED:
            print_lw_message("连续多次重连失败，停止重连并进入完全离线模式", color="33")

    def _abandon_task_push(self, source: str):
        _old_state, changed = self._set_transport_state(TransportState.TASK_DELETED)
        if not changed:
            return
        self._sync_state_events(TransportState.TASK_DELETED)
        self._queue.archive_task(self.task_id, reason=f"task deleted: {source}")
        print_lw_message(
            f"任务已被服务端删除（{source}收到 HTTP 409），后续日志转归档状态",
            color="33",
        )

    def mark_task_deleted(self, source: str):
        self._abandon_task_push(source)

    def mark_transport_success(self, source: str):
        self._mark_transport_success(source)

    def mark_retryable_failure(self, source: str, count_towards_giveup: bool = True):
        self._mark_retryable_failure(source, count_towards_giveup=count_towards_giveup)

    def get_transport_state(self) -> str:
        return self._get_transport_state().value

    def is_task_deleted(self) -> bool:
        return self._get_transport_state() == TransportState.TASK_DELETED

    def is_offline(self) -> bool:
        return self._get_transport_state() in (TransportState.OFFLINE_GIVEUP, TransportState.TASK_DELETED)

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
            auth_headers=self._auth_headers,
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
            self._mark_transport_success("日志上传")
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
        self._mark_retryable_failure("日志上传", count_towards_giveup=False)


# ── 事件上报 ──────────────────────────────────────────

def send_event(server: str, task_id: str, user_id: str, user_token: str, event_type: str,
               name: str, machine: str, command: str,
               exit_code: Optional[int] = None, heartbeat_interval: Optional[int] = None,
               retries: int = 3, uploader: Optional[LogUploader] = None,
               cwd: str = "", pid: Optional[int] = None, python_version: str = "") -> bool:
    """上报任务事件（开始/结束/失败），支持重试"""
    data = {
        "task_id": task_id,
        "user_id": user_id,
        "type": event_type,
        "name": name,
        "machine": machine,
        "command": command,
        "cwd": cwd,
        "pid": pid,
        "python_version": python_version,
        "exit_code": exit_code,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if heartbeat_interval is not None:
        data["heartbeat_interval"] = heartbeat_interval
    url = f"{server.rstrip('/')}/api/event"
    session = uploader.get_http_session() if uploader else None
    request_lock = uploader.get_request_lock() if uploader else None
    auth_headers = build_user_auth_headers(user_id=user_id, user_token=user_token)

    for i in range(retries):
        status = post_json_status(
            url,
            data,
            timeout=UPLOAD_TIMEOUT_SECONDS,
            auth_headers=auth_headers,
            session=session,
            request_lock=request_lock,
        )
        if status == POST_OK:
            if uploader:
                uploader.mark_transport_success("事件上报")
            return True
        if status == POST_TASK_DELETED:
            if uploader:
                uploader.mark_task_deleted("事件上报")
            else:
                print_lw_message("任务已被服务端删除（事件上报收到 HTTP 409），停止该任务后续上报", color="33")
            return False
        if uploader:
            uploader.mark_retryable_failure("事件上报", count_towards_giveup=False)
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


def get_user_token(config: dict) -> str | None:
    """获取用户 Token：优先使用配置文件，其次环境变量。"""
    return config.get("user_token") or os.environ.get("LW_USER_TOKEN")


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
    parser.add_argument("--user-token", help="用户 Token（默认读取 ~/.lwconfig 或 LW_USER_TOKEN）")
    parser.add_argument("--setup", action="store_true", help="交互式配置向导并执行连通测试")
    parser.add_argument("--health", action="store_true", help="健康检查（连通性、队列、离线邮件）")
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-check", action="store_true", help="跳过服务器连通性检查")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="要执行的命令")

    args = parser.parse_args()

    # 处理 --setup
    if args.setup or args.init:
        if args.init:
            print_lw_message("`--init` 已废弃，请使用 `--setup`", color="33")
        setup_config()
        sys.exit(0)

    if args.health:
        sys.exit(run_health_check(args))

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

    # user_token: 命令行 > 配置文件 > 环境变量
    user_token = getattr(args, "user_token", None) or get_user_token(config)

    task_id = str(uuid.uuid4())
    task_name = args.name or f"{machine}-{datetime.now().strftime('%m%d-%H%M%S')}"
    command_str = " ".join(command)
    command_cwd = str(Path.cwd().resolve())
    task_python_version = sys.version.split()[0]
    publish_grace_seconds = _get_int_config(config, "publish_grace_seconds", PUBLISH_GRACE_SECONDS)

    # 预检查命令
    precheck_code = precheck_command(command)
    if precheck_code != 0:
        sys.exit(precheck_code)

    # 检查是否强制离线模式
    force_offline = config.get("force_offline", "false").lower() == "true"
    offline_mode = force_offline

    if not user_token and not offline_mode:
        print_lw_message("错误: 未设置 user_token", color="31")
        print_lw_message("请使用以下方式之一设置:", color="31")
        print_lw_message("  1. 命令行参数: lw --user-token YOUR_TOKEN ...", color="31")
        print_lw_message("  2. 配置文件 ~/.lwconfig: user_token=YOUR_TOKEN", color="31")
        print_lw_message("  3. 环境变量: export LW_USER_TOKEN=YOUR_TOKEN", color="31")
        print_lw_message("  4. 运行 lw --setup 交互式配置", color="31")
        sys.exit(1)

    # 检查服务器连通性（可选，非强制离线时）
    if not offline_mode and not args.no_check:
        if not check_server_connectivity(server):
            print_lw_message("无法连接服务器，先按离线模式提示运行并自动重试连接", color="33")

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
    uploader = None if offline_mode else LogUploader(server, task_id, log_file, user_id, user_token or "", config)
    uploader_started = False
    published = False
    email_start_sent = False  # 离线模式开始邮件是否已发送
    email_config = load_email_config(config) if offline_mode else None

    # 执行命令
    start_time = time.time()
    exit_code = 1
    task_pid: Optional[int] = None
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
            task_pid = child_pid
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
                if not send_event(server, task_id, user_id, user_token or "", "start", task_name, machine, command_str,
                                  heartbeat_interval=uploader._heartbeat_interval if uploader else 30,
                                  uploader=uploader, cwd=command_cwd,
                                  pid=task_pid, python_version=task_python_version):
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
        if not send_event(
            server, task_id, user_id, user_token or "", "start", task_name, machine, command_str,
            uploader=uploader, cwd=command_cwd, pid=task_pid, python_version=task_python_version
        ):
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
        if not send_event(
            server, task_id, user_id, user_token or "", event_type, task_name, machine, command_str, exit_code,
            uploader=uploader, cwd=command_cwd, pid=task_pid, python_version=task_python_version
        ):
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
