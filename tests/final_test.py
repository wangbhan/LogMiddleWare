"""
最终测试：验证 sanic.log.logger 在请求上下文中的 trace_id
"""
import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
from sanic import Sanic
import sanic.response
from sanic.log import logger as sanic_logger
from log_middleware import SanicTraceMiddleware, setup_trace_logging, TraceConfig

# 清除旧日志
log_file = "./logs/final-test.log"
if os.path.exists(log_file):
    os.remove(log_file)

config = TraceConfig(
    auto_configure_sanic_loggers=True,
    log_output_path=log_file,
    log_level=logging.INFO
)

app = Sanic("final-test")
SanicTraceMiddleware(app, service_name="final-test", config=config)
setup_trace_logging(config)

# 创建一个业务 logger 用于对比
business_logger = logging.getLogger("business")

@app.get("/test")
async def test_handler(request):
    business_logger.info("业务logger：有请求上下文")
    sanic_logger.info("sanic.log.logger：有请求上下文")
    sanic_logger.info("sanic.log.logger：应该包含相同trace_id")
    return sanic.response.json({"message": "success"})

if __name__ == "__main__":
    print("=" * 50)
    print("最终测试：验证 sanic.log.logger 的 trace_id")
    print("=" * 50)
    print(f"日志文件: {log_file}")
    print("启动服务器 http://localhost:9876/test")
    print()

    app.config.ACCESS_LOG = True
    app.run(host="0.0.0.0", port=9876, debug=False, single_process=True, access_log=True)