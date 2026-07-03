"""
下游服务（port 8001）。
接收来自 service_a 的请求，从 traceparent 头继承 trace_id，形成链路的第二个节点。
"""
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("service-b")
SanicTraceMiddleware(app, service_name="service-b")
setup_trace_logging()

logger = logging.getLogger("service-b")


@app.get("/internal/data")
async def internal_data(request):
    # 此处的 trace_id 与 service_a 相同，parent_span_id = service_a 的 span_id
    logger.info("service-b: 收到内部数据请求，开始处理")
    data = {"items": [1, 2, 3], "source": "service-b"}
    logger.info("service-b: 数据处理完成，返回响应")
    return sanic.response.json(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=True, single_process=True)
