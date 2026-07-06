from opentelemetry.sdk.trace.export import (
    SpanExporter,
    SpanExportResult,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    BatchSpanProcessor,
)


class FileSpanExporter(SpanExporter):
    """将每个 Span 以 JSON 行写入文件。"""

    def __init__(self, path: str):
        self._file = open(path, "a", encoding="utf-8")

    def export(self, spans):
        for span in spans:
            self._file.write(span.to_json() + "\n")
        self._file.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self._file.close()


def build_processors(config: "TraceConfig") -> list:
    """根据配置返回 SpanProcessor 列表。"""
    # otlp 导出用 BatchSpanProcessor 以减少网络开销，其余遵循 processor_type 配置
    def make_processor(exporter):
        if config.exporter_type == "otlp" or config.processor_type == "batch":
            return BatchSpanProcessor(exporter)
        return SimpleSpanProcessor(exporter)

    exporters = []
    if config.exporter_type in ("console", "both"):
        exporters.append(ConsoleSpanExporter())
    if config.exporter_type in ("file", "both"):
        if not config.log_file_path:
            raise ValueError("exporter_type 为 'file' 或 'both' 时必须设置 log_file_path")
        exporters.append(FileSpanExporter(config.log_file_path))
    if config.exporter_type == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporters.append(OTLPSpanExporter(endpoint=f"{config.otlp_endpoint}/v1/traces"))

    return [make_processor(exp) for exp in exporters]
