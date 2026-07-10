import contextvars
import logging
from contextvars import ContextVar
from dataclasses import dataclass

from opentelemetry import trace, context as context_api
from opentelemetry.trace import SpanKind, StatusCode

from .config import TraceConfig
from .provider import setup_provider
from .propagation import extract_trace_context

_logger = logging.getLogger(__name__)


@dataclass
class _TraceCleanupInfo:
    """存储当前请求的 span 和 detach token，供 send() patch 在真正结束时清理。"""
    span: object
    token: object
    done: bool = False  # 防止重复 end/detach


# 每个 asyncio Task 独立存储，ContextVar 保证并发请求间隔离
_REQUEST_TRACE: ContextVar[_TraceCleanupInfo | None] = ContextVar(
    "_log_middleware_trace", default=None
)

_sanic_send_patched = False       # 是否已尝试过 patch（保证幂等，无论成功失败）
_send_patch_active = False        # send patch 是否真正生效（探测失败时为 False）
_run_in_executor_patched = False


def _patch_run_in_executor_once() -> None:
    """
    对 asyncio.BaseEventLoop.run_in_executor 做一次全局 class-level patch。

    问题：`loop.run_in_executor(None, sync_fn)` 会把同步函数扔进线程池执行，
         而线程有独立的 ContextVar 命名空间，asyncio Task 的 OTel span 不会
         自动跨到线程 → 线程内日志失去 trace_id。

    修复：patch 后，每次调用自动 `contextvars.copy_context()` 快照当前 Task
         的所有 ContextVar（含 OTel span），并用 `ctx.run(fn, *args)` 在线程
         内恢复 → 线程内日志重新有 trace_id。

    验证过 uvloop（Sanic 默认）也会走这条代码路径，所以只 patch BaseEventLoop 即可。
    """
    global _run_in_executor_patched
    if _run_in_executor_patched:
        return
    _run_in_executor_patched = True

    from asyncio.base_events import BaseEventLoop
    _orig_run_in_executor = BaseEventLoop.run_in_executor

    def _traced_run_in_executor(self, executor, func, *args):
        # 快照当前 asyncio Task 的所有 ContextVar（含 OTel span）
        # 无请求上下文时快照仍然会做，但 ctx 为空，语义等价于原生 run_in_executor
        ctx = contextvars.copy_context()

        def _wrapped(*inner_args):
            return ctx.run(func, *inner_args)

        return _orig_run_in_executor(self, executor, _wrapped, *args)

    BaseEventLoop.run_in_executor = _traced_run_in_executor


def _patch_sanic_send_once() -> None:
    """
    对 BaseHTTPResponse.send() 做 class-level patch（只执行一次）。

    将 span.end() + context_api.detach() 从 _after_response() 延迟到
    send(end_stream=True) 时执行，使 OTel ContextVar 在整个流式输出期间保持有效。

    因为 BaseHTTPResponse 使用 __slots__，无法对单个实例做 instance-level patch，
    必须在类层面替换 send 方法。

    防御 1: Sanic 主版本升级/内部路径改动导致 import 失败时，仅打印 warning 并跳过
    patch —— 此时 `_send_patch_active` 保持 False，`_after_response` 会退回旧逻辑
    做立即清理，保证 span 不泄漏（代价是流式修复失效）。
    """
    global _sanic_send_patched, _send_patch_active
    if _sanic_send_patched:
        return
    _sanic_send_patched = True  # 无论成功失败都标记为"尝试过"，保证幂等

    try:
        from sanic.response.types import BaseHTTPResponse
    except ImportError as e:
        _logger.warning(
            "Sanic BaseHTTPResponse 导入失败（%s），跳过 send patch。"
            "流式输出场景 trace_id 将丢失，_after_response 退回立即清理逻辑。", e
        )
        return

    _orig_send = BaseHTTPResponse.send

    async def _traced_send(self, data=None, end_stream=None):
        # Sanic 约定：data=None 且 end_stream=None 等价于 end_stream=True
        is_final = end_stream or (data is None and end_stream is None)
        if is_final:
            info = _REQUEST_TRACE.get()
            if info is not None and not info.done:
                info.done = True
                info.span.end()
                context_api.detach(info.token)
        return await _orig_send(self, data, end_stream=end_stream)

    BaseHTTPResponse.send = _traced_send
    _send_patch_active = True   # 只有真正 patch 成功才置为 True


class SanicTraceMiddleware:
    """
    OpenTelemetry 链路追踪中间件，像注册 Sanic 中间件一样使用：

        app = Sanic("my-service")
        SanicTraceMiddleware(app, service_name="my-service")
    """

    def __init__(self, app, service_name: str = "unknown-service", config: TraceConfig | None = None):
        if config is None:
            config = TraceConfig(
                service_name=service_name,
                resource_attributes={
                    "vx_trace.name": "vx_trace",
                    "vx_trace.sdk.name": "LogMiddleWare",
                    "vx_trace.sdk.version": "0.1.0",
                    "vx_trace.sdk.language": "python",
                }
            )
        else:
            config.service_name = service_name

        self._provider = setup_provider(config)
        self._tracer = trace.get_tracer(__name__)

        app.register_middleware(self._before_request, "request")
        app.register_middleware(self._after_response, "response")

        @app.before_server_stop
        async def _flush_spans(app, loop):
            self._provider.force_flush(timeout_millis=5000)
            self._provider.shutdown()

        # 延迟 patch：在中间件初始化时对 Sanic 的 send() 做一次全局 patch
        _patch_sanic_send_once()
        # 同时 patch loop.run_in_executor：线程池自动继承当前 asyncio Task 的 OTel 上下文
        _patch_run_in_executor_once()

    async def _before_request(self, request):
        traceparent = request.headers.get("traceparent")
        if traceparent:
            _logger.debug("收到上游 traceparent: %s", traceparent)

        # 从入站请求头提取父上下文（跨服务传播的 traceparent）
        parent_ctx = extract_trace_context(request)

        span = self._tracer.start_span(
            name=f"{request.method} {request.path}",
            context=parent_ctx,
            kind=SpanKind.SERVER,
            attributes={
                "http.method": request.method,
                "http.url": str(request.url),
                "http.scheme": request.scheme,
                "http.host": request.host,
                "http.target": request.path,
            },
        )

        # 将 span 绑定到当前 asyncio Task 的 ContextVar，保证请求间隔离
        ctx = trace.set_span_in_context(span)
        token = context_api.attach(ctx)

        request.ctx.otel_span = span
        request.ctx.otel_token = token
        request.ctx.otel_context = ctx  # 暴露不可变 OTel Context，供用户代码按需使用

        # 存储清理信息到 ContextVar：send() patch 将在 end_stream=True 时读取并执行清理
        _REQUEST_TRACE.set(_TraceCleanupInfo(span=span, token=token))

    async def _after_response(self, request, response):
        span = getattr(request.ctx, "otel_span", None)

        if span is not None and response is not None:
            span.set_attribute("http.status_code", response.status)
            if response.status >= 500:
                span.set_status(StatusCode.ERROR)
            else:
                span.set_status(StatusCode.OK)
            ctx = span.get_span_context()
            if ctx.is_valid:
                response.headers["X-Trace-Id"] = trace.format_trace_id(ctx.trace_id)

        # 防御 2: 条件兜底清理
        #   ① response is None (handler 抛异常未生成响应) → send 不会被触发
        #   ② send patch 未生效 (Sanic 不兼容，探测失败) → 退回原来的中间件清理时机
        # 正常路径（有 response 且 patch 生效）继续依赖 _traced_send 在 end_stream=True 时清理
        # info.done flag 与 _traced_send 共享，天然防重复清理
        should_cleanup_now = response is None or not _send_patch_active
        if should_cleanup_now:
            info = _REQUEST_TRACE.get()
            if info is not None and not info.done:
                info.done = True
                info.span.end()
                context_api.detach(info.token)
