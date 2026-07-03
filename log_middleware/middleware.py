from opentelemetry import trace, context as context_api
from opentelemetry.trace import SpanKind, StatusCode

from .config import TraceConfig
from .provider import setup_provider
from .propagation import extract_trace_context


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
                    "vx_trace.sdk.version": "0.0.1",
                    "vx_trace.sdk.language": "python",
                }
            )
        else:
            config.service_name = service_name

        setup_provider(config)
        self._tracer = trace.get_tracer(__name__)

        app.register_middleware(self._before_request, "request")
        app.register_middleware(self._after_response, "response")

    async def _before_request(self, request):
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

    async def _after_response(self, request, response):
        span = getattr(request.ctx, "otel_span", None)
        token = getattr(request.ctx, "otel_token", None)

        if span is not None:
            if response is not None:
                span.set_attribute("http.status_code", response.status)
                if response.status >= 500:
                    span.set_status(StatusCode.ERROR)
                else:
                    span.set_status(StatusCode.OK)
            span.end()

        if token is not None:
            context_api.detach(token)
