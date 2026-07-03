from dataclasses import dataclass


@dataclass
class TraceConfig:
    service_name: str = "unknown-service"
    exporter_type: str = "console"  # "console" | "file" | "both"
    log_file_path: str | None = None
    processor_type: str = "simple"  # "simple" | "batch"
    auto_instrument_aiohttp: bool = True  # 自动拦截 aiohttp 出站请求，无需手动 inject headers
    log_format: str = (
        "[%(asctime)s] %(levelname)s [%(name)s] "
        "[trace_id=%(trace_id)s span_id=%(span_id)s parent_span_id=%(parent_span_id)s] "
        "%(message)s"
    )
    log_level: int = 10  # logging.DEBUG
