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
    from .config import TraceConfig

    Processor = SimpleSpanProcessor if config.processor_type == "simple" else BatchSpanProcessor

    exporters = []
    if config.exporter_type in ("console", "both"):
        exporters.append(ConsoleSpanExporter())
    if config.exporter_type in ("file", "both"):
        if not config.log_file_path:
            raise ValueError("exporter_type 为 'file' 或 'both' 时必须设置 log_file_path")
        exporters.append(FileSpanExporter(config.log_file_path))

    return [Processor(exp) for exp in exporters]
