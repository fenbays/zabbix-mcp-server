# Token-Based Dynamic Tools Filtering

## 功能概述

从 v1.x 开始，zabbix-mcp-server 支持基于 MCP token 的动态工具过滤。不同权限的 MCP 客户端会看到不同的工具列表，实现单实例多租户的细粒度权限控制。

## 工作原理

### 架构

```
MCP Client A (monitoring)
    ↓ (connects with token_monitoring)
    ↓ tools/list request
    ↓
[_TokenAwareToolsFilterMiddleware]
    ↓ (filters by token scopes)
    ↓ 
Response: [host_get, problem_get, ...]  (只包含 monitoring 工具)


MCP Client B (alerts)
    ↓ (connects with token_alerts)
    ↓ tools/list request
    ↓
[_TokenAwareToolsFilterMiddleware]
    ↓ (filters by token scopes)
    ↓
Response: [action_get, mediatype_get, ...]  (只包含 alerts 工具)
```

### 过滤时机

- ✅ **tools/list 请求**：在响应时根据 token scopes 动态过滤
- ❌ **工具调用请求**：直接透传，零开销（仍然会在执行时验证权限）

### 性能特性

| 请求类型 | 是否拦截 | 性能影响 |
|---------|---------|---------|
| tools/list | ✅ 是 | +3-5ms (低频操作，可忽略) |
| 工具调用 (host_get, etc.) | ❌ 否 | 0ms (零开销透传) |
| 其他 HTTP 请求 | ❌ 否 | 0ms (快速路径) |

## 配置示例

### 场景1：监控只读 + 告警管理

```toml
# config.toml

[server]
port = 8080

[zabbix.production]
url = "https://zabbix.example.com"
api_token = "${ZABBIX_TOKEN}"
read_only = false

# 监控只读 token（只能查看，不能操作）
[tokens.monitoring_readonly]
name = "Monitoring Dashboard"
token_hash = "sha256:abc123..."
scopes = ["monitoring"]
read_only = true
allowed_ips = ["10.0.1.0/24"]

# 告警管理 token（可以创建/修改告警规则）
[tokens.alerts_manager]
name = "Alert Manager"
token_hash = "sha256:def456..."
scopes = ["alerts"]
read_only = false
allowed_servers = ["production"]

# 全局管理员 token
[tokens.admin]
name = "Admin Full Access"
token_hash = "sha256:xyz789..."
scopes = ["*"]
read_only = false
```

### 客户端看到的工具列表

#### monitoring_readonly 连接后
```json
{
  "tools": [
    {"name": "host_get", ...},
    {"name": "problem_get", ...},
    {"name": "trigger_get", ...},
    {"name": "item_get", ...},
    {"name": "event_get", ...},
    // ... 其他 monitoring 组工具
  ]
}
```

**不可见的工具：**
- ❌ `action_get`, `mediatype_get` (alerts 组)
- ❌ `user_get`, `role_get` (users 组)
- ❌ `configuration_import`, `template_delete` (administration 组)

#### alerts_manager 连接后
```json
{
  "tools": [
    {"name": "action_get", ...},
    {"name": "action_create", ...},
    {"name": "action_update", ...},
    {"name": "mediatype_get", ...},
    {"name": "script_execute", ...},
    // ... 其他 alerts 组工具
  ]
}
```

**不可见的工具：**
- ❌ `host_get`, `problem_get` (monitoring 组)
- ❌ `user_get`, `template_get` (其他组)

#### admin 连接后
```json
{
  "tools": [
    // 所有已注册的工具 (~225 个)
  ]
}
```

## 使用场景

### 1. 多团队共享单实例

```toml
# 运维团队：全权限
[tokens.ops_team]
scopes = ["*"]
read_only = false

# 开发团队：只读监控
[tokens.dev_team]
scopes = ["monitoring", "data_collection"]
read_only = true

# 安全团队：审计和用户管理
[tokens.security_team]
scopes = ["users", "administration"]
read_only = false
```

### 2. CI/CD 集成

```toml
# CI pipeline：自动化测试和配置导入
[tokens.ci_pipeline]
scopes = ["monitoring", "administration"]
read_only = false
allowed_ips = ["172.16.0.0/12"]  # CI 服务器 IP 段
expires_at = "2026-12-31T23:59:59Z"

# CD deployment：只读验证
[tokens.cd_verify]
scopes = ["monitoring"]
read_only = true
```

### 3. 外部集成服务

```toml
# Grafana：只读数据查询
[tokens.grafana]
scopes = ["monitoring", "data_collection"]
read_only = true
allowed_ips = ["10.0.2.100"]

# PagerDuty：告警管理
[tokens.pagerduty]
scopes = ["alerts"]
read_only = false
```

## 工具分组参考

### monitoring (82 tools)
- host_*, hostgroup_*, hostinterface_*
- item_*, trigger_*, problem_*
- graph_*, event_*, history_*, trend_*
- sla_*, httptest_*, drule_*, ...

### data_collection (27 tools)
- template_*, templatedashboard_*
- templategroup_*
- valuemap_*, dashboard_*

### alerts (16 tools)
- action_*, alert_*
- mediatype_*, script_*

### users (39 tools)
- user_*, usergroup_*, userdirectory_*
- token_*, role_*, mfa_*
- usermacro_*

### administration (59 tools)
- configuration_*, settings_*
- maintenance_*, proxy_*, proxygroup_*
- authentication_*, housekeeping_*
- auditlog_*, report_*, task_*

### extensions (8 tools)
- zabbix_raw_api_call
- graph_render
- anomaly_detect
- capacity_forecast
- report_generate
- action_prepare / action_confirm
- health_check

## 安全性说明

### 多层防护

1. **工具列表过滤**（UX 层）
   - 隐藏不可访问的工具
   - 提升用户体验
   - 减少误操作

2. **执行时权限验证**（安全层）
   - 每次工具调用都验证 token scopes
   - 即使客户端绕过过滤直接调用，也会被拒绝
   - 记录所有未授权尝试

3. **Zabbix API 权限**（最终防线）
   - Zabbix User Roles 控制方法级权限
   - API token 的用户权限限制

### 不要依赖单一防护

```toml
# ❌ 错误：只依赖工具过滤
[tokens.limited]
scopes = ["monitoring"]
# 如果客户端绕过过滤，仍然可能调用其他工具

# ✅ 正确：结合多层防护
[tokens.limited]
scopes = ["monitoring"]           # 1. 工具列表过滤
read_only = true                  # 2. 执行时写操作拦截
allowed_servers = ["production"]  # 3. 服务器访问限制
allowed_ips = ["10.0.0.0/8"]     # 4. IP 白名单
# + Zabbix User Roles             # 5. API 方法级权限
```

## 调试和监控

### 启用调试日志

```toml
[server]
log_level = "debug"
log_file = "/var/log/zabbix-mcp/server.log"
```

日志示例：
```
2026-04-27 14:30:15 [INFO] Token-aware tools filtering enabled (3 tokens configured)
2026-04-27 14:30:20 [DEBUG] Filtered tools for token 'monitoring_readonly': 82/225 tools visible (scopes: monitoring)
2026-04-27 14:30:25 [DEBUG] Filtered tools for token 'alerts_manager': 16/225 tools visible (scopes: alerts)
```

### 验证过滤结果

使用 Claude Desktop 或其他 MCP 客户端：

```bash
# 查看可用工具列表
claude mcp list

# 使用特定 token 连接
# 在 Claude Desktop config 中指定不同的 token
```

### 性能监控

中间件在日志中记录处理时间（仅当超过 10ms 时）：

```
2026-04-27 14:30:15 [DEBUG] Tools filter middleware took 12.3ms
```

正常情况下应 < 5ms。如果持续 > 10ms，可能需要检查：
- 工具数量是否过多（> 500）
- JSON 响应大小是否异常
- 服务器负载是否过高

## 故障排查

### 问题1：客户端看到所有工具

**原因：** Token scopes 包含 "*"

**解决：**
```toml
# 错误
[tokens.my_token]
scopes = ["*"]

# 正确
[tokens.my_token]
scopes = ["monitoring", "alerts"]
```

### 问题2：客户端看不到预期的工具

**检查清单：**
1. Token scopes 是否包含对应的分组？
2. 工具是否在 [server].tools 白名单中？
3. 工具是否在 [server].disabled_tools 黑名单中？
4. 是否启用了全局 read_only 过滤掉写操作工具？

**调试步骤：**
```bash
# 1. 检查 token 配置
grep -A 5 "tokens.my_token" /etc/zabbix-mcp/config.toml

# 2. 查看日志中的过滤记录
grep "Filtered tools for token" /var/log/zabbix-mcp/server.log

# 3. 测试 token scopes 展开
python3 -c "
from zabbix_mcp.config import _expand_tool_groups
print(_expand_tool_groups(['monitoring']))
"
```

### 问题3：性能下降

**症状：** tools/list 响应缓慢

**可能原因：**
- 工具数量过多（> 500）
- 同时大量新连接

**解决方案：**
```toml
# 减少注册的工具数量
[server]
tools = ["monitoring", "alerts"]  # 只注册需要的分组
disabled_tools = ["extensions"]   # 禁用不必要的工具
```

## 更新和维护

### 添加新 token

1. 通过 admin portal（推荐）
2. 或手动编辑 config.toml：

```toml
[tokens.new_token]
name = "New Service"
token_hash = "sha256:..."  # 通过 admin portal 生成
scopes = ["monitoring"]
read_only = true
```

3. 重启服务（未来版本将支持热重载）

### 修改 token scopes

```bash
# 1. 编辑配置
vim /etc/zabbix-mcp/config.toml

# 2. 重启服务
systemctl restart zabbix-mcp-server

# 3. 验证
grep "Token-aware tools filtering enabled" /var/log/zabbix-mcp/server.log
```

## 限制和注意事项

1. **stdio 传输模式不支持**
   - 仅在 HTTP/SSE 传输模式下工作
   - stdio 模式下没有 token 认证，因此无法过滤

2. **需要配置 [tokens.*]**
   - 使用传统 auth_token 时不启用过滤
   - 必须配置至少一个 [tokens.*] section

3. **工具调用权限仍需验证**
   - 过滤只是 UX 改进，不是安全措施
   - 始终在工具执行时验证权限

4. **性能考虑**
   - 大量工具（> 500）时可能略微影响 tools/list 性能
   - 通过 tools/disabled_tools 减少注册数量

## 参考资料

- [MCP Token 配置指南](./TOKENS.md)
- [权限分层设计](./SECURITY.md)
- [性能优化](./PERFORMANCE.md)
- [故障排查](./TROUBLESHOOTING.md)
