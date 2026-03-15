<div align="center">

```
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║   ██╗      ██████╗  ██████╗ ██╗    ██╗ █████╗ ████████╗ ██████╗██╗  ██╗ ║
║   ██║     ██╔═══██╗██╔════╝ ██║    ██║██╔══██╗╚══██╔══╝██╔════╝██║  ██║ ║
║   ██║     ██║   ██║██║  ███╗██║ █╗ ██║███████║   ██║   ██║     ███████║ ║
║   ██║     ██║   ██║██║   ██║██║███╗██║██╔══██║   ██║   ██║     ██╔══██║ ║
║   ███████╗╚██████╔╝╚██████╔╝╚███╔███╔╝██║  ██║   ██║   ╚██████╗██║  ██║ ║
║   ╚══════╝ ╚═════╝  ╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝   ╚═╝    ╚═════╝╚═╝  ╚═╝ ║
║                                                                          ║
╚══════════════════════════════════════════════════════════════════════════╝
```

<h3>⚡ 实时日志监控客户端 ⚡</h3>

<p>
  <img src="https://img.shields.io/pypi/v/logwatch-client?style=for-the-badge&logo=pypi&logoColor=white&color=00d9ff&labelColor=0a0e27" alt="PyPI"/>
  <img src="https://img.shields.io/badge/Python-3.8+-00d9ff?style=for-the-badge&logo=python&logoColor=white&labelColor=0a0e27" alt="Python"/>
  <img src="https://img.shields.io/badge/License-MIT-00ff88?style=for-the-badge&labelColor=0a0e27" alt="License"/>
</p>

```
┌─────────────────────────────────────────────────────────────┐
│  包裹任意命令 → 实时上传日志 → LogWatch 服务端              │
└─────────────────────────────────────────────────────────────┘
```

</div>

<br>

## 🚀 快速开始

<table>
<tr>
<td width="50%">

### 📦 安装

```bash
# 推荐使用 uv（更快）
uv tool install logwatch-client

# 或使用 pip
pip install logwatch-client
```

</td>
<td width="50%">

### ⚙️ 初始化

```bash
# 交互式配置向导
lw --setup

# 健康检查
lw --health
```

</td>
</tr>
</table>

### 🎯 运行任务

```bash
lw --user-id 104698 --user-token ut_xxx python long_running_test.py
```

> `lw` 会为每次执行自动生成新的 UUID `task_id`。这个 ID 只代表这一次运行；服务端删除后不会复用，也不会被后续推送重新激活。

> 如果任务在服务端被删除，客户端会把后续日志转为本地归档，不再尝试重新激活该任务；若只是网络中断，则会进入低频离线探测并在恢复后继续上传。

<br>

---

<br>

## 🤖 AI 辅助安装与配置

> 让 AI 助手自动完成安装和配置流程

<details open>
<summary><b>📋 准备工作</b></summary>

<br>

在开始之前，请准备以下信息：

- 🌐 **服务端地址** — 例如 `http://127.0.0.1:8000`
- 🆔 **用户 ID** — 在网页中查看
- 🔑 **用户 Token** — 在网页设置中创建

</details>

<details>
<summary><b>✨ 一键安装（推荐）</b></summary>

<br>

复制以下内容发给 AI 助手（Codex / Claude Code）：

```text
请帮我安装和配置 logwatch-client：
参考 AI 协作手册 (https://raw.githubusercontent.com/Becomingw/logwatch_client/refs/heads/main/docs/ai-assistant-playbook.md) 来完成
```

</details>

<details>
<summary><b>🛠️ 手动安装</b></summary>

<br>

```bash
# 安装客户端
uv tool install logwatch-client || pip install logwatch-client

# 运行配置向导
lw --setup

# 验证安装
lw --health
```

</details>

<br>

---

<br>

## 📝 配置文件

**配置文件路径：** `~/.lwconfig`

### 最小可用配置

```ini
server=http://127.0.0.1:8000
machine=my-macbook
user_id=104698
user_token=ut_xxx
```

> 💡 **鉴权说明：** 客户端上报接口统一使用 `user_id + user_token`

<br>

---

<br>

## 💻 常用命令

```bash
# 配置向导
lw --setup

# 健康检查
lw --health

# 运行任务（带名称）
lw --name "train-exp-1" python train.py

# 指定服务器
lw --server http://127.0.0.1:8000 python train.py

# 跳过连通性检查
lw --no-check python train.py
```

<br>

---

<br>

## 📖 命令行参数

<div align="center">

| 参数 | 简写 | 说明 |
|:-----|:----:|:-----|
| `--name` | `-n` | 任务名称 |
| `--server` | `-s` | 服务器地址 |
| `--machine` | `-m` | 机器标识 |
| `--user-id` | `-u` | 用户 ID |
| `--user-token` | — | 用户 Token |
| `--setup` | — | 交互式配置向导（含连通+上报测试） |
| `--health` | — | 健康检查（连通性/队列/离线邮件） |
| `--no-check` | — | 跳过启动前连通性检查 |
| `--init` | — | 已废弃，等价于 `--setup` |

</div>

<br>

---

<br>

## ⚙️ 完整配置项

<details>
<summary><b>展开查看所有配置选项</b></summary>

<br>

```ini
# ═══════════════════════════════════════════════════════════
# 基础配置
# ═══════════════════════════════════════════════════════════
server=http://127.0.0.1:8000
machine=my-macbook
user_id=104698
user_token=ut_xxx

# ═══════════════════════════════════════════════════════════
# 上传配置
# ═══════════════════════════════════════════════════════════
upload_interval_seconds=2
batch_size=100
batch_interval_ms=5000
compression_level=6
publish_grace_seconds=1
upload_circuit_break_max=5

# ═══════════════════════════════════════════════════════════
# 日志管理
# ═══════════════════════════════════════════════════════════
log_retention_days=7
log_max_files=1000
force_offline=false

# ═══════════════════════════════════════════════════════════
# 邮件通知
# ═══════════════════════════════════════════════════════════
email_enabled=false
email_notify_on=all
email_notify_on_start=false
smtp_host=smtp.example.com
smtp_port=465
smtp_user=your-email@example.com
smtp_pass=your-password
smtp_use_tls=true
email_from=your-email@example.com
email_to=notify@example.com
```

</details>

<br>

---

<br>

## 🔧 运行机制

<table>
<tr>
<td width="33%">

### 📁 本地存储
- **日志目录**
  `~/.lw_logs`
- **队列数据库**
  `~/.lw_logs/queue.db`
  (SQLite WAL)

</td>
<td width="33%">

### 📤 上传策略
- **批量上传**
  每 100 条或 5 秒触发
- **心跳上报**
  默认每 30 秒
- **失败重试**
  指数退避

</td>
<td width="33%">

### 🔍 健康检查
- **连通性探测**
  `GET /api/health`
- **离线模式**
  达到阈值后自动进入低频探测

</td>
</tr>
</table>

<br>

---

<br>

## 🔁 协议语义

- `task_id` 表示一次运行实例，客户端不会复用，服务端也不会接受复用
- 服务端删除任务后会保留 tombstone，旧客户端后续 `event`、`log`、`heartbeat`、`last-ack` 推送会收到 `409 task_deleted`
- 客户端收到 `task_deleted` 后停止该任务后续上报，并将本地队列标记为 `archived`
- 客户端收到 `task_not_running` 时不会立即放弃任务，而是按可重试异常处理
- 网络故障与任务删除是两类不同状态：前者允许恢复，后者不允许复活原任务

<br>

---

<div align="center">

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  Made with ⚡ by LogWatch Team                              │
│  📚 Documentation • 🐛 Issues • 💬 Discussions              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

<sub>© 2024 LogWatch • MIT License</sub>

</div>
