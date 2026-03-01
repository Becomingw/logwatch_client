# Changelog

## [0.1.3] - 2026-03-01

### Added
- 新增 `lw --setup` 命令用于交互式配置客户端
- 新增 `lw --health` 命令用于检查客户端健康状态
- 实现基于 SQLite 的本地持久化队列，提升日志上传可靠性
- 增强传输层状态管理，新增重试逻辑

### Changed
- 使用 requests 库替换 urllib 进行 HTTP 操作，提升性能和稳定性
- 改进任务通知邮件格式和内容

### Documentation
- 更新 README 布局和样式
- 完善文档结构

## [0.1.2] - 2025-02-06

### Fixed
- 修复多余的 f-string 前缀问题 (ruff F541)

### Added
- 添加离线邮箱功能
- 添加基础测试和 .gitignore

### CI/CD
- 集成 GitHub Actions
- 添加 Ruff 代码检查

## [0.1.1] - 2025-02-06

初始版本功能

## [0.1.0] - 2025-02-06

首次发布
