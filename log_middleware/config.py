from dataclasses import dataclass, field


@dataclass
class TraceConfig:
    service_name: str = "unknown-service"
    exporter_type: str = "otlp"  # "none" | "console" | "file" | "both" ｜ "otlp"
    log_file_path: str | None = None         # 导出span的jsonl使用
    log_output_path: str | None = None       # 日志文本落盘路径，None 表示不写文件
    log_max_bytes: int = 10 * 1024 * 1024    # 单文件上限，默认 10 MB
    log_backup_count: int = 5                # 保留旧文件数，默认 5 个
    processor_type: str = "simple"  # "simple" | "batch"
    resource_attributes: dict = field(default_factory=dict)  # 自定义 resource 属性，合并到 Span 的 resource 块
    otlp_endpoint: str = "http://localhost:4318"  # OTLP HTTP 端点，exporter_type="otlp" 时生效
    log_format: str = (
        "[%(asctime)s] %(levelname)s "
        "[%(trace_id)s - %(span_id)s - %(parent_span_id)s] "
        "[%(name)s] %(message)s"
    )
    log_level: int = 10  # logging.DEBUG
    baggage_keys: list[str] = field(default_factory=list)  # 第二阶段启用，需透传的业务字段名，如 ["user_id", "tenant_id"]
