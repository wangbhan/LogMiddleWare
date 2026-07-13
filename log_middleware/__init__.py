__version__ = "0.1.0"

from .middleware import SanicTraceMiddleware
from .middleware import _patch_requests_inject_once as patch_requests
from .logging_integration import setup_trace_logging, TraceContextFilter
from .config import TraceConfig
from .propagation import inject_trace_headers, extract_trace_context
from .provider import get_tracer
from .http_client import TracedClientSession, TracedAsyncClient, TracedClient, TracedSession

__all__ = [
    "SanicTraceMiddleware",
    "setup_trace_logging",
    "TraceContextFilter",
    "TraceConfig",
    "inject_trace_headers",
    "extract_trace_context",
    "get_tracer",
    # HTTP 客户端手动包装类（替代原生客户端，自动注入 traceparent）
    "TracedClientSession",   # aiohttp
    "TracedAsyncClient",     # httpx 异步
    "TracedClient",          # httpx 同步
    "TracedSession",         # requests（独立脚本）
    # requests 全局 patch（独立脚本入口调用一次）
    "patch_requests",
]
