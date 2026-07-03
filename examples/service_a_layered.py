"""
分层架构示例（port 8000）：controller层 → service层 → repository层 → 调用 service_b

验证要点：
  1. 三层的日志 trace_id 完全相同 —— SDK 自动透传，无需任何额外配置
  2. 三层的 span_id 也相同（都指向 Sanic 中间件创建的 SERVER span）
  3. service_b 的 trace_id 与本服务相同（跨服务链路正常）
  4. aiohttp 出站请求自动注入 traceparent，无需手动调用 inject_trace_headers

运行方式：
  终端 1: .venv/bin/python examples/service_b.py
  终端 2: .venv/bin/python examples/service_a_layered.py
  终端 3: curl http://localhost:8000/api/users
"""
import aiohttp
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging, get_tracer

app = Sanic("service-a-layered")
SanicTraceMiddleware(app, service_name="service-a-layered")
setup_trace_logging()

# 各层使用独立的 logger name，方便区分日志来源，trace_id 仍然相同
repo_logger = logging.getLogger("service-a.repository")
svc_logger = logging.getLogger("service-a.service")
ctrl_logger = logging.getLogger("service-a.controller")

# 可选：通过 get_tracer() 在各层创建子 span（增加链路粒度）
tracer = get_tracer(__name__)


# ─── Repository 层 ────────────────────────────────────────────────────────────
class UserRepository:
    async def find_all(self):
        repo_logger.info("[repo] 开始查询本地用户数据")
        users = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
        repo_logger.info(f"[repo] 查询完成，共 {len(users)} 条记录")
        return users


# ─── Service 层 ───────────────────────────────────────────────────────────────
class UserService:
    def __init__(self):
        self._repo = UserRepository()

    async def get_users(self):
        svc_logger.info("[service] 开始聚合用户数据")

        # 调用本地 repository 层
        local_users = await self._repo.find_all()

        # 调用下游 service_b（aiohttp 已被 SDK 自动插桩，自动注入 traceparent）
        async with aiohttp.ClientSession() as session:
            async with session.get("http://localhost:8001/internal/data") as resp:
                remote_data = await resp.json()

        remote_items = remote_data.get("items", [])
        svc_logger.info(
            f"[service] 数据聚合完成：本地 {len(local_users)} 条，远端 {len(remote_items)} 条"
        )
        return {"local_users": local_users, "remote_items": remote_items}


# ─── Controller 层（Sanic 路由 handler）──────────────────────────────────────
_service = UserService()


@app.get("/api/users")
async def get_users(request):
    ctrl_logger.info("[controller] 收到 GET /api/users 请求")
    result = await _service.get_users()
    ctrl_logger.info("[controller] 响应准备完成，返回给客户端")
    return sanic.response.json(result)


# ─── 可选演示：各层使用子 Span 增加链路粒度 ──────────────────────────────────
# 取消注释以下代码，可以看到每层有独立的 span_id（但 trace_id 仍相同）
#
# class UserServiceWithSpans:
#     def __init__(self):
#         self._repo = UserRepository()
#
#     async def get_users(self):
#         with tracer.start_as_current_span("UserService.get_users") as span:
#             svc_logger.info("[service+span] 开始聚合（span_id 是子 span）")
#             local_users = await self._repo.find_all()
#             span.set_attribute("local_user_count", len(local_users))
#             ...
#             return ...


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True, single_process=True)
