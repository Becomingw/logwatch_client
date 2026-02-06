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

<br/>

---

### 快速开始

**uv (推荐)**&emsp;&emsp;`uv tool install logwatch-client`

**pip**&emsp;&emsp;`pip install logwatch-client`

<details>
<summary>从源码安装</summary>

<br/>

`cd logwatch_client && uv pip install -e .`

</details>

<br/>

---

### 使用方式

</div>

```bash
lw python train.py                                      # 基础用法
lw --name "resnet-v2" python train.py                   # 指定任务名称
lw --server http://your-server.com:8000 python train.py # 指定服务器
lw --offline python train.py                            # 离线模式
```

<div align="center">

<br/>

---

### 核心特性

**零依赖** · 仅使用 Python 标准库
<br/>
**实时上传** · 每 2 秒增量同步日志
<br/>
**离线模式** · 本地记录 + 邮件通知
<br/>
**熔断保护** · 网络波动自动重试

<br/>

---

### 系统要求

**Python** 3.8+&emsp;•&emsp;**OS** Linux / macOS

<br/>

---

### 配置

运行 `lw --init` 生成配置文件 `~/.lwconfig`

</div>

```ini
server=http://your-server.com:8000   # 服务器地址（必填）
machine=my-gpu-server                # 机器标识
user_id=alice                        # 用户 ID
```

<details>
<summary><b>完整配置项</b></summary>

```ini
upload_interval_seconds=2            # 上传间隔（秒）
publish_grace_seconds=1              # 发布前等待窗口
log_retention_days=7                 # 本地日志保留天数
log_max_files=1000                   # 本地日志最大文件数
upload_gzip_min_kb=64                # gzip 压缩阈值（KB）
upload_retry_times=3                 # 上传失败重试次数
upload_retry_interval_seconds=2      # 重试间隔（秒）
upload_circuit_break_minutes=5       # 熔断时长（分钟）
upload_circuit_break_max=3           # 熔断次数阈值
force_offline=false                  # 强制离线模式
```

</details>

<div align="center">

<br/>

---

### 邮件通知

离线模式下任务完成后通过邮件发送通知，支持 HTML 格式

</div>

```ini
email_enabled=true                   # 启用邮件通知
email_notify_on=all                  # all / failed / success
smtp_host=smtp.example.com           # SMTP 服务器
smtp_port=465                        # 端口
smtp_user=your-email@example.com     # 用户名
smtp_pass=your-password              # 密码或授权码
smtp_use_tls=true                    # 使用 TLS
email_from=your-email@example.com    # 发件人
email_to=notify@example.com          # 收件人
```

<details>
<summary><b>常见邮箱配置</b></summary>

**QQ** `smtp.qq.com:465` · **163** `smtp.163.com:465` · **Gmail** `smtp.gmail.com:587`

</details>

<div align="center">

<br/>

---

### 命令行参数

</div>

| 参数 | 简写 | 说明 |
|:-----|:----:|:-----|
| `--name` | `-n` | 任务名称 |
| `--server` | `-s` | 服务器地址 |
| `--machine` | `-m` | 机器标识 |
| `--user-id` | `-u` | 用户 ID |
| `--init` | - | 生成配置文件模板 |
| `--no-check` | - | 跳过服务器连通性检查 |
| `--offline` | - | 强制离线模式 |

<div align="center">

<br/>

---

<sub>Made with ☕ by BecomingW with Claude❤️</sub>

[![MIT License](https://img.shields.io/badge/License-MIT-gray?style=flat-square)](LICENSE)

</div>
