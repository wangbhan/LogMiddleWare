"""
测试日志重复打印的情况
"""
import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging, TraceConfig

# 配置临时日志文件
test_log_path = "./logs/duplicate-test.log"

# 清空旧日志
import os
if os.path.exists(test_log_path):
    os.remove(test_log_path)

config = TraceConfig(
    auto_configure_sanic_loggers=True,
    log_output_path=test_log_path,
    log_level=logging.INFO  # 只看 INFO 级别
)

app = Sanic("duplicate-test")
SanicTraceMiddleware(app, service_name="duplicate-test", config=config)

# 配置业务日志
setup_trace_logging(config)

# 使用 sanic.log.logger 来模拟用户的使用方式
from sanic.log import logger as sanic_logger
logger = logging.getLogger("duplicate-test")

@app.get("/test")
async def test_handler(request):
    # 使用 sanic.log.logger 打印日志（用户的方式）
    sanic_logger.info("Sanic Logger: 测试日志")

    # 使用普通业务 logger 打印日志
    logger.info("业务 Logger: 测试日志")

    return sanic.response.json({"message": "success"})

if __name__ == "__main__":
    print("启动重复测试服务器...")
    print(f"日志文件: {test_log_path}")
    print("访问 http://localhost:8888/test 进行测试")

    app.config.ACCESS_LOG = True
    app.run(host="0.0.0.0", port=8888, debug=False, single_process=True, access_log=True)