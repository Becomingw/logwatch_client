# LogWatch Client (lw)

LogWatch 客户端 - 包裹命令并上传日志到监控服务器的命令行工具。

## 🚀 安装

### 使用 uv (推荐)

```bash
uv tool install logwatch-client
```

### 使用 pip

```bash
pip install logwatch-client
```

### 从源码安装

```bash
cd client
uv pip install -e .
```

## 📖 使用方式

### 基本使用

```bash
# 包裹命令执行，自动上传日志
lw python train.py

# 指定任务名称
lw --name "resnet-v2-training" python train.py

# 指定服务器
lw --server http://your-server.com:8000 python train.py
```

### 配置文件

首次使用前，运行 `--init` 生成配置模板：

```bash
lw --init
```

配置文件位于 `~/.lwconfig`：

```ini
# LogWatch 客户端配置
# 服务器地址（必填）
server=http://your-server.com:8000

# 机器标识（可选，默认使用 hostname）
machine=my-gpu-server

# 用户 ID（用于鉴权/多用户隔离）
user_id=alice
```

### 命令行参数

| 参数 | 说明 |
|------|------|
| `--name`, `-n` | 任务名称（默认自动生成） |
| `--server`, `-s` | 服务器地址（默认读取 ~/.lwconfig） |
| `--machine`, `-m` | 机器标识（默认使用 hostname） |
| `--user-id`, `-u` | 用户 ID（默认读取 ~/.lwconfig） |
| `--init` | 生成配置文件模板 |
| `--no-check` | 跳过服务器连通性检查 |

## ✨ 特性

- **零依赖**：仅使用 Python 标准库
- **实时上传**：每 2 秒增量上传日志
- **心跳监测**：自动发送心跳，服务端可检测任务存活状态
- **失败重试**：网络波动时自动重试，支持熔断保护
- **离线模式**：服务器不可达时可选择离线模式，仅记录本地日志
- **跨平台**：支持 Linux 和 macOS

## 📋 系统要求

- Python 3.8+
- Linux 或 macOS（使用 PTY 捕获输出）

## 📄 License

MIT License
