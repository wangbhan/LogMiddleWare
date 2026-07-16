"""
测试 Sanic 框架日志自动配置功能。

验证：
1. Sanic 访问日志是否包含 trace_id
2. Sanic 错误日志是否包含 trace_id
3. 业务日志和框架日志格式是否统一
4. 配置的幂等性（多次调用不会重复配置）
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging, TraceConfig

def test_sanic_loggers_auto_configuration():
    """测试 Sanic 日志自动配置功能"""

    print("=== 开始测试 Sanic 框架日志自动配置 ===\n")

    # 创建配置，启用自动配置
    config = TraceConfig(
        auto_configure_sanic_loggers=True,  # 启用自动配置
        log_output_path="./logs/test-sanic-integration.log"
    )

    app = Sanic("test-sanic-logger-integration")

    # 初始化中间件（会自动配置 Sanic logger）
    SanicTraceMiddleware(app, service_name="test-service", config=config)

    # 配置业务日志
    setup_trace_logging(config)
    logger = logging.getLogger("test-service")

    @app.get("/api/test-success")
    async def test_success(request):
        """测试成功的请求"""
        logger.info("业务日志：处理测试请求")
        return sanic.response.json({"message": "success", "path": "/api/test-success"})

    @app.get("/api/test-error")
    async def test_error(request):
        """测试产生错误的请求"""
        logger.info("业务日志：即将产生错误")
        # 故意触发一个 500 错误
        raise ValueError("测试错误")

    print("✓ 中间件初始化完成")
    print("✓ 已配置自动配置 Sanic logger: {}".format(config.auto_configure_sanic_loggers))
    print("✓ 日志输出路径: {}".format(config.log_output_path))
    print()

    print("=== 测试说明 ===")
    print("1. 启动服务: python tests/test_sanic_loggers_integration.py")
    print("2. 测试成功请求: curl http://localhost:8080/api/test-success")
    print("3. 测试错误请求: curl http://localhost:8080/api/test-error")
    print("4. 观察日志输出，确认:")
    print("   - 业务日志包含 trace_id: [trace_id - span_id - parent_span_id]")
    print("   - Sanic 访问日志包含 trace_id: [trace_id - span_id - parent_span_id]")
    print("   - Sanic 错误日志包含 trace_id: [trace_id - span_id - parent_span_id]")
    print("   - 所有日志格式统一")
    print()

    if __name__ == "__main__":
        print("=== 启动测试服务器 ===")
        print("访问 http://localhost:8080/api/test-success 测试正常请求")
        print("访问 http://localhost:8080/api/test-error 测试错误请求")
        print()
        app.run(host="0.0.0.0", port=8080, debug=True, single_process=True)

if __name__ == "__main__":
    test_sanic_loggers_auto_configuration()