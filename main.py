"""
快速演示：OpenTelemetry Sanic 日志中间件 SDK

运行多服务全链路追踪示例：
    # 终端 1 — 下游服务（先启动）
    .venv/bin/python examples/service_b.py

    # 终端 2 — 上游服务
    .venv/bin/python examples/service_a.py

    # 终端 3 — 发起请求
    curl http://localhost:8000/api/users

验证：两个服务的日志 trace_id 应完全相同，service_b 的 parent_span_id 等于 service_a 的 span_id。
"""
import asyncio
import logging
import sys

from log_middleware import SanicTraceMiddleware, setup_trace_logging, TraceConfig
from sanic import Sanic
import sanic.response


def create_demo_app() -> Sanic:
    import log_middleware.provider as p
    p._provider = None  # 重置单例，允许重复演示

    app = Sanic("demo-service")

    config = TraceConfig(
        service_name="demo-service",
        exporter_type="console",
    )
    SanicTraceMiddleware(app, service_name="demo-service", config=config)
    setup_trace_logging(config=config)

    logger = logging.getLogger("demo-service")

    @app.get("/hello")
    async def hello(request):
        logger.info("处理 /hello 请求")
        return sanic.response.json({"message": "Hello from demo-service!"})

    return app


if __name__ == "__main__":
    if "--demo" in sys.argv:
        app = create_demo_app()
        print("启动演示服务，访问 http://localhost:8000/hello")
        app.run(host="0.0.0.0", port=8000, debug=True, single_process=True)
    else:
        print(__doc__)
