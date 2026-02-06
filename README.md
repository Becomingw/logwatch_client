<div align="center">

```
 ██╗      ██████╗  ██████╗ ██╗    ██╗ █████╗ ████████╗ ██████╗██╗  ██╗
 ██║     ██╔═══██╗██╔════╝ ██║    ██║██╔══██╗╚══██╔══╝██╔════╝██║  ██║
 ██║     ██║   ██║██║  ███╗██║ █╗ ██║███████║   ██║   ██║     ███████║
 ██║     ██║   ██║██║   ██║██║███╗██║██╔══██║   ██║   ██║     ██╔══██║
 ███████╗╚██████╔╝╚██████╔╝╚███╔███╔╝██║  ██║   ██║   ╚██████╗██║  ██║
 ╚══════╝ ╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝
```

**实时日志监控 · 零依赖 · 开箱即用**

[![PyPI](https://img.shields.io/pypi/v/logwatch-client?style=for-the-badge&logo=pypi&logoColor=white&color=3775a9)](https://pypi.org/project/logwatch-client/)
[![Python](https://img.shields.io/badge/Python-3.8+-3776ab?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-22c55e?style=for-the-badge)](LICENSE)
[![Linux](https://img.shields.io/badge/Linux-FCC624?style=for-the-badge&logo=linux&logoColor=black)](/)
[![macOS](https://img.shields.io/badge/macOS-000000?style=for-the-badge&logo=apple&logoColor=white)](/)

<br/>

包裹任意命令，实时上传日志到监控服务器

[快速开始](#快速开始) · [配置说明](#配置) · [邮件通知](#邮件通知)

---

</div>

<br/>

## 快速开始

<table>
<tr>
<td width="50%">

### 使用 uv（推荐）

```bash
uv tool install logwatch-client
```

</td>
<td width="50%">

### 使用 pip

```bash
pip install logwatch-client
```

</td>
</tr>
</table>

<details>
<summary><b>从源码安装</b></summary>

```bash
cd logwatch_client
uv pip install -e .
```

</details>

<br/>

## 使用方式

```bash
# 基础用法 - 包裹命令执行，自动上传日志
lw python train.py

# 指定任务名称
lw --name "resnet-v2-training" python train.py

# 指定服务器地址
lw --server http://your-server.com:8000 python train.py

# 强制离线模式（仅本地记录 + 邮件通知）
lw --offline python train.py
```

<br/>

## 系统要求

| 环境 | 要求 |
|:-----|:-----|
| Python | 3.8+ |
| 操作系统 | Linux / macOS（使用 PTY 捕获输出） |

<br/>

## 配置

首次使用前，运行以下命令生成配置模板：

```bash
lw --init
```

配置文件位于 `~/.lwconfig`：

```ini
# ┌─────────────────────────────────────────┐
# │       LogWatch 客户端配置文件            │
# └─────────────────────────────────────────┘

# 服务器地址（必填）
server=http://your-server.com:8000

# 机器标识（可选，默认使用 hostname）
machine=my-gpu-server

# 用户 ID（用于鉴权/多用户隔离）
user_id=alice
```

<details>
<summary><b>完整配置项</b></summary>

```ini
# 日志上传间隔（秒，默认 2 秒）
upload_interval_seconds=2

# 发布前等待窗口（秒，默认 1 秒）
# 瞬间退出的程序不会产生无效日志
publish_grace_seconds=1

# 本地日志保留天数（默认 7 天）
log_retention_days=7

# 本地日志最大文件数（默认 1000）
log_max_files=1000

# 日志超过该大小才 gzip 压缩（KB，默认 64）
upload_gzip_min_kb=64

# 上传失败重试次数（默认 3）
upload_retry_times=3

# 上传失败重试间隔（秒，默认 2）
upload_retry_interval_seconds=2

# 熔断时长（分钟，默认 5）
upload_circuit_break_minutes=5

# 熔断次数阈值（默认 3）
upload_circuit_break_max=3

# 强制始终使用离线模式
force_offline=false
```

</details>

<br/>

## 邮件通知

在离线模式下，任务完成后可通过邮件发送通知。支持 HTML 格式的精美邮件模板。

### 邮件配置

在 `~/.lwconfig` 中添加：

```ini
# ── 邮件通知配置 ─────────────────────────

# 是否启用邮件通知
email_enabled=true

# 通知类型：all=全部, failed=仅失败, success=仅成功
email_notify_on=all

# 任务开始时是否发送通知
email_notify_on_start=false

# SMTP 服务器配置
smtp_host=smtp.example.com
smtp_port=465
smtp_user=your-email@example.com
smtp_pass=your-password-or-auth-code
smtp_use_tls=true

# 发件人和收件人
email_from=your-email@example.com
email_to=notify@example.com
```

<details>
<summary><b>常见邮箱 SMTP 配置</b></summary>

| 邮箱服务 | SMTP 地址 | 端口 | 说明 |
|:---------|:----------|:-----|:-----|
| QQ 邮箱 | `smtp.qq.com` | 465 | 需要开启 SMTP 服务并获取授权码 |
| 163 邮箱 | `smtp.163.com` | 465 | 需要开启 SMTP 服务并设置授权码 |
| Gmail | `smtp.gmail.com` | 587 | 需要开启两步验证并使用应用密码 |
| Outlook | `smtp.office365.com` | 587 | 使用账户密码 |

</details>

<br/>

## 命令行参数

| 参数 | 简写 | 说明 | 默认值 |
|:-----|:----:|:-----|:-------|
| `--name` | `-n` | 任务名称 | 自动生成 |
| `--server` | `-s` | 服务器地址 | 读取 `~/.lwconfig` |
| `--machine` | `-m` | 机器标识 | 系统 hostname |
| `--user-id` | `-u` | 用户 ID | 读取 `~/.lwconfig` |
| `--init` | - | 生成配置文件模板 | - |
| `--no-check` | - | 跳过服务器连通性检查 | - |
| `--offline` | - | 强制离线模式 | - |

<br/>

## 特性

<table width="100%">
<tr>
<td align="center" width="25%"><b>零依赖</b><br/><sub>仅使用 Python 标准库</sub></td>
<td align="center" width="25%"><b>实时上传</b><br/><sub>每 2 秒增量上传日志</sub></td>
<td align="center" width="25%"><b>离线模式</b><br/><sub>支持本地记录 + 邮件通知</sub></td>
<td align="center" width="25%"><b>熔断保护</b><br/><sub>网络波动自动重试</sub></td>
</tr>
</table>

<br/>

---

<div align="center">

<sub>Made with ☕ by LogWatch Team</sub>

[![MIT License](https://img.shields.io/badge/License-MIT-gray?style=flat-square)](LICENSE)

</div>
