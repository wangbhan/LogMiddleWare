"""
手动 Traced* 包装类：各 HTTP 客户端的零侵入替代品。

在不使用全局 patch 的场景下，直接用对应的 Traced* 类替换原生客户端即可自动注入
W3C traceparent 头，无需手动调用 inject_trace_headers。

    aiohttp:  TracedClientSession  替代 aiohttp.ClientSession
    httpx:    TracedAsyncClient    替代 httpx.AsyncClient
              TracedClient         替代 httpx.Client
    requests: TracedSession        替代 requests.Session
"""

from opentelemetry.propagate import inject as _otel_inject


# ─── aiohttp ─────────────────────────────────────────────────────────────────

try:
    import aiohttp as _aiohttp
except ImportError as _e:
    _aiohttp = None  # type: ignore[assignment]


class TracedClientSession:
    """
    aiohttp.ClientSession 的替代品，自动注入 traceparent 头。

    用法：
        async with TracedClientSession() as session:
            async with session.get("http://...") as resp:
                ...
    """

    def __new__(cls, *args, **kwargs):
        if _aiohttp is None:
            raise ImportError("TracedClientSession 需要安装 aiohttp") from None
        return object.__new__(cls)

    def __init__(self, *args, **kwargs):
        tc = _aiohttp.TraceConfig()

        async def _inject(session, ctx, params):
            _otel_inject(params.headers)

        tc.on_request_start.append(_inject)
        existing = list(kwargs.pop("trace_configs", []))
        kwargs["trace_configs"] = [tc] + existing
        self._session = _aiohttp.ClientSession(*args, **kwargs)

    async def __aenter__(self):
        return await self._session.__aenter__()

    async def __aexit__(self, *args):
        return await self._session.__aexit__(*args)

    def __getattr__(self, name):
        return getattr(self._session, name)


# ─── httpx ───────────────────────────────────────────────────────────────────

try:
    import httpx as _httpx
except ImportError as _e:
    _httpx = None  # type: ignore[assignment]


class TracedAsyncClient:
    """
    httpx.AsyncClient 的替代品，自动注入 traceparent 头。

    用法：
        async with TracedAsyncClient() as client:
            resp = await client.get("http://...")
    """

    def __new__(cls, *args, **kwargs):
        if _httpx is None:
            raise ImportError("TracedAsyncClient 需要安装 httpx") from None
        return object.__new__(cls)

    def __init__(self, *args, **kwargs):
        hooks = dict(kwargs.pop("event_hooks", None) or {})

        async def _inject(request):
            _otel_inject(request.headers)

        hooks.setdefault("request", []).insert(0, _inject)
        kwargs["event_hooks"] = hooks
        self._client = _httpx.AsyncClient(*args, **kwargs)

    async def __aenter__(self):
        return await self._client.__aenter__()

    async def __aexit__(self, *args):
        return await self._client.__aexit__(*args)

    def __getattr__(self, name):
        return getattr(self._client, name)


class TracedClient:
    """
    httpx.Client 的替代品，自动注入 traceparent 头（同步版本）。

    用法：
        with TracedClient() as client:
            resp = client.get("http://...")
    """

    def __new__(cls, *args, **kwargs):
        if _httpx is None:
            raise ImportError("TracedClient 需要安装 httpx") from None
        return object.__new__(cls)

    def __init__(self, *args, **kwargs):
        hooks = dict(kwargs.pop("event_hooks", None) or {})

        def _inject(request):
            _otel_inject(request.headers)

        hooks.setdefault("request", []).insert(0, _inject)
        kwargs["event_hooks"] = hooks
        self._client = _httpx.Client(*args, **kwargs)

    def __enter__(self):
        return self._client.__enter__()

    def __exit__(self, *args):
        return self._client.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._client, name)


# ─── requests ────────────────────────────────────────────────────────────────

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore[assignment]


class TracedSession:
    """
    requests.Session 的替代品，自动注入 traceparent 头（同步，适用于独立脚本）。

    OTel 上下文需调用方自行建立（tracer.start_as_current_span 等）。

    用法：
        with TracedSession() as session:
            resp = session.get("http://...")
    """

    def __new__(cls, *args, **kwargs):
        if _requests is None:
            raise ImportError("TracedSession 需要安装 requests") from None
        return object.__new__(cls)

    def __init__(self):
        self._session = _requests.Session()

    def send(self, request, **kwargs):
        _otel_inject(request.headers)
        return self._session.send(request, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._session.close()

    def __getattr__(self, name):
        return getattr(self._session, name)
