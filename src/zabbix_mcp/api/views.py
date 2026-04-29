#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""视图层工具：提供针对 LLM 优化的、更高层次的数据视图抽象。

这些函数封装常见的 Zabbix API 查询模式，过滤掉禁用的对象，
并返回对 AI 助手友好的结构化数据。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Annotated, Optional

if TYPE_CHECKING:
    from zabbix_mcp.client import ClientManager

logger = logging.getLogger("zabbix_mcp.views")

# 导入类型注解所需的类（用于动态签名生成）
try:
    from pydantic import Field
except ImportError:
    Field = None  # type: ignore

# Zabbix 严重程度映射
SEVERITY_NAMES: dict[str, str] = {
    "0": "未分类",
    "1": "信息",
    "2": "警告",
    "3": "一般严重",
    "4": "严重",
    "5": "灾难",
}


def _ts_to_str(ts: int | float, timezone_offset: int = 0) -> str:
    """将 Unix 时间戳转换为人类可读的 UTC 时间字符串。

    Args:
        ts: Unix 时间戳
        timezone_offset: 时区偏移量（小时），默认为 0（UTC）

    Returns:
        格式化的时间字符串，例如 "2026-04-28 17:30 UTC"
    """
    from datetime import timedelta
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    if timezone_offset != 0:
        dt = dt + timedelta(hours=timezone_offset)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _error_json(error: str) -> str:
    """返回标准化的 JSON 错误响应。"""
    return json.dumps({"error": error})


def problem_active_get(
    client_manager: ClientManager,
    server_name: str,
    **kwargs: Any,
) -> str:
    """获取最近的 Zabbix 活跃问题（过滤禁用的 trigger 和 host）。

    此工具专门用于获取真正需要关注的活跃问题，自动过滤掉：
    - 禁用的 trigger（status != 0）
    - 禁用的 host（status != 0）

    只返回严重程度 >= 2（警告及以上）的问题，并提供 LLM 友好的输出字段：
    - 主机名（host）
    - 告警内容（name, description）
    - 人类可读的时间（time）
    - 严重程度标签（severity_label）
    - 是否已确认（acknowledged）

    兼容 Zabbix 5 / 6 / 7，使用跨版本稳定的最小参数集发起单次 API 调用。

    Args:
        client_manager: ClientManager 实例
        server_name: 目标 Zabbix 服务器名称
        **kwargs: 额外参数
            - limit: 返回结果数量限制（默认 20）
            - sortfield: 排序字段（默认 "eventid"）
            - sortorder: 排序顺序（默认 "DESC"）

    Returns:
        包含活跃问题列表的 JSON 字符串

    示例:
        当 LLM 需要查看当前的活跃告警时，使用此工具而不是 problem.get，
        因为后者会返回包括已禁用主机和触发器的所有问题。
    """
    limit = kwargs.get("limit", 20)
    sortfield = kwargs.get("sortfield", "eventid")
    sortorder = kwargs.get("sortorder", "DESC")

    t0 = time.monotonic()
    logger.info(
        "[problem_active_get] 开始执行 server=%s limit=%s sortfield=%s sortorder=%s",
        server_name, limit, sortfield, sortorder,
    )

    # 仅使用 Zabbix 5/6/7 全部版本均支持的稳定参数，避免版本兼容性探测重试。
    # acknowledged/suppressed/real_time 存在版本差异，不在 API 侧过滤；
    # acknowledged 字段已包含在 output=extend 的返回结果中，由调用方或展示层处理。
    params = {
        "output": "extend",
        "severities": [2, 3, 4, 5],  # 警告及以上，Zabbix 4.x+ 均支持
        "sortfield": sortfield,
        "sortorder": sortorder,
        "limit": limit,
    }

    logger.info("[problem_active_get] 调用 problem.get，参数: %s", params)
    t1 = time.monotonic()
    try:
        problems = client_manager.call(server_name, "problem.get", params)
    except Exception as e:
        logger.exception(
            "[problem_active_get] problem.get 失败 (耗时 %.2fs)", time.monotonic() - t1
        )
        return _error_json(f"无法获取问题列表: {e}")
    logger.info(
        "[problem_active_get] problem.get 完成，返回 %d 条，耗时 %.2fs",
        len(problems) if problems else 0, time.monotonic() - t1,
    )

    if not problems:
        logger.info("[problem_active_get] 无活跃问题，总耗时 %.2fs", time.monotonic() - t0)
        return json.dumps({
            "problems": [],
            "count": 0,
            "message": "当前没有活跃问题"
        })

    # 收集所有 triggerid（problem 的 objectid 即 triggerid）
    trigger_ids = list(set(str(p["objectid"]) for p in problems if p.get("objectid")))
    logger.info(
        "[problem_active_get] 收集到 %d 个唯一 trigger_id（来自 %d 个问题）",
        len(trigger_ids), len(problems),
    )

    if not trigger_ids:
        logger.info("[problem_active_get] 无有效 trigger_id，总耗时 %.2fs", time.monotonic() - t0)
        return json.dumps({
            "problems": [],
            "count": 0,
            "message": "没有有效的 trigger ID"
        })

    # 批量查询 enabled triggers 及其关联主机
    logger.info(
        "[problem_active_get] 调用 trigger.get，triggerids 数量: %d", len(trigger_ids)
    )
    t2 = time.monotonic()
    try:
        triggers = client_manager.call(server_name, "trigger.get", {
            "output": ["triggerid", "description", "priority", "value", "status"],
            "triggerids": trigger_ids,
            "selectHosts": ["hostid", "host", "name", "status"],
            "filter": {"status": 0},  # 仅返回启用状态的 trigger（status=0）
            "monitored": True,        # 仅返回被监控的 trigger
            "active": True,           # 仅返回 active 的 trigger
        })
    except Exception as e:
        logger.exception(
            "[problem_active_get] trigger.get 失败 (耗时 %.2fs)", time.monotonic() - t2
        )
        return _error_json(f"无法获取 trigger 信息: {e}")
    logger.info(
        "[problem_active_get] trigger.get 完成，返回 %d 条，耗时 %.2fs",
        len(triggers) if triggers else 0, time.monotonic() - t2,
    )
    
    # 构建 triggerid -> 主机信息映射（只取第一个 enabled host）
    trigger_host_map: dict[str, str] = {}       # triggerid -> host.name
    trigger_hostid_map: dict[str, str] = {}     # triggerid -> hostid
    
    for t in triggers:
        # 过滤出 enabled 的主机（status=0）
        active_hosts = [h for h in t.get("hosts", []) if int(h.get("status", 1)) == 0]
        if active_hosts:
            host = active_hosts[0]
            trigger_host_map[t["triggerid"]] = host.get("name") or host.get("host", "")
            trigger_hostid_map[t["triggerid"]] = host.get("hostid", "")
    
    # 过滤并组装结果：只保留有 enabled host 的问题，附加 LLM 友好字段
    filtered = []
    for p in problems:
        trigger_id = str(p.get("objectid", ""))
        if trigger_id not in trigger_host_map:
            continue
        
        clock = p.get("clock", "")
        severity_val = str(p.get("severity", "0"))
        
        filtered.append({
            "eventid": p["eventid"],
            "triggerid": trigger_id,
            # LLM 关键字段：主机名
            "host": trigger_host_map[trigger_id],
            "hostid": trigger_hostid_map.get(trigger_id, ""),
            # LLM 关键字段：告警内容
            "name": p.get("name", ""),
            "description": p.get("description", ""),
            # LLM 关键字段：严重程度（标签 + 原始值）
            "severity": severity_val,
            "severity_label": SEVERITY_NAMES.get(severity_val, f"未知({severity_val})"),
            # LLM 关键字段：告警时间（人类可读 + 原始时间戳）
            "clock": clock,
            "time": _ts_to_str(clock, timezone_offset=0),
            # 确认状态（精简：只保留计数，不传完整 acknowledges 数组）
            "acknowledged": int(p.get("acknowledged", 0)),
            "ack_count": len(p.get("acknowledges", [])),
        })
    
    logger.info(
        "[problem_active_get] 完成：filtered=%d filtered_out=%d，总耗时 %.2fs",
        len(filtered), len(problems) - len(filtered), time.monotonic() - t0,
    )
    return json.dumps({
        "problems": filtered,
        "count": len(filtered),
        "filtered_out": len(problems) - len(filtered),
    }, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Views 工具注册系统（类似 MethodDef，但用于自定义 view 函数）
# ---------------------------------------------------------------------------

@dataclass
class ViewParam:
    """View 工具的参数定义（类似 ParamDef）。"""
    name: str
    param_type: str
    description: str
    required: bool = False
    default: Any = None


@dataclass
class ViewToolDef:
    """View 工具的定义（类似 MethodDef）。"""
    tool_name: str
    description: str
    handler: Any  # 实际的处理函数
    params: list[ViewParam]
    tool_prefix: str = "problem"  # 用于权限检查的前缀


# Views 工具注册表 — 在此处添加新工具，无需修改 server.py
VIEWS_TOOLS: list[ViewToolDef] = [
    ViewToolDef(
        tool_name="problem_active_get",
        description=(
            "获取活跃的 Zabbix 问题（仅包含启用的 trigger 和 host）。\n\n"
            "与 problem_get 不同，此工具自动过滤掉禁用的触发器和主机，只返回真正"
            "需要关注的活跃问题（严重程度 >= 警告）。返回的字段包括：主机名、告警内容、"
            "人类可读的时间、严重程度标签等，特别适合 AI 助手直接呈现给用户。\n\n"
            "使用场景：\n"
            "- 查看当前需要处理的活跃告警\n"
            "- 生成监控报告或摘要\n"
            "- 筛选真正重要的问题（排除测试/禁用的主机）"
        ),
        handler=problem_active_get,
        params=[
            ViewParam("limit", "int", "返回结果数量限制（默认 20）", default=20),
            ViewParam("sortfield", "str", "排序字段（默认 'eventid'）", default="eventid"),
            ViewParam("sortorder", "str", "排序顺序：'ASC' 或 'DESC'（默认 'DESC'）", default="DESC"),
        ],
        tool_prefix="problem",
    ),
]


def make_view_handler(
    view_def: ViewToolDef,
    client_manager: ClientManager,
    server_names: list[str],
) -> Any:
    """为 view 函数创建 MCP tool handler（类似 _make_tool_handler）。
    
    此工厂函数为每个 view 工具生成一个带有正确类型签名的 async handler，
    使 FastMCP 能够自动生成 JSON Schema。
    
    Args:
        view_def: View 工具定义
        client_manager: ClientManager 实例
        server_names: 可用的 Zabbix 服务器名称列表
    
    Returns:
        带有正确签名和文档的 async handler 函数
    """
    # 构建实际的处理函数
    async def handler(**kwargs: Any) -> str:
        server_name = kwargs.get("server") or client_manager.default_server
        if not server_name:
            return json.dumps({
                "error": True,
                "message": "No Zabbix server configured.",
                "type": "ConfigurationError"
            })
        
        try:
            server_name = client_manager.resolve_server(server_name)
            
            # 检查 token 授权（服务器、作用域、read_only）
            from zabbix_mcp.token_store import check_token_authorization
            _auth_err = check_token_authorization(
                server_name,
                tool_prefix=view_def.tool_prefix,
                is_write=False  # views 工具都是只读的
            )
            if _auth_err:
                return json.dumps({
                    "error": True,
                    "message": _auth_err,
                    "type": "AuthorizationError"
                })
            
            # 过滤掉 server 参数，传递给实际的 view 函数
            view_kwargs = {k: v for k, v in kwargs.items() if k != "server"}
            
            # 调用实际的 view 处理函数
            result = await asyncio.to_thread(
                view_def.handler,
                client_manager,
                server_name,
                **view_kwargs
            )
            return result
            
        except Exception as e:
            logger.exception("Error in view tool '%s' on server '%s'", view_def.tool_name, server_name)
            return json.dumps({
                "error": True,
                "message": f"View tool failed: {e}",
                "type": "ViewError"
            })
    
    # 构建动态函数签名，使 FastMCP 能生成正确的 JSON Schema
    sig_params: list[inspect.Parameter] = []
    
    # 添加 server 参数
    server_desc = (
        f"Target Zabbix server. Available: {', '.join(server_names)}. "
        f"Defaults to '{server_names[0]}' if omitted."
    )
    
    if Field is not None:
        sig_params.append(inspect.Parameter(
            "server",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Annotated[Optional[str], Field(description=server_desc)],
        ))
    
    # 添加 view 特定的参数
    _PYTHON_TYPES = {
        "str": str,
        "int": int,
        "bool": bool,
        "list[str]": list[str],
        "list": list,
        "dict": dict,
    }
    
    for param in view_def.params:
        python_type = _PYTHON_TYPES.get(param.param_type, str)
        if param.required:
            if Field is not None:
                annotation = Annotated[python_type, Field(description=param.description)]
            else:
                annotation = python_type
            default = inspect.Parameter.empty
        else:
            if Field is not None:
                annotation = Annotated[Optional[python_type], Field(description=param.description)]
            else:
                annotation = Optional[python_type]
            default = param.default
        
        sig_params.append(inspect.Parameter(
            param.name,
            inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=annotation,
        ))
    
    # 设置函数签名和元数据
    handler.__signature__ = inspect.Signature(sig_params, return_annotation=str)  # type: ignore
    handler.__name__ = view_def.tool_name
    handler.__doc__ = view_def.description
    handler.__qualname__ = view_def.tool_name
    
    return handler
