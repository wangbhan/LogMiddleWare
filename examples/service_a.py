"""
上游服务（port 8000）。
接收外部请求，生成根 trace_id，调用 service_b 时由 SDK 自动注入 traceparent 头。
无需手动调用 inject_trace_headers。
"""
import logging
import sys
import os

from examples.service import demo_a

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("service-a")
SanicTraceMiddleware(app, service_name="service-a")  # 自动开启 aiohttp 拦截
setup_trace_logging()

logger = logging.getLogger("service-a")


@app.get("/api/users")
async def get_users(request):
    logger.info("service-a: 收到 GET /api/users 请求，开始处理")

    # 直接使用 aiohttp，SDK 自动拦截并注入 traceparent 头
    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:8001/internal/data") as resp:
            data = await resp.json()

    logger.info("service-a: 已从 service-b 获取数据，准备返回响应")
    return sanic.response.json({"users": data})

@app.get("/api/test")
async def test(request):
    logger.info("service-a: 测试")
    return sanic.response.json({"message": "hello world"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=True, single_process=True)
