# AI 协作手册（Codex / Claude Code）

本文档用于让 AI 在 LogWatch 项目中稳定完成：客户端安装与配置。

---

## 1. 用户准备工作

用户需要提前准备以下信息：

- **服务端地址**：例如 `http://127.0.0.1:8000`
- **用户鉴权**：
  - `user_id`（数字ID）
  - `user_token`（在网页设置中创建，格式：`ut_xxx`）

**安全建议**：建议用户将 token 放在环境变量.bashrc中，避免明文出现在对话记录：

```bash
export LW_USER_ID=用户提供的user_id
export LW_USER_TOKEN=ut_xxx
```
执行 source ~/.bashrc
---

## 2. AI 执行指南

### 2.1 安装步骤

**优先级顺序**：

1. 尝试 `uv tool install logwatch-client`
2. 如果失败，使用 `pip install logwatch-client`
3. 验证安装：`lw --version` 或 `which lw`

**错误处理**：

- 如果 `uv` 不存在，直接使用 `pip`
- 如果 `pip` 也失败，检查 Python 环境并提示用户
- 如果权限不足，建议使用 `--user` 标志

### 3.2 配置步骤

运行交互式配置：

```bash
lw --setup
```

**配置项说明**：

- `server`：用户提供的服务端地址（必须包含协议，如 `http://`）
- `user_id`：用户提供的数字ID
- `user_token`：用户提供的token（格式：`ut_xxx`）
- `machine`：自动获取当前主机名，或让用户自定义

**配置文件位置**：`~/.lwconfig`

**验证配置**：

```bash
cat ~/.lwconfig | grep -E "^(server|user_id|machine)="
```

### 3.3 健康检查

运行健康检查：

```bash
lw --health
```

**预期输出**：

- ✅ **成功**：`health result=PASS`
- ❌ **失败**：会显示具体错误原因

**常见错误及解决方案**：

| 错误信息 | 原因 | 解决方案 |
| -------- | ---- | -------- |
| `Connection refused` | 服务端未启动或地址错误 | 检查服务端地址和端口 |
| `401 Unauthorized` | user_id 或 token 错误 | 重新检查鉴权信息 |
| `404 Not Found` | API 路径错误 | 检查服务端版本 |
| `Timeout` | 网络问题 | 检查防火墙和网络连接 |

### 3.4 邮件通知配置（可选）

如果用户需要配置邮件通知功能，需要在 `~/.lwconfig` 中添加以下配置：

**基础邮件配置**：

```ini
email_enabled=true
email_notify_on=all              # 可选值: all, failure, success
email_notify_on_start=false      # 是否在任务开始时发送通知
```

**SMTP 服务器配置**：

```ini
smtp_host=smtp.example.com       # SMTP 服务器地址
smtp_port=465                    # SMTP 端口（465 for SSL, 587 for TLS）
smtp_user=your-email@example.com # SMTP 用户名
smtp_pass=your-password          # SMTP 密码或授权码
smtp_use_tls=true                # 是否使用 TLS
```

**发件人和收件人**：

```ini
email_from=your-email@example.com    # 发件人地址
email_to=notify@example.com          # 收件人地址
```

**常见邮箱配置示例**：

| 邮箱服务 | SMTP 地址 | 端口 | 说明 |
| -------- | --------- | ---- | ---- |
| Gmail | smtp.gmail.com | 587 | 需要开启"应用专用密码" |
| QQ 邮箱 | smtp.qq.com | 465/587 | 需要开启 SMTP 服务并获取授权码 |
| 163 邮箱 | smtp.163.com | 465/994 | 需要开启 SMTP 服务并获取授权码 |
| Outlook | smtp-mail.outlook.com | 587 | 使用账号密码 |
| 阿里云邮箱 | smtp.aliyun.com | 465 | 使用账号密码 |

**AI 配置邮件通知时的注意事项**：

1. **安全性**：
   - ❌ 不要在输出中显示完整的 `smtp_pass`
   - ✅ 使用 `smtp_pass=***` 脱敏显示
   - 建议用户使用邮箱的"授权码"或"应用专用密码"，而非账号密码

2. **验证配置**：
   - 配置完成后，运行 `lw --health` 会测试邮件配置
   - 如果邮件配置有误，健康检查会给出具体错误信息

3. **常见错误**：
   - `Authentication failed`：用户名或密码错误，检查是否使用授权码
   - `Connection refused`：SMTP 服务器地址或端口错误
   - `SSL/TLS error`：检查 `smtp_use_tls` 设置是否与端口匹配

### 3.5 生成测试命令

提供一条可执行的测试命令，例如：

```bash
lw --name "ai-setup-test" python -c "import time; [print(f'Line {i}') or time.sleep(0.1) for i in range(20)]"
```

**命令要求**：

- 使用 `--name` 指定任务名称
- 命令应该能快速执行（10-30秒）
- 输出应该有多行，便于验证日志上传
- 不要在命令中硬编码 `--user-id` 和 `--user-token`（应该从配置文件读取）

---

## 4. AI 输出格式要求

### 4.1 安全要求

**严格禁止**：

- ❌ 在输出中显示完整的 `user_token`
- ❌ 在日志中记录完整的 `user_token`
- ❌ 在输出中显示完整的 `smtp_pass`（邮箱密码/授权码）

**允许的显示方式**：

- ✅ `user_token=ut_***` （脱敏显示）
- ✅ `user_token=<已配置>` （状态显示）
- ✅ `smtp_pass=***` （脱敏显示）
- ✅ 在配置文件验证时使用 `grep -v user_token` 或 `grep -v smtp_pass`

### 4.2 输出结构

建议 AI 按以下结构输出：

```text
1. ✅ 安装完成
   - 使用方式：uv / pip
   - 版本：x.x.x

2. ✅ 配置完成
   - 配置文件：~/.lwconfig
   - server: http://127.0.0.1:8000
   - user_id: 104698
   - machine: macbook-pro
   - email_enabled: true/false

3. ✅ 健康检查
   - 结果：PASS / FAIL
   - 连通性：正常
   - 邮件配置：正常/未配置/配置错误
   - 说明：[解释结果]

4. 📝 测试命令
   ```bash
   lw --name "test-task" python -c "..."
   ```
```

### 4.3 错误报告

如果任何步骤失败，AI 应该：

1. 明确指出哪一步失败
2. 提供完整的错误信息
3. 给出可能的解决方案
4. 询问用户是否需要手动干预

---

## 5. 验证清单

AI 完成后，用户应该能够：

- [ ] 运行 `lw --version` 查看版本
- [ ] 查看 `~/.lwconfig` 包含所有必需配置
- [ ] 运行 `lw --health` 显示 `PASS`
- [ ] 执行测试命令并在服务端看到任务
- [ ] 在服务端看到实时日志输出
- [ ] （如果配置了邮件）收到任务通知邮件

---

## 6. 常见问题

### Q1: 如果用户没有提供 token 怎么办？
A: 提示用户在服务端网页的"设置"页面创建 token，并建议使用环境变量存储。

### Q2: 如果 `lw --setup` 交互式输入不方便怎么办？
A: 可以直接编辑 `~/.lwconfig` 文件，或使用环境变量。

### Q3: 如果健康检查失败怎么办？
A: 按照错误信息排查，常见问题包括：服务端未启动、网络不通、鉴权错误。

### Q4: 如何卸载？
A: `uv tool uninstall logwatch-client` 或 `pip uninstall logwatch-client`

### Q5: 如何配置邮件通知？

A: 在 `~/.lwconfig` 中添加邮件相关配置项。需要注意：

1. 大多数邮箱需要开启 SMTP 服务并使用"授权码"而非登录密码
2. Gmail 需要开启"两步验证"并创建"应用专用密码"
3. QQ/163 邮箱需要在设置中开启 SMTP 服务并获取授权码
4. 配置完成后运行 `lw --health` 验证邮件配置是否正确

### Q6: 邮件通知不工作怎么办？

A: 常见问题排查：

1. 检查是否使用了授权码而非登录密码
2. 确认 SMTP 端口和 TLS 设置匹配（465 通常需要 TLS，587 可选）
3. 检查邮箱是否开启了 SMTP 服务
4. 运行 `lw --health` 查看具体错误信息
5. 检查防火墙是否阻止了 SMTP 端口

### Q7: 其他注意事项？

A:

1. 尽量使用 uv 进行安装，可以先使用 pip 配合清华源安装 uv 后再安装 logwatch-client
2. 如果配置邮件通知，建议先用测试任务验证邮件是否能正常发送
3. 邮件密码建议使用环境变量存储，避免明文保存在配置文件中

---

## 7. 高级场景

### 7.1 多机器配置

如果用户需要在多台机器上配置，建议：

1. 为每台机器使用不同的 `machine` 名称
2. 可以共用同一个 `user_id` 和 `user_token`
3. 使用脚本批量部署

### 7.2 CI/CD 集成

在 CI/CD 环境中：

1. 使用环境变量传递鉴权信息
2. 使用 `--no-check` 跳过启动前检查（如果网络不稳定）
3. 使用 `--name` 指定任务名称（如：`build-${CI_JOB_ID}`）
4. 建议禁用邮件通知或使用专门的通知邮箱

### 7.3 邮件通知最佳实践

1. **开发环境**：建议禁用邮件通知（`email_enabled=false`）
2. **生产环境**：
   - 使用 `email_notify_on=failure` 只在失败时通知
   - 设置 `email_notify_on_start=false` 避免过多邮件
   - 使用专门的通知邮箱账号
3. **安全性**：
   - 使用授权码而非账号密码
   - 考虑使用环境变量存储 SMTP 密码
   - 定期更换授权码

---

## 8. 参考资源

- 完整文档：[client/README.md](../README.md)
- PyPI 页面：https://pypi.org/becomingw/logwatch-client/
- 问题反馈：项目 Issues 页面


