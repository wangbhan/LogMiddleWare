"""
上游服务（port 8000）。
接收外部请求，生成根 trace_id，调用 service_b 时通过 traceparent 头传播链路上下文。
"""
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging, inject_trace_headers

app = Sanic("service-a")
SanicTraceMiddleware(app, service_name="service-a")
setup_trace_logging()

logger = logging.getLogger("service-a")


@app.get("/api/users")
async def get_users(request):
    logger.info("service-a: 收到 GET /api/users 请求，开始处理")

    # 注入当前 span 的 traceparent 到请求头，传播给 service_b
    headers = inject_trace_headers({})

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "http://localhost:8001/internal/data",
            headers=headers,
        ) as resp:
            data = await resp.json()

    logger.info("service-a: 已从 service-b 获取数据，准备返回响应")
    return sanic.response.json({"users": data})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=True, single_process=True)
