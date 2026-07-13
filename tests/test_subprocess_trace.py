"""
tests/test_subprocess_trace.py

测试 log_middleware.subprocess_integration 的全部公共函数。

运行方式：
    .venv/bin/python tests/test_subprocess_trace.py
    .venv/bin/python -m pytest tests/test_subprocess_trace.py -v
"""
import os
import sys
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from log_middleware.subprocess_integration import (
    get_trace_env_vars,
    restore_trace_from_env,
    traced_popen,
    traced_subprocess_run,
)


# ─────────────────────────────────────────────
# 测试基础设施
# ─────────────────────────────────────────────

def setup_otel() -> trace.Tracer:
    """每个测试前调用，重置全局 OTel 状态，避免测试间污染。"""
    import log_middleware.provider as _p
    _p._provider = None

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    set_global_textmap(TraceContextTextMapPropagator())
    trace.set_tracer_provider(provider)
    return provider.get_tracer("test-subprocess")


_RESULTS: list[tuple[str, bool]] = []


def _report(name: str, passed: bool, note: str = "") -> None:
    icon = "✅" if passed else "❌"
    _RESULTS.append((name, passed))
    suffix = f"  ← {note}" if note else ""
    print(f"  {icon}  {name}{suffix}")


# ═════════════════════════════════════════════
# 测试 1：有效 span → get_trace_env_vars 返回 TRACEPARENT
# ═════════════════════════════════════════════

def test_get_trace_env_vars_with_valid_span():
    tracer = setup_otel()

    with tracer.start_as_current_span("test-span") as span:
        expected_trace_id = format_trace_id(span.get_span_context().trace_id)
        result = get_trace_env_vars()

    passed = (
        "TRACEPARENT" in result
        and expected_trace_id in result["TRACEPARENT"]
        and result["TRACEPARENT"].startswith("00-")
    )
    _report(
        "test_get_trace_env_vars_with_valid_span",
        passed,
        f"TRACEPARENT={result.get('TRACEPARENT', '(absent)')!r}",
    )
    return passed


# ═════════════════════════════════════════════
# 测试 2：无 span → get_trace_env_vars 返回空字典
# ═════════════════════════════════════════════

def test_get_trace_env_vars_no_span():
    setup_otel()
    result = get_trace_env_vars()

    passed = result == {}
    _report(
        "test_get_trace_env_vars_no_span",
        passed,
        f"result={result!r}（预期空字典）",
    )
    return passed


# ═════════════════════════════════════════════
# 测试 3：traced_subprocess_run 将 TRACEPARENT 注入子进程
# ═════════════════════════════════════════════

def test_traced_subprocess_run_injects_traceparent():
    tracer = setup_otel()

    with tracer.start_as_current_span("inject-test") as span:
        parent_trace_id = format_trace_id(span.get_span_context().trace_id)
        result = traced_subprocess_run(
            [
                sys.executable,
                "-c",
                "import os,sys; sys.stdout.write(os.environ.get('TRACEPARENT','MISSING'))",
            ],
            capture_output=True,
            text=True,
        )

    child_output = result.stdout.strip()
    passed = (
        result.returncode == 0
        and child_output != "MISSING"
        and parent_trace_id in child_output
    )
    _report(
        "test_traced_subprocess_run_injects_traceparent",
        passed,
        f"子进程 TRACEPARENT={child_output!r}",
    )
    return passed


# ═════════════════════════════════════════════
# 测试 4：env=None 时不报错，子进程仍继承 os.environ
# ═════════════════════════════════════════════

def test_traced_subprocess_run_env_none():
    setup_otel()
    home = os.environ.get("HOME", "")

    result = traced_subprocess_run(
        [
            sys.executable,
            "-c",
            "import os,sys; sys.stdout.write(os.environ.get('HOME','NOT_FOUND'))",
        ],
        env=None,
        capture_output=True,
        text=True,
    )

    passed = result.returncode == 0 and result.stdout.strip() == home
    _report(
        "test_traced_subprocess_run_env_none",
        passed,
        f"HOME={result.stdout.strip()!r}（预期 {home!r}）",
    )
    return passed


# ═════════════════════════════════════════════
# 测试 5：调用者传入的 env dict 不被修改
# ═════════════════════════════════════════════

def test_traced_subprocess_run_does_not_mutate_caller_env():
    tracer = setup_otel()

    caller_env = {"CUSTOM_VAR": "hello", "PATH": os.environ.get("PATH", "")}
    original_copy = dict(caller_env)

    with tracer.start_as_current_span("mutation-test"):
        traced_subprocess_run(
            [sys.executable, "-c", "pass"],
            env=caller_env,
            capture_output=True,
        )

    passed = caller_env == original_copy and "TRACEPARENT" not in caller_env
    _report(
        "test_traced_subprocess_run_does_not_mutate_caller_env",
        passed,
        f"caller_env 未被修改: {passed}",
    )
    return passed


# ═════════════════════════════════════════════
# 测试 6：restore_trace_from_env 从 TRACEPARENT 恢复 trace context
# ═════════════════════════════════════════════

def test_restore_trace_from_env_valid_traceparent():
    setup_otel()

    known_traceparent = "00-a1b2c3d4e5f6789012345678901234ab-abcdef1234567890-01"
    expected_trace_id = "a1b2c3d4e5f6789012345678901234ab"

    with unittest.mock.patch.dict(os.environ, {"TRACEPARENT": known_traceparent}):
        token = restore_trace_from_env()
        try:
            if token is None:
                passed, note = False, "token 为 None，恢复失败"
            else:
                sc = trace.get_current_span().get_span_context()
                actual = format_trace_id(sc.trace_id)
                passed = sc.is_valid and sc.is_remote and actual == expected_trace_id
                note = f"trace_id={actual!r}, is_remote={sc.is_remote}"
        finally:
            if token is not None:
                context_api.detach(token)

    _report("test_restore_trace_from_env_valid_traceparent", passed, note)
    return passed


# ═════════════════════════════════════════════
# 测试 7：无 TRACEPARENT → restore 返回 None 且不抛异常
# ═════════════════════════════════════════════

def test_restore_trace_from_env_no_traceparent():
    setup_otel()

    clean_env = {k: v for k, v in os.environ.items() if k != "TRACEPARENT"}
    with unittest.mock.patch.dict(os.environ, clean_env, clear=True):
        try:
            token = restore_trace_from_env()
            passed = token is None
            note = f"token={token!r}（预期 None）"
        except Exception as exc:
            passed = False
            note = f"抛出异常：{exc!r}"

    _report("test_restore_trace_from_env_no_traceparent", passed, note)
    return passed


# ═════════════════════════════════════════════
# 测试 8：端到端 — 父 span 注入 → restore → trace_id 一致
# ═════════════════════════════════════════════

def test_end_to_end_trace_propagation():
    tracer = setup_otel()

    # 父进程侧：注入 trace context
    with tracer.start_as_current_span("parent-request") as parent_span:
        parent_trace_id = format_trace_id(parent_span.get_span_context().trace_id)
        env_vars = get_trace_env_vars()

    # 父 span 已退出，模拟子进程独立恢复
    with unittest.mock.patch.dict(os.environ, env_vars):
        token = restore_trace_from_env()
        try:
            if token is None:
                passed, note = False, "restore 返回 None"
            else:
                sc = trace.get_current_span().get_span_context()
                child_trace_id = format_trace_id(sc.trace_id)
                passed = child_trace_id == parent_trace_id and sc.is_valid and sc.is_remote
                note = (
                    f"parent={parent_trace_id[:12]}... "
                    f"== child={child_trace_id[:12]}..."
                )
        finally:
            if token is not None:
                context_api.detach(token)

    _report("test_end_to_end_trace_propagation", passed, note)
    return passed


# ─────────────────────────────────────────────
# 也支持 pytest 收集（每个函数符合 test_ 前缀规范）
# ─────────────────────────────────────────────

def run_all() -> None:
    print("\n" + "═" * 68)
    print("  subprocess_integration 全场景测试")
    print("═" * 68)

    fns = [
        test_get_trace_env_vars_with_valid_span,
        test_get_trace_env_vars_no_span,
        test_traced_subprocess_run_injects_traceparent,
        test_traced_subprocess_run_env_none,
        test_traced_subprocess_run_does_not_mutate_caller_env,
        test_restore_trace_from_env_valid_traceparent,
        test_restore_trace_from_env_no_traceparent,
        test_end_to_end_trace_propagation,
    ]
    results = [fn() for fn in fns]

    total = len(results)
    passed = sum(results)
    print("\n" + "═" * 68)
    print(f"\n  共 {total} 个测试，{passed} 个通过，{total - passed} 个失败\n")

    if passed < total:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
