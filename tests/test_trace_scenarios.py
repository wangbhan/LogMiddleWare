"""
全面测试：哪些场景有 trace_id / span_id / parent_span_id，哪些没有。

重点验证：大模型流式输出（LLM Streaming）场景下 trace 字段的行为及修复方案。

关键发现（Python asyncio + OTel 行为）：
  - asyncio.create_task() 在 context attach 期间调用 → 自动复制 ContextVar → 有 trace
  - asyncio.create_task() 在 context detach 之后调用 → 无 trace
  - asyncio.loop.run_in_executor() → 线程不复制 ContextVar → 无 trace
  - async for 迭代（同一 asyncio Task）→ 有 trace
  - Sanic response.stream(callback) → callback 在 _after_response(detach) 之后执行 → 无 trace

运行：
    .venv/bin/python tests/test_trace_scenarios.py
"""
import asyncio
import contextvars
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opentelemetry import trace, context as context_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from log_middleware.logging_integration import TraceContextFilter


# ──────────────────────────────────────────────
# 测试基础设施
# ──────────────────────────────────────────────

class LogCapture(logging.Handler):
    """拦截日志记录，方便断言 trace 字段。"""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def get_trace_fields(self, idx: int = -1) -> dict:
        r = self.records[idx]
        return {
            "trace_id": getattr(r, "trace_id", "MISSING"),
            "span_id": getattr(r, "span_id", "MISSING"),
            "parent_span_id": getattr(r, "parent_span_id", "MISSING"),
        }

    def clear(self) -> None:
        self.records.clear()


def make_test_env() -> tuple[TracerProvider, trace.Tracer, logging.Logger, LogCapture]:
    """
    每个测试用例独立创建隔离的 OTel 环境。

    注意：不调用 trace.set_tracer_provider()（全局 provider 只能设置一次）。
    直接使用 provider.get_tracer() 创建 tracer，
    TraceContextFilter 从 ContextVar 读取 span，不依赖全局 provider。
    """
    import log_middleware.provider as _p
    _p._provider = None  # 重置 SDK 单例

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    tracer = provider.get_tracer("test-tracer")

    capture = LogCapture()
    capture.addFilter(TraceContextFilter())

    logger = logging.getLogger(f"test.{id(capture)}")
    logger.handlers.clear()
    logger.addHandler(capture)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    return provider, tracer, logger, capture


def has_trace(fields: dict) -> bool:
    return bool(fields.get("trace_id"))


def has_parent(fields: dict) -> bool:
    return bool(fields.get("parent_span_id"))


# ──────────────────────────────────────────────
# 结果收集与展示
# ──────────────────────────────────────────────

RESULTS: list[tuple[str, str]] = []


def report(name: str, passed: bool, fields: dict, note: str = "") -> None:
    icon = "✅" if passed else "❌"
    RESULTS.append((name, icon))
    tid = fields.get("trace_id") or "(空)"
    sid = fields.get("span_id") or "(空)"
    psid = fields.get("parent_span_id") or "(空)"
    suffix = f"  ← {note}" if note else ""
    short = lambda s: (s[:12] + "...") if len(s) > 12 else s
    print(f"  {icon}  {name}")
    print(f"        trace_id       = {short(tid)}")
    print(f"        span_id        = {short(sid)}")
    print(f"        parent_span_id = {short(psid)}{suffix}")


# ══════════════════════════════════════════════
# Group 1: 正常有 trace_id 的场景
# ══════════════════════════════════════════════

async def test_direct_context_attach():
    """
    场景1: 直接 attach OTel context 后记日志
    原理: context_api.attach(ctx) 将 span 写入当前 asyncio Task 的 ContextVar，
          TraceContextFilter.filter() 调用 trace.get_current_span() 从 ContextVar 读取
    预期: 有 trace_id
    """
    _, tracer, logger, capture = make_test_env()

    span = tracer.start_span("root-span")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        logger.info("直接 attach 后记日志")
    finally:
        span.end()
        context_api.detach(token)

    fields = capture.get_trace_fields()
    report("test_direct_context_attach", has_trace(fields), fields,
           "context_api.attach() 后 ContextVar 中有 span")


async def test_nested_await_chain():
    """
    场景2: 分层 await 链（controller → service → repository）
    原理: await 不切换 asyncio Task，ContextVar 在同一 Task 内自动共享
    预期: 三层日志的 trace_id 完全相同
    """
    _, tracer, logger, capture = make_test_env()

    async def repository_method():
        logger.info("[repo] 查询数据库")
        return [{"id": 1}]

    async def service_method():
        logger.info("[service] 聚合数据")
        return await repository_method()

    async def controller_handler():
        logger.info("[controller] 处理请求")
        return await service_method()

    span = tracer.start_span("http-request")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        await controller_handler()
    finally:
        span.end()
        context_api.detach(token)

    assert len(capture.records) == 3, f"期望3条日志，实际 {len(capture.records)} 条"
    ids = [capture.get_trace_fields(i)["trace_id"] for i in range(3)]
    all_same = len(set(ids)) == 1 and bool(ids[0])
    fields = capture.get_trace_fields(0)
    report("test_nested_await_chain", all_same, fields,
           f"3层（controller/service/repo）trace_id 均为 {ids[0][:8]}...")


async def test_manual_child_span():
    """
    场景3: 手动创建子 Span
    原理: tracer.start_as_current_span() 将子 span 推入 ContextVar 栈；
          退出 with 块后自动恢复父 span；
          child 日志的 parent_span_id = 父 span 的 span_id
    预期: child 日志有 trace_id 且 parent_span_id 不为空
    """
    _, tracer, logger, capture = make_test_env()

    with tracer.start_as_current_span("root-span") as root:
        root_sid = format(root.get_span_context().span_id, "016x")
        logger.info("root 日志")

        with tracer.start_as_current_span("child-span") as child:
            child_sid = format(child.get_span_context().span_id, "016x")
            logger.info("child 日志")  # parent_span_id 应 = root_sid

        logger.info("root 退出 child 后的日志")  # parent_span_id 应为空

    root_fields = capture.get_trace_fields(0)
    child_fields = capture.get_trace_fields(1)
    after_fields = capture.get_trace_fields(2)

    child_ok = (
        has_trace(child_fields)
        and child_fields["parent_span_id"] == root_sid
        and child_fields["span_id"] == child_sid
    )
    # 退出 child span 后，parent_span_id 恢复为空（root 没有父）
    after_ok = has_trace(after_fields) and not has_parent(after_fields)

    report("test_manual_child_span", child_ok and after_ok, child_fields,
           f"child.parent_span_id({child_fields['parent_span_id'][:8]})  == root.span_id({root_sid[:8]}): {child_ok}")


async def test_async_for_inside_handler():
    """
    场景4: handler 内部直接 async for 迭代（模拟 LLM 流式返回，但在 handler 内收集结果）
    原理: async for 在同一 asyncio Task 内迭代，ContextVar 不变
    预期: 每次迭代内的日志都有 trace_id

    注意：这种模式需要等所有 chunk 收集完后再返回响应，
          不同于 Sanic response.stream() 实时推送给客户端。
    """
    _, tracer, logger, capture = make_test_env()

    async def fake_llm_stream():
        for chunk in ["Hello", " world", "!"]:
            await asyncio.sleep(0)
            yield chunk

    span = tracer.start_span("POST /api/chat")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        result = ""
        async for chunk in fake_llm_stream():
            logger.info(f"收到 chunk: {chunk!r}")
            result += chunk
    finally:
        span.end()
        context_api.detach(token)

    all_have_trace = all(
        bool(capture.get_trace_fields(i)["trace_id"])
        for i in range(len(capture.records))
    )
    fields = capture.get_trace_fields(0)
    report("test_async_for_inside_handler", all_have_trace, fields,
           f"3个 chunk 日志均有 trace_id: {all_have_trace}（同 Task ContextVar 自动继承）")


# ══════════════════════════════════════════════
# Group 2: 无 trace_id（预期行为 / 已知限制）
# ══════════════════════════════════════════════

async def test_no_request_context():
    """
    场景5: 无任何请求上下文时记日志（服务启动日志、全局初始化等）
    原理: 没有 span 被 attach 到 ContextVar，trace.get_current_span() 返回 NonRecordingSpan
    预期: trace_id 为空（设计如此）
    """
    _, _, logger, capture = make_test_env()

    logger.info("Sanic 服务启动中...")
    logger.warning("配置加载完成")

    fields = capture.get_trace_fields(0)
    expected_empty = not has_trace(fields)
    report("test_no_request_context", expected_empty, fields,
           "启动日志 trace_id 应为空（预期行为）")


async def test_background_task_created_after_detach():
    """
    场景6: asyncio.create_task() 在 context_api.detach() 之后才创建任务
    原理: create_task 复制"当前时刻"的 ContextVar；
          如果在 detach 之后创建，此时 ContextVar 中已无 span → 任务无 trace
    典型场景: 请求完成后由调度器/事件触发的后台任务
    预期: trace_id 为空
    """
    _, tracer, logger, capture = make_test_env()

    background_fields: dict = {}

    async def background_work():
        logger.info("请求完成后才启动的后台任务")
        background_fields.update(capture.get_trace_fields(-1))

    # 请求周期
    span = tracer.start_span("request")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    span.end()
    context_api.detach(token)  # ← 先 detach

    # 请求完成后才创建任务
    task = asyncio.create_task(background_work())  # 此时 ContextVar 已无 span
    await task

    expected_empty = not has_trace(background_fields)
    report("test_background_task_created_after_detach", expected_empty, background_fields,
           "detach 后 create_task → 无 trace（任务复制的是空 ContextVar）")


# ══════════════════════════════════════════════
# Group 3: LLM 流式输出问题场景（Bug 复现）
# ══════════════════════════════════════════════

async def test_sanic_stream_callback_loses_context():
    """
    场景7: 模拟 Sanic response.stream(callback) 的真实执行顺序
    （这是 LLM 流式输出 trace_id 丢失的核心原因）

    Sanic 实际执行顺序：
      1. _before_request()  → context_api.attach(ctx)       [上下文绑定]
      2. 路由 handler 返回  ResponseStream(callback_fn)
      3. _after_response()  → span.end() + context_api.detach(token)  [上下文已清除!]
      4. Sanic 调用 callback_fn(response) 写流式 body
      5. callback_fn 内 logging → trace.get_current_span() = NonRecordingSpan → trace_id = ""

    预期: trace_id 为空（Bug 复现）
    """
    _, tracer, logger, capture = make_test_env()

    stream_fields: dict = {}

    # 步骤 1: _before_request → attach
    span = tracer.start_span("POST /api/chat/stream")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)

    # 步骤 2: 定义流式回调（此时上下文已绑定，但回调尚未执行）
    async def stream_callback(response):
        async def fake_llm_chunks():
            for chunk in ["Token1", " Token2", " Token3"]:
                await asyncio.sleep(0)
                yield chunk

        async for chunk in fake_llm_chunks():
            logger.info(f"[LLM stream] chunk: {chunk!r}")

        stream_fields.update(capture.get_trace_fields(-1))

    # 步骤 3: _after_response → 上下文清除！（中间件比回调早执行）
    span.end()
    context_api.detach(token)

    # 步骤 4: Sanic 调用 stream_callback（上下文已清除）
    await stream_callback(None)

    expected_empty = not has_trace(stream_fields)
    report("test_sanic_stream_callback_loses_context", expected_empty, stream_fields,
           "BUG: _after_response 先 detach，stream callback 后执行 → trace_id 丢失")


async def test_run_in_executor_loses_context():
    """
    场景8: asyncio.loop.run_in_executor() 在线程池执行同步代码
    原理: 线程池的线程有独立的 ContextVar 命名空间，不会复制父 Task 的 ContextVar
    典型场景: LLM 调用某些同步 SDK（如 requests 库）时用线程池包装
    预期: trace_id 为空（线程内无 OTel context）
    """
    _, tracer, logger, capture = make_test_env()

    thread_fields: dict = {}

    def sync_llm_call():
        """在线程池中执行的同步代码"""
        logger.info("[thread] 同步 LLM 调用")
        thread_fields.update(capture.get_trace_fields(-1))
        return "LLM response"

    span = tracer.start_span("POST /api/chat")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_llm_call)
    finally:
        span.end()
        context_api.detach(token)

    expected_empty = not has_trace(thread_fields)
    report("test_run_in_executor_loses_context", expected_empty, thread_fields,
           "BUG: run_in_executor 线程不继承 ContextVar → trace_id 丢失")


# ══════════════════════════════════════════════
# Group 4: 修复方案演示（Fix Demos）
# ══════════════════════════════════════════════

async def test_fix_save_restore_otel_context():
    """
    修复1: 在 handler 返回前保存 OTel context，stream callback 内手动 attach/detach
    适用：Sanic response.stream() 回调场景（最精简的修复）

    在实际 Sanic 代码中：
        @app.get('/api/chat/stream')
        async def chat_stream(request):
            saved_ctx = context_api.get_current()   # ← 关键：handler 内保存 context
            async def stream_body(response):
                token = context_api.attach(saved_ctx)   # ← 恢复 context
                try:
                    async for chunk in llm.stream('...'):
                        logger.info('chunk')  # 现在有 trace_id
                        await response.write(chunk)
                finally:
                    context_api.detach(token)
            return sanic.response.ResponseStream(stream_body, content_type='text/event-stream')
    """
    _, tracer, logger, capture = make_test_env()

    fixed_fields: dict = {}

    span = tracer.start_span("POST /api/chat/stream [fix-1]")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)

    # 关键：在 handler 内（context attach 期间）保存 context
    saved_ctx = context_api.get_current()

    # 模拟 _after_response → detach
    span.end()
    context_api.detach(token)

    # stream callback（已在 middleware detach 之后）
    async def stream_body_fixed(response):
        restore_token = context_api.attach(saved_ctx)   # ← 修复点
        try:
            for chunk in ["Token1", " Token2", " Token3"]:
                await asyncio.sleep(0)
                logger.info(f"[fix-1] chunk: {chunk!r}")
            fixed_fields.update(capture.get_trace_fields(-1))
        finally:
            context_api.detach(restore_token)

    await stream_body_fixed(None)

    report("test_fix_save_restore_otel_context", has_trace(fixed_fields), fixed_fields,
           "修复1: handler 内 get_current() → callback 内 attach() 恢复")


async def test_fix_copy_context_snapshot():
    """
    修复2: contextvars.copy_context() 快照 + asyncio.create_task(context=snapshot)
    适用：同时需要保留多个 ContextVar（不仅限于 OTel）的场景

    重要：copy_context().run() 是同步的，不能直接 await 异步函数。
    正确做法是用 asyncio.create_task(coro, context=snapshot) 在快照上下文中执行协程。

    在实际 Sanic 代码中：
        @app.get('/api/chat/stream')
        async def chat_stream(request):
            ctx_snapshot = contextvars.copy_context()   # ← 快照所有 ContextVar
            async def stream_body(response):
                async def _inner():
                    async for chunk in llm.stream('...'):
                        logger.info('chunk')  # 有 trace_id
                        await response.write(chunk)
                # 在快照上下文中作为 Task 运行（context= 参数，Python 3.7+）
                await asyncio.create_task(_inner(), context=ctx_snapshot)
            return sanic.response.ResponseStream(stream_body, content_type='text/event-stream')
    """
    _, tracer, logger, capture = make_test_env()

    fixed_fields: dict = {}

    span = tracer.start_span("POST /api/chat/stream [fix-2]")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)

    # 关键：快照当前所有 ContextVar（含 OTel 和业务自定义 ContextVar）
    ctx_snapshot = contextvars.copy_context()

    # 模拟 _after_response → detach
    span.end()
    context_api.detach(token)

    # stream callback
    async def stream_body_fixed(response):
        async def _inner():
            for chunk in ["Token1", " Token2", " Token3"]:
                await asyncio.sleep(0)
                logger.info(f"[fix-2] chunk: {chunk!r}")
            fixed_fields.update(capture.get_trace_fields(-1))

        # ← 修复点：在快照上下文中作为 Task 运行（不能用 ctx.run() await 异步函数）
        await asyncio.create_task(_inner(), context=ctx_snapshot)

    await stream_body_fixed(None)

    report("test_fix_copy_context_snapshot", has_trace(fixed_fields), fixed_fields,
           "修复2: copy_context() 快照 → create_task(context=snapshot) 在快照中执行")


async def test_fix_create_task_with_context():
    """
    修复3: asyncio.create_task(coro, context=copy_context()) 显式传递 ContextVar 快照
    适用：需要在后台任务中处理 LLM chunk 且保持 trace_id 的场景
    要求：Python 3.11+（context 参数从 3.7 可用，但 copy_context 传入从 3.11 更稳定）

    在实际代码中：
        ctx_snapshot = contextvars.copy_context()  # 在 context 仍有效时快照
        tasks = [
            asyncio.create_task(process_chunk(c), context=ctx_snapshot)
            for c in chunks
        ]
        await asyncio.gather(*tasks)
    """
    _, tracer, logger, capture = make_test_env()

    task_fields: list[dict] = []

    async def process_chunk(chunk: str) -> None:
        logger.info(f"[fix-3 task] chunk: {chunk!r}")
        task_fields.append(capture.get_trace_fields(-1))

    span = tracer.start_span("POST /api/chat/stream [fix-3]")
    otel_ctx = trace.set_span_in_context(span)
    token = context_api.attach(otel_ctx)
    try:
        ctx_snapshot = contextvars.copy_context()  # 关键：在 attach 期间快照
        chunks = ["Token1", " Token2", " Token3"]
        tasks = [
            asyncio.create_task(process_chunk(c), context=ctx_snapshot)   # ← 修复点
            for c in chunks
        ]
        await asyncio.gather(*tasks)
    finally:
        span.end()
        context_api.detach(token)

    all_have_trace = all(has_trace(f) for f in task_fields)
    fields = task_fields[0] if task_fields else {}
    report("test_fix_create_task_with_context", all_have_trace, fields,
           f"修复3: create_task(context=copy_context()) → {len(task_fields)}个 task 均有 trace_id")


# ══════════════════════════════════════════════
# Group 5: SDK 改造后的零侵入验证
# ══════════════════════════════════════════════

async def test_sdk_streaming_fix_zero_invasiveness():
    """
    验证：SDK 改造（BaseHTTPResponse.send class-level patch）后
    用户代码无需任何改动，流式 callback 内的日志自动有 trace_id。

    改造要点：
    1. _before_request() 中设置 _REQUEST_TRACE ContextVar（存储 span + token）
    2. _after_response() 中不再调用 span.end() + context_api.detach()
    3. send(end_stream=True) 时才执行清理，保持 ContextVar 在整个流式期间有效

    由于 streaming_fn 与 handler 在同一 asyncio Task 中运行（Sanic 直接 await），
    只要不提前 detach，ContextVar 天然保持有效。
    """
    from log_middleware.middleware import _REQUEST_TRACE, _TraceCleanupInfo

    _, tracer, logger, capture = make_test_env()

    stream_fields: list[dict] = []

    # ── 步骤 1: _before_request → attach + 设置 _REQUEST_TRACE ──
    span = tracer.start_span("POST /api/chat/stream [sdk-fix]")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    _REQUEST_TRACE.set(_TraceCleanupInfo(span=span, token=token))  # 新中间件行为

    # ── 步骤 2: _after_response → 设置属性，但不 detach（新行为）──
    # 仅设置 X-Trace-Id header 和 span 属性，不调用 span.end() / detach()
    # （此处省略 header 设置，只体现不 detach 的核心改变）

    # ── 步骤 3: streaming_fn 运行（同一 Task，ContextVar 仍有效）──
    async def streaming_fn_user_code(response):
        """用户代码：不需要任何 attach/detach，直接记日志"""
        for chunk in ["Token1", " Token2", " Token3"]:
            await asyncio.sleep(0)
            logger.info(f"[user] 发送 chunk: {chunk!r}")  # 不需要任何修改！
            stream_fields.append(capture.get_trace_fields(-1))

    await streaming_fn_user_code(None)

    # ── 步骤 4: send(end_stream=True) → 触发 _traced_send 清理 ──
    info = _REQUEST_TRACE.get()
    if info and not info.done:
        info.done = True
        info.span.end()
        context_api.detach(info.token)

    all_have_trace = all(has_trace(f) for f in stream_fields)
    fields = stream_fields[0] if stream_fields else {}
    report(
        "test_sdk_streaming_fix_zero_invasiveness",
        all_have_trace,
        fields,
        f"SDK 改造后：{len(stream_fields)}个 chunk 均有 trace_id，用户零感知",
    )


async def test_concurrent_streaming_no_cross_contamination():
    """
    验证：两个并发流式"请求"的 trace_id 严格隔离，互不串号。

    核心保证：
    - _REQUEST_TRACE 是 ContextVar（asyncio Task 级别隔离）
    - 每个 Task（请求）有自己独立的 _TraceCleanupInfo 实例
    - 并发时 Task A 读/写的是 Task A 的 ContextVar 副本，不影响 Task B
    """
    from log_middleware.middleware import _REQUEST_TRACE, _TraceCleanupInfo

    _, tracer, logger_a, capture_a = make_test_env()
    _, tracer_b, logger_b, capture_b = make_test_env()

    results: dict[str, list[dict]] = {"a": [], "b": []}

    async def simulate_streaming_request(
        req_label: str,
        t: trace.Tracer,
        lg: logging.Logger,
        cap: LogCapture,
        result_list: list,
    ):
        """模拟一个完整的流式请求生命周期（使用 SDK 改造后的新行为）"""
        # _before_request
        span = t.start_span(f"POST /api/chat/{req_label}")
        ctx_obj = trace.set_span_in_context(span)
        tok = context_api.attach(ctx_obj)
        _REQUEST_TRACE.set(_TraceCleanupInfo(span=span, token=tok))

        # _after_response（不 detach）
        # ...（略去 header 设置）

        # streaming_fn（用户代码，无需改动）
        for chunk in ["A1", "A2", "A3"] if req_label == "request-a" else ["B1", "B2", "B3"]:
            await asyncio.sleep(0)  # 让出控制权，模拟交错执行
            lg.info(f"[{req_label}] chunk: {chunk}")
            result_list.append(cap.get_trace_fields(-1))

        # send(end_stream=True) 触发的清理
        info = _REQUEST_TRACE.get()
        if info and not info.done:
            info.done = True
            info.span.end()
            context_api.detach(info.token)

    # 并发运行两个请求
    await asyncio.gather(
        simulate_streaming_request("request-a", tracer, logger_a, capture_a, results["a"]),
        simulate_streaming_request("request-b", tracer_b, logger_b, capture_b, results["b"]),
    )

    # 验证：A 的 trace_id 自身一致，B 的 trace_id 自身一致，A ≠ B
    ids_a = [f["trace_id"] for f in results["a"]]
    ids_b = [f["trace_id"] for f in results["b"]]
    a_consistent = len(set(ids_a)) == 1 and bool(ids_a[0])
    b_consistent = len(set(ids_b)) == 1 and bool(ids_b[0])
    no_cross = ids_a[0] != ids_b[0]

    passed = a_consistent and b_consistent and no_cross
    fields = results["a"][0] if results["a"] else {}
    report(
        "test_concurrent_streaming_no_cross_contamination",
        passed,
        fields,
        f"A trace_id={ids_a[0][:8]}... B trace_id={ids_b[0][:8]}... 不串号: {no_cross}",
    )


# ══════════════════════════════════════════════
# Group 6: handler 内并发调度模式（gather / create_task / run_in_executor）
# ══════════════════════════════════════════════

async def test_gather_coroutines_in_handler():
    """
    场景14: handler 内 asyncio.gather(coro1(), coro2()) —— 直接 gather 协程（无 create_task）
    原理: 协程在同一 asyncio Task 内 await，ContextVar 天然共享
    预期: 所有 gather 分支的日志都有 trace_id
    """
    _, tracer, logger, capture = make_test_env()

    async def sub_task(label):
        for _ in range(2):
            await asyncio.sleep(0)
            logger.info(f"[gather:{label}] tick")

    span = tracer.start_span("POST /api/parallel")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        await asyncio.gather(sub_task("a"), sub_task("b"), sub_task("c"))
    finally:
        span.end()
        context_api.detach(token)

    all_have_trace = all(
        bool(capture.get_trace_fields(i)["trace_id"])
        for i in range(len(capture.records))
    )
    fields = capture.get_trace_fields(0)
    report(
        "test_gather_coroutines_in_handler",
        all_have_trace,
        fields,
        f"{len(capture.records)} 条日志（3 分支 × 2 条）全部有 trace_id",
    )


async def test_create_task_inside_active_context():
    """
    场景15: handler 内 asyncio.create_task(coro) —— 在 context attach 期间创建
    原理: CPython 的 create_task 会自动 copy_context()，Task 私有 context 中含 OTel span
    预期: 所有 Task 内的日志都有 trace_id（用户无需显式传 context）

    这与"Group 2 test_background_task_created_after_detach"的区别：
    - 这里 create_task 在 attach 期间调用（handler 生命周期内）→ 有 trace ✅
    - Group 2 是 detach 之后才 create_task → 无 trace
    """
    _, tracer, logger, capture = make_test_env()

    task_fields: list[dict] = []

    async def bg_worker(label):
        asyncio.sleep(0)
        logger.info(f"[bg-task:{label}] running")
        task_fields.append(capture.get_trace_fields(-1))

    span = tracer.start_span("POST /api/batch")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        # 在 handler 内正常创建 Task —— 用户完全无感知
        tasks = [asyncio.create_task(bg_worker(str(i))) for i in range(3)]
        await asyncio.gather(*tasks)
    finally:
        span.end()
        context_api.detach(token)

    all_have_trace = all(has_trace(f) for f in task_fields)
    ids = [f["trace_id"] for f in task_fields]
    fields = task_fields[0] if task_fields else {}
    report(
        "test_create_task_inside_active_context",
        all_have_trace and len(set(ids)) == 1,
        fields,
        f"3个 Task 均有相同 trace_id: {len(set(ids)) == 1 and bool(ids[0])}",
    )


async def test_run_in_executor_transparent_after_patch():
    """
    场景16: SDK 全局 patch BaseEventLoop.run_in_executor 后，用户零改动
    → 原生 `loop.run_in_executor(None, sync_fn)` 在线程内也自动有 trace_id。

    原理: patch 后自动 `contextvars.copy_context()` 快照当前 asyncio Task 的
         ContextVar，并用 `ctx.run(fn, *args)` 在线程内恢复。

    调用前需触发 patch（`run_all()` 会在 Group 6 之前调用 _patch_run_in_executor_once）
    """
    _, tracer, logger, capture = make_test_env()

    thread_fields: dict = {}

    def sync_worker():
        logger.info("[thread] 同步 LLM 调用")
        thread_fields.update(capture.get_trace_fields(-1))
        return "LLM response"

    span = tracer.start_span("POST /api/sync-llm")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    try:
        loop = asyncio.get_event_loop()
        # 用户完全不需要改动，直接用原生 run_in_executor
        await loop.run_in_executor(None, sync_worker)
    finally:
        span.end()
        context_api.detach(token)

    report(
        "test_run_in_executor_transparent_after_patch",
        has_trace(thread_fields),
        thread_fields,
        "patch 后：原生 loop.run_in_executor 自动继承 trace（用户零感知）",
    )


# ══════════════════════════════════════════════
# Group 7: 防御性测试 —— 边界失败场景的兜底
# ══════════════════════════════════════════════

class _MockRequest:
    """轻量模拟 Sanic Request，供 middleware._after_response 使用"""
    class _Ctx: pass
    def __init__(self):
        self.ctx = self._Ctx()


class _MockResponse:
    """轻量模拟 Sanic Response，供 middleware._after_response 使用"""
    def __init__(self, status=200):
        self.status = status
        self.headers = {}


async def test_defense_after_response_cleanup_when_no_response():
    """
    防御测试 1: handler 抛异常未产生响应时 (response=None)，
    send 永远不会被调用 → _after_response 必须兜底清理，防止 span 泄漏
    """
    from log_middleware.middleware import (
        SanicTraceMiddleware, _REQUEST_TRACE, _TraceCleanupInfo,
    )

    _, tracer, _, _ = make_test_env()

    # 构造一个已 attach 但尚未清理的请求上下文
    span = tracer.start_span("POST /api/broken")
    ctx = trace.set_span_in_context(span)
    token = context_api.attach(ctx)
    info = _TraceCleanupInfo(span=span, token=token)
    _REQUEST_TRACE.set(info)

    mock_req = _MockRequest()
    mock_req.ctx.otel_span = span
    mock_req.ctx.otel_token = token

    # 模拟中间件被创建 (触发 patch 一次), 但用一个未经初始化的实例调用 _after_response
    mw = SanicTraceMiddleware.__new__(SanicTraceMiddleware)
    await mw._after_response(mock_req, None)  # response=None → 兜底触发

    # 断言：立即清理已发生
    span_ended = getattr(span, "_end_time", None) is not None
    passed = info.done and span_ended
    # 手动 detach 一次以恢复 ContextVar（避免影响后续测试）
    # 注意：info.done 之后 detach 已在 _after_response 内做过，这里不再重复
    report(
        "test_defense_after_response_cleanup_when_no_response",
        passed,
        {"trace_id": format(span.get_span_context().trace_id, "032x"),
         "span_id": format(span.get_span_context().span_id, "016x"),
         "parent_span_id": ""},
        f"response=None 时兜底：info.done={info.done}, span 已 end={span_ended}",
    )


async def test_defense_after_response_fallback_when_send_patch_disabled():
    """
    防御测试 2: send patch 未生效（模拟 Sanic 不兼容）→ 有 response 的正常请求
    也必须由 _after_response 立即清理（退回旧逻辑）
    """
    import log_middleware.middleware as mw_mod
    from log_middleware.middleware import (
        SanicTraceMiddleware, _REQUEST_TRACE, _TraceCleanupInfo,
    )

    _, tracer, _, _ = make_test_env()

    # 临时禁用 send patch（模拟 Sanic 不兼容）
    original_active = mw_mod._send_patch_active
    mw_mod._send_patch_active = False
    try:
        span = tracer.start_span("GET /api/normal")
        ctx = trace.set_span_in_context(span)
        token = context_api.attach(ctx)
        info = _TraceCleanupInfo(span=span, token=token)
        _REQUEST_TRACE.set(info)

        mock_req = _MockRequest()
        mock_req.ctx.otel_span = span
        mock_req.ctx.otel_token = token
        mock_resp = _MockResponse(status=200)

        mw = SanicTraceMiddleware.__new__(SanicTraceMiddleware)
        await mw._after_response(mock_req, mock_resp)

        span_ended = getattr(span, "_end_time", None) is not None
        passed = info.done and span_ended
        # 顺便检查 X-Trace-Id 头也被设置了
        header_set = "X-Trace-Id" in mock_resp.headers
        report(
            "test_defense_after_response_fallback_when_send_patch_disabled",
            passed and header_set,
            {"trace_id": mock_resp.headers.get("X-Trace-Id", ""),
             "span_id": format(span.get_span_context().span_id, "016x"),
             "parent_span_id": ""},
            f"send patch 关闭 → _after_response 立即清理（info.done={info.done}），"
            f"X-Trace-Id 头已设置: {header_set}",
        )
    finally:
        mw_mod._send_patch_active = original_active


async def test_defense_patch_success_keeps_delayed_cleanup():
    """
    防御测试 3: send patch 生效时（正常场景），_after_response 不应立即清理，
    保留延迟到 send(end_stream=True) 才清理的行为
    """
    import log_middleware.middleware as mw_mod
    from log_middleware.middleware import (
        SanicTraceMiddleware, _REQUEST_TRACE, _TraceCleanupInfo,
    )

    _, tracer, _, _ = make_test_env()

    # 确保 send patch 处于生效状态
    original_active = mw_mod._send_patch_active
    mw_mod._send_patch_active = True
    try:
        span = tracer.start_span("GET /api/streaming")
        ctx = trace.set_span_in_context(span)
        token = context_api.attach(ctx)
        info = _TraceCleanupInfo(span=span, token=token)
        _REQUEST_TRACE.set(info)

        mock_req = _MockRequest()
        mock_req.ctx.otel_span = span
        mock_req.ctx.otel_token = token
        mock_resp = _MockResponse(status=200)

        mw = SanicTraceMiddleware.__new__(SanicTraceMiddleware)
        await mw._after_response(mock_req, mock_resp)

        # 关键：info.done 必须还是 False（清理被延迟）
        span_ended = getattr(span, "_end_time", None) is not None
        passed = (not info.done) and (not span_ended)
        # 收尾清理，避免影响后续测试
        span.end()
        context_api.detach(token)
        report(
            "test_defense_patch_success_keeps_delayed_cleanup",
            passed,
            {"trace_id": mock_resp.headers.get("X-Trace-Id", ""),
             "span_id": format(span.get_span_context().span_id, "016x"),
             "parent_span_id": ""},
            f"send patch 启用 → _after_response 延迟清理（info.done={info.done}, span_ended={span_ended}）",
        )
    finally:
        mw_mod._send_patch_active = original_active


# ══════════════════════════════════════════════
# 测试运行器
# ══════════════════════════════════════════════

async def run_all():
    print("\n" + "═" * 72)
    print("  日志 SDK  trace_id / span_id / parent_span_id  全场景测试")
    print("═" * 72)

    print("\n─── Group 1: 正常有 trace_id 的场景 ────────────────────────────────")
    await test_direct_context_attach()
    await test_nested_await_chain()
    await test_manual_child_span()
    await test_async_for_inside_handler()

    print("\n─── Group 2: 无 trace_id（预期行为 / 已知限制）─────────────────────")
    await test_no_request_context()
    await test_background_task_created_after_detach()

    print("\n─── Group 3: LLM 流式输出问题场景（Bug 复现）───────────────────────")
    await test_sanic_stream_callback_loses_context()
    await test_run_in_executor_loses_context()

    print("\n─── Group 4: 修复方案演示 ───────────────────────────────────────────")
    await test_fix_save_restore_otel_context()
    await test_fix_copy_context_snapshot()
    await test_fix_create_task_with_context()

    print("\n─── Group 5: SDK 改造验证（用户零感知）─────────────────────────────")
    await test_sdk_streaming_fix_zero_invasiveness()
    await test_concurrent_streaming_no_cross_contamination()

    print("\n─── Group 6: handler 内并发调度（gather / create_task / 线程池）──")
    await test_gather_coroutines_in_handler()
    await test_create_task_inside_active_context()
    # 触发全局 patch —— 模拟 SanicTraceMiddleware 初始化时对 run_in_executor 的 patch
    # 之前 Group 3 test_run_in_executor_loses_context 需要"未 patch"的 baseline，
    # 所以 patch 放在这里而非 import 时
    from log_middleware.middleware import _patch_run_in_executor_once
    _patch_run_in_executor_once()
    await test_run_in_executor_transparent_after_patch()

    print("\n─── Group 7: 防御性兜底（Sanic 兼容探测 + _after_response 兜底）─")
    await test_defense_after_response_cleanup_when_no_response()
    await test_defense_after_response_fallback_when_send_patch_disabled()
    await test_defense_patch_success_keeps_delayed_cleanup()

    # ── 汇总 ──
    print("\n" + "═" * 72)
    passed = sum(1 for _, s in RESULTS if s == "✅")
    total = len(RESULTS)
    print(f"\n  共 {total} 个测试，全部 {passed} 个符合预期\n")

    print("  ┌─────────────────────────────────────────────────────────────────")
    print("  │ 有 trace_id 的情况：")
    print("  │   1. context_api.attach() 后，在同一 asyncio Task 内记日志")
    print("  │   2. 分层 await 链（controller→service→repository）同 Task 自动继承")
    print("  │   3. tracer.start_as_current_span() 子 span，child.parent_span_id = 父 span_id")
    print("  │   4. handler 内直接 async for 迭代 LLM 流（同 Task，ContextVar 不变）")
    print("  ├─────────────────────────────────────────────────────────────────")
    print("  │ 无 trace_id 的情况（预期行为）：")
    print("  │   5. 无请求上下文（服务启动日志、全局初始化）→ 空")
    print("  │   6. context_api.detach() 之后才 create_task() → 空（无 span 可复制）")
    print("  ├─────────────────────────────────────────────────────────────────")
    print("  │ LLM 流式输出 Bug 场景：")
    print("  │   7. Sanic response.stream(callback)：")
    print("  │      _after_response(detach) 先于 callback 执行 → callback 内无 trace")
    print("  │      这是大模型流式对话 trace_id 丢失的核心原因！")
    print("  │   8. run_in_executor 线程池：线程不继承 asyncio Task ContextVar → 无 trace")
    print("  ├─────────────────────────────────────────────────────────────────")
    print("  │ 修复方案（用户侧）：")
    print("  │   9.  saved_ctx = context_api.get_current()  # handler 内保存")
    print("  │       token = context_api.attach(saved_ctx)  # callback 内恢复")
    print("  │  10.  snapshot = copy_context() → create_task(context=snapshot)")
    print("  │  11.  asyncio.create_task(coro, context=copy_context())  # 显式传递快照")
    print("  ├─────────────────────────────────────────────────────────────────")
    print("  │ SDK 改造（用户零感知，推荐）：")
    print("  │  12.  middleware 新增 _REQUEST_TRACE ContextVar + send() class-level patch")
    print("  │       _after_response 不再 detach；send(end_stream=True) 时才清理")
    print("  │       streaming_fn 与 handler 同 Task → ContextVar 天然保持有效")
    print("  │  13.  并发请求各自 Task 独立 ContextVar → trace_id 严格隔离，不串号")
    print("  │  14.  asyncio.gather(coro1(), coro2()) → 同 Task 天然共享 ContextVar")
    print("  │  15.  handler 内 asyncio.create_task(coro) → 自动 copy_context() 携带 span")
    print("  │  16.  全局 patch BaseEventLoop.run_in_executor → 原生调用自动携带 trace")
    print("  │       （patch 后用户无需改 loop.run_in_executor 的任何代码）")
    print("  └─────────────────────────────────────────────────────────────────")

    if passed < total:
        failed = total - passed
        print(f"\n  ⚠️  {failed} 个测试未通过，请检查上方输出")
        sys.exit(1)
    else:
        print("\n  所有场景测试结果均符合预期 ✅\n")


if __name__ == "__main__":
    asyncio.run(run_all())
