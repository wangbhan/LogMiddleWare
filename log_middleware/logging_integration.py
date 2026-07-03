import logging
from opentelemetry import trace
from opentelemetry.trace import format_trace_id, format_span_id

from .config import TraceConfig

_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16


class TraceContextFilter(logging.Filter):
    """
    向每条 LogRecord 自动注入 trace_id、span_id、parent_span_id。
    通过 contextvars 读取当前 asyncio Task 的活跃 Span，异步环境下天然隔离。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context()

        if ctx and ctx.is_valid:
            record.trace_id = format_trace_id(ctx.trace_id)
            record.span_id = format_span_id(ctx.span_id)
        else:
            record.trace_id = _ZERO_TRACE_ID
            record.span_id = _ZERO_SPAN_ID

        # _parent 是 OTel SDK 的内部属性（ReadableSpan），用 getattr 防护
        parent = getattr(span, "_parent", None)
        if parent is not None and parent.is_valid:
            record.parent_span_id = format_span_id(parent.span_id)
        else:
            record.parent_span_id = _ZERO_SPAN_ID

        return True


def setup_trace_logging(
    config: TraceConfig | None = None,
    logger_names: list[str] | None = None,
) -> None:
    """
    配置日志格式并挂载 TraceContextFilter。

    Args:
        config: 日志格式和级别配置，默认使用 TraceConfig 默认值。
        logger_names: 要配置的 logger 名称列表，None 表示配置 root logger。
    """
    if config is None:
        config = TraceConfig()

    filter_ = TraceContextFilter()
    formatter = logging.Formatter(config.log_format)

    targets = logger_names if logger_names is not None else [None]

    for name in targets:
        logger = logging.getLogger(name)
        logger.setLevel(config.log_level)

        # 将 filter 挂在 handler 上（而非 logger），保证子 logger 通过传播到达 root handler
        # 时也能正确注入 trace_id 等字段，避免 KeyError
        existing = [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]
        if existing:
            for h in existing:
                if not any(isinstance(f, TraceContextFilter) for f in h.filters):
                    h.addFilter(filter_)
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            handler.addFilter(filter_)
            logger.addHandler(handler)
