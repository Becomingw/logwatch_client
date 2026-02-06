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
from urllib.request import Request, urlopen
from urllib.error import URLError
from collections import deque
from typing import Optional


# ── 配置 ──────────────────────────────────────────────

DEFAULT_SERVER = "http://127.0.0.1:8000"
CONFIG_PATH = Path.home() / ".lwconfig"
LOG_DIR = Path.home() / ".lw_logs"
UPLOAD_INTERVAL = 2  # 秒（实时上传）
LOG_RETENTION_DAYS = 7  # 本地日志保留天数
LOG_MAX_FILES = 1000  # 本地日志最大文件数
GZIP_MIN_BYTES = 64 * 1024  # 超过该大小才 gzip 压缩
UPLOAD_RETRY_TIMES = 3  # 上传失败重试次数
UPLOAD_RETRY_INTERVAL = 2  # 上传失败重试间隔（秒）
UPLOAD_CIRCUIT_BREAK_MINUTES = 5  # 熔断时长（分钟）
UPLOAD_CIRCUIT_BREAK_MAX = 3  # 熔断次数达到该值后进入离线模式
PUBLISH_GRACE_SECONDS = 1  # 发布前等待窗口（秒）
MAX_RETRY_QUEUE = 100  # 最大重试队列大小


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
        plain_body += f"\n\n--- 日志尾部 ---\n" + '\n'.join(log_lines)

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
# 值越小越实时，但会增加网络请求频率
# upload_interval_seconds=2

# 发布前等待窗口（秒，可选，默认 1 秒）
# 等待程序稳定后再开始上传，避免瞬间退出的程序产生无效日志
# publish_grace_seconds=1

# 本地日志保留天数（可选）
# log_retention_days=7

# 本地日志最大文件数（可选，超过则删除最旧的）
# log_max_files=1000

# 日志上传超过该大小才 gzip 压缩（单位 KB）
# upload_gzip_min_kb=64

# 上传失败重试次数（可选）
# upload_retry_times=3

# 上传失败重试间隔（秒，可选）
# upload_retry_interval_seconds=2

# 熔断时长（分钟，可选）
# upload_circuit_break_minutes=5

# 熔断次数达到该值后进入离线模式（可选）
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

def post_json(url: str, data: dict, timeout: float = 5, gzip_min_bytes: int = 0) -> bool:
    """POST JSON 到服务端，失败时静默返回 False"""
    try:
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if gzip_min_bytes > 0 and len(body) >= gzip_min_bytes:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        req = Request(url, data=body, headers=headers)
        urlopen(req, timeout=timeout)
        return True
    except (URLError, OSError, TimeoutError):
        return False


def check_server_connectivity(server: str) -> bool:
    """检查服务端是否可达（使用心跳接口，无需鉴权）"""
    try:
        url = f"{server.rstrip('/')}/api/heartbeat"
        # 发送一个空的心跳请求来测试连通性
        body = json.dumps({"task_id": "health-check", "timestamp": datetime.now(timezone.utc).isoformat()}).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        urlopen(req, timeout=3)
        return True
    except (URLError, OSError, TimeoutError):
        return False


# ── 日志上传线程 ──────────────────────────────────────

class LogUploader:
    """后台线程：定时将日志增量上传到服务端，支持失败重试"""

    def __init__(self, server: str, task_id: str, log_file: Path, user_id: str, config: dict):
        self.server = server.rstrip("/")
        self.task_id = task_id
        self.log_file = log_file
        self.user_id = user_id
        self._offset = 0
        self._stop = threading.Event()
        self._thread = None
        self._heartbeat_thread = None
        self._retry_queue = deque(maxlen=MAX_RETRY_QUEUE)
        self._lock = threading.Lock()
        self._circuit_until = 0.0
        self._circuit_count = 0
        self._offline = threading.Event()
        self._upload_interval = _get_int_config(config, "upload_interval_seconds", UPLOAD_INTERVAL)
        self._heartbeat_interval = 30  # 心跳间隔 30 秒
        self._gzip_min_bytes = _get_int_config(config, "upload_gzip_min_kb", GZIP_MIN_BYTES // 1024) * 1024
        self._retry_times = _get_int_config(config, "upload_retry_times", UPLOAD_RETRY_TIMES)
        self._retry_interval = _get_int_config(config, "upload_retry_interval_seconds", UPLOAD_RETRY_INTERVAL)
        self._circuit_break_seconds = _get_int_config(
            config, "upload_circuit_break_minutes", UPLOAD_CIRCUIT_BREAK_MINUTES
        ) * 60
        self._circuit_max = _get_int_config(config, "upload_circuit_break_max", UPLOAD_CIRCUIT_BREAK_MAX)
        self._last_heartbeat = 0.0

    def start(self):
        """启动上传线程和心跳线程"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._heartbeat_thread = threading.Thread(target=self._run_heartbeat, daemon=True)
        self._heartbeat_thread.start()

    def stop(self):
        """停止上传线程和心跳线程，并做最后一次上传"""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)
        # 最后上传一次，确保不丢日志
        self._upload()
        # 处理重试队列中剩余的内容
        self._flush_retry_queue()

    def _run(self):
        while not self._stop.wait(self._upload_interval):
            self._upload()
            self._flush_retry_queue()

    def _run_heartbeat(self):
        """心跳线程：定期发送心跳"""
        while not self._stop.wait(self._heartbeat_interval):
            self._send_heartbeat()

    def _send_heartbeat(self):
        """发送心跳到服务端"""
        if self._offline.is_set():
            return
        try:
            success = post_json(
                f"{self.server}/api/heartbeat",
                {
                    "task_id": self.task_id,
                    "user_id": self.user_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            if success:
                self._last_heartbeat = time.time()
        except Exception:
            pass  # 心跳失败不阻塞

    def _enter_offline(self):
        if not self._offline.is_set():
            self._offline.set()
            print_lw_message("上传多次熔断，进入离线模式", color="33")

    def is_offline(self) -> bool:
        return self._offline.is_set()

    def _post_with_retry(self, payload: dict) -> bool:
        if self._offline.is_set():
            return False
        if time.time() < self._circuit_until:
            return False
        retries = max(0, self._retry_times)
        for i in range(retries + 1):
            if post_json(
                f"{self.server}/api/log",
                payload,
                gzip_min_bytes=self._gzip_min_bytes,
            ):
                self._circuit_count = 0
                self._circuit_until = 0.0
                return True
            if i < retries:
                time.sleep(self._retry_interval)
        self._circuit_count += 1
        if self._circuit_max > 0 and self._circuit_count >= self._circuit_max:
            self._enter_offline()
        else:
            self._circuit_until = time.time() + self._circuit_break_seconds
        return False

    def _upload(self):
        try:
            if self._offline.is_set():
                return
            if time.time() < self._circuit_until:
                return
            with open(self.log_file, "rb") as f:
                f.seek(self._offset)
                chunk = f.read()
            if not chunk:
                return

            # 尝试解码为文本
            try:
                content = chunk.decode("utf-8", errors="replace")
            except Exception:
                content = chunk.decode("latin-1")

            new_offset = self._offset + len(chunk)

            success = self._post_with_retry({
                "task_id": self.task_id,
                "user_id": self.user_id,
                "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            if success:
                self._offset = new_offset
            else:
                # 上传失败，加入重试队列
                with self._lock:
                    self._retry_queue.append({
                        "content": content,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                self._offset = new_offset  # 继续读取新内容，旧内容在队列中重试

        except FileNotFoundError:
            pass
        except Exception:
            pass  # 静默处理其他异常

    def _flush_retry_queue(self):
        """尝试重发队列中的失败日志"""
        if self._offline.is_set():
            return
        if time.time() < self._circuit_until:
            return
        with self._lock:
            retry_items = list(self._retry_queue)
            self._retry_queue.clear()

        for item in retry_items:
            success = self._post_with_retry({
                "task_id": self.task_id,
                "user_id": self.user_id,
                "content": item["content"],
                "timestamp": item["timestamp"],
            })
            if not success:
                # 仍然失败，放回队列
                with self._lock:
                    if len(self._retry_queue) < MAX_RETRY_QUEUE:
                        self._retry_queue.append(item)
                if self._offline.is_set() or time.time() < self._circuit_until:
                    break


# ── 事件上报 ──────────────────────────────────────────

def send_event(server: str, task_id: str, user_id: str, event_type: str,
               name: str, machine: str, command: str,
               exit_code: Optional[int] = None, heartbeat_interval: Optional[int] = None,
               retries: int = 3) -> bool:
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
    # start 事件时发送心跳间隔，让服务端知道超时阈值
    if heartbeat_interval is not None:
        data["heartbeat_interval"] = heartbeat_interval
    url = f"{server.rstrip('/')}/api/event"

    for i in range(retries):
        if post_json(url, data):
            return True
        if i < retries - 1:
            time.sleep(1)  # 重试前等待
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
                                  heartbeat_interval=uploader._heartbeat_interval if uploader else 30):
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
        if not send_event(server, task_id, user_id, "start", task_name, machine, command_str):
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
        if not send_event(server, task_id, user_id, event_type, task_name, machine, command_str, exit_code):
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
