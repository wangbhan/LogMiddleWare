"""
在有请求上下文的情况下测试 Sanic 日志
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
from sanic import Sanic
import sanic.response
from sanic.log import logger as sanic_logger
from log_middleware import SanicTraceMiddleware, setup_trace_logging, TraceConfig

config = TraceConfig(
    log_output_path="./logs/context-test.log",
    log_level=logging.INFO
)

app = Sanic("context-test")
SanicTraceMiddleware(app, service_name="context-test", config=config)
setup_trace_logging(config)

@app.get("/test")
async def test_handler(request):
    # 使用 sanic.log.logger 打印日志（用户的实际使用方式）
    sanic_logger.info("在有请求上下文时使用 sanic.log.logger")
    sanic_logger.info("这应该包含 trace_id")
    return sanic.response.json({"message": "success"})

if __name__ == "__main__":
    print("启动上下文测试服务器...")
    print("访问 http://localhost:8765/test 进行测试")
    app.run(host="0.0.0.0", port=8765, debug=False, single_process=True, access_log=True)