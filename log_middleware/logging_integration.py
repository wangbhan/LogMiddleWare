import logging
import logging.handlers
import os
from opentelemetry import trace
from opentelemetry.trace import format_trace_id, format_span_id

from .config import TraceConfig


class TraceContextFilter(logging.Filter):
    """
    向每条 LogRecord 自动注入 trace_id、span_id、parent_span_id。
    通过 contextvars 读取当前 asyncio Task 的活跃 Span，异步环境下天然隔离。

    新增功能：将零值trace字段转换为空字符串，便于在Filebeat中进行条件过滤。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        span = trace.get_current_span()
        ctx = span.get_span_context()

        if ctx and ctx.is_valid:
            record.trace_id = format_trace_id(ctx.trace_id)
            record.span_id = format_span_id(ctx.span_id)
        else:
            # 将零值改为空字符串，便于后续在Filebeat中进行条件过滤
            record.trace_id = ""
            record.span_id = ""

        # _parent 是 OTel SDK 的内部属性（ReadableSpan），用 getattr 防护
        parent = getattr(span, "_parent", None)
        if parent is not None and parent.is_valid:
            record.parent_span_id = format_span_id(parent.span_id)
        else:
            record.parent_span_id = ""

        return True


class SanicAccessFilter(logging.Filter):
    """将 sanic.access 的 extra 字段重组为 record.msg，使其能被通用 SDK formatter 渲染。

    sanic.access logger 的 msg 始终为空字符串，实际内容在 extra（host/request/status/byte/duration）中。
    此 Filter 在 record 传播到 root logger 前原地改写 msg，无需单独的 formatter 或 handler。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.getMessage():
            host = getattr(record, "host", "-")
            request = getattr(record, "request", "-")
            status = getattr(record, "status", "-")
            byte = getattr(record, "byte", "-")
            duration = getattr(record, "duration", "")
            record.msg = f"{host} {request} {status} {byte} {duration}".strip()
            record.args = ()
        return True


_SANIC_LOGGER_NAMES = (
    "sanic.root",
    "sanic.error",
    "sanic.access",
    "sanic.server",
    "sanic.websockets",
)


def _configure_sanic_loggers(config: TraceConfig) -> None:
    """清理 sanic.* loggers 的 Sanic 原生 handlers，使日志统一流经 root logger 的 SDK handler。

    策略：
    1. 移除 Sanic 通过 dictConfig 安装的 AutoFormatter handlers
    2. 保留 propagate=True，让 sanic.* 日志传播到 root logger（SDK handler 统一输出）
    3. 对 sanic.access 额外挂 SanicAccessFilter，提前改写空 message 为可读访问信息
    sanic.* loggers 本身不挂任何 handler，root logger 的单一 SDK handler 处理所有输出。

    幂等：SanicAccessFilter 已存在时跳过，安全多次调用。
    """
    try:
        from sanic.logging.formatter import AutoFormatter
    except ImportError:
        return

    for name in _SANIC_LOGGER_NAMES:
        lg = logging.getLogger(name)

        # 移除 Sanic 原生 handlers（识别特征：formatter 是 AutoFormatter 实例）
        for h in list(lg.handlers):
            if isinstance(h.formatter, AutoFormatter):
                lg.removeHandler(h)

        # sanic.access 挂 Filter 改写 msg（不挂 handler，由 root logger 统一输出）
        if name == "sanic.access":
            if not any(isinstance(f, SanicAccessFilter) for f in lg.filters):
                lg.addFilter(SanicAccessFilter())

        # 确保传播到 root logger（dictConfig 可能已改过，显式重置）
        lg.propagate = True


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
        # name为None传给getLogger()是root配置，即全局日志都按这个标准配置
        logger = logging.getLogger(name)
        logger.setLevel(config.log_level)

        # type() 精确匹配，排除 RotatingFileHandler（它继承自 StreamHandler）
        existing_stream = [h for h in logger.handlers if type(h) is logging.StreamHandler]
        existing_file = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]

        if existing_stream:
            for h in existing_stream:
                h.setFormatter(formatter)
                if not any(isinstance(f, TraceContextFilter) for f in h.filters):
                    h.addFilter(filter_)
        else:
            handler = logging.StreamHandler()
            handler.setFormatter(formatter)
            handler.addFilter(filter_)
            logger.addHandler(handler)

        if config.log_output_path and not existing_file:
            os.makedirs(os.path.dirname(os.path.abspath(config.log_output_path)), exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                filename=config.log_output_path,
                maxBytes=config.log_max_bytes,
                backupCount=config.log_backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(filter_)
            logger.addHandler(file_handler)