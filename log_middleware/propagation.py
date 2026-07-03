from opentelemetry.propagators.textmap import Getter
from opentelemetry.propagate import extract, inject
from opentelemetry.context import Context


class SanicHeaderGetter(Getter):
    """适配 Sanic 的 request.headers 到 OTel Getter 接口。"""

    def get(self, carrier, key: str) -> list[str] | None:
        val = carrier.get(key)
        return [val] if val is not None else None

    def keys(self, carrier) -> list[str]:
        return list(carrier.keys())


_sanic_getter = SanicHeaderGetter()


def extract_trace_context(sanic_request) -> Context:
    """从入站 Sanic 请求头中提取 W3C traceparent 上下文。"""
    return extract(sanic_request.headers, getter=_sanic_getter)


def inject_trace_headers(headers: dict, context: Context | None = None) -> dict:
    """向出站请求 headers dict 注入 W3C traceparent，用于下游服务调用。"""
    inject(headers, context=context)
    return headers
