"""
简单验证脚本：测试 Sanic 访问日志是否包含 trace_id
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
test_log_path = "./logs/quick-test.log"

config = TraceConfig(
    auto_configure_sanic_loggers=True,  # 启用自动配置
    log_output_path=test_log_path
)

app = Sanic("quick-test")
SanicTraceMiddleware(app, service_name="quick-test", config=config)

# 配置业务日志
setup_trace_logging(config)
logger = logging.getLogger("quick-test")

@app.get("/test")
async def test_handler(request):
    logger.info("业务日志：正在处理请求")
    return sanic.response.json({"message": "success", "timestamp": time.time()})

if __name__ == "__main__":
    print("启动测试服务器...")
    print(f"日志文件: {test_log_path}")
    print("访问 http://localhost:9999/test 进行测试")

    # 确保启用访问日志
    app.config.ACCESS_LOG = True

    app.run(host="0.0.0.0", port=9999, debug=False, single_process=True, access_log=True)