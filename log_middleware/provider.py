from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.propagate import set_global_textmap

_provider: TracerProvider | None = None


def setup_provider(config: "TraceConfig") -> TracerProvider:
    global _provider
    if _provider is not None:
        return _provider

    resource = Resource.create({"service.name": config.service_name})
    provider = TracerProvider(resource=resource)

    from .exporters import build_processors
    for processor in build_processors(config):
        provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))

    if config.auto_instrument_aiohttp:
        try:
            from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
            instrumentor = AioHttpClientInstrumentor()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument()
        except ImportError:
            pass

    _provider = provider
    return provider


def get_tracer(name: str) -> trace.Tracer:
    if _provider is None:
        raise RuntimeError("请先调用 SanicTraceMiddleware 初始化 provider。")
    return _provider.get_tracer(name)
