"""
subprocess 场景 trace_id 传递支持。

subprocess 是独立进程，不共享父进程的 ContextVar 命名空间，导致子进程日志
trace_id 为空。本模块通过 W3C traceparent 环境变量跨进程传递 trace context。

父进程侧：使用 traced_subprocess_run / traced_popen 代替标准库同名函数，
          trace context 自动注入子进程环境变量。

子进程侧：启动时调用 restore_trace_from_env()，从环境变量恢复 trace context，
          之后日志自动携带正确的 trace_id。
"""
import contextvars
import logging
import os
import subprocess
from typing import Any

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.propagators.textmap import Getter

_logger = logging.getLogger(__name__)


class _EnvGetter(Getter):
    """将 OTel 小写 header key 映射到大写环境变量名（traceparent → TRACEPARENT）。"""

    def get(self, carrier: dict, key: str) -> list[str] | None:
        val = carrier.get(key.upper())
        return [val] if val is not None else None

    def keys(self, carrier: dict) -> list[str]:
        return list(carrier.keys())


_env_getter = _EnvGetter()


def get_trace_env_vars() -> dict[str, str]:
    """从当前 OTel span 提取 trace context，序列化为环境变量 dict。

    Returns:
        {"TRACEPARENT": "00-<trace_id>-<span_id>-<flags>"}，无有效 span 时返回 {}
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return {k.upper(): v for k, v in carrier.items()}


def _build_env(env: dict[str, str] | None) -> dict[str, str]:
    base = dict(os.environ) if env is None else dict(env)
    base.update(get_trace_env_vars())
    return base


def traced_subprocess_run(
    args: Any,
    *,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """封装 subprocess.run()，自动将当前 trace context 注入子进程环境变量。

    不修改调用者传入的原始 env dict。
    子进程可调用 restore_trace_from_env() 恢复 trace context。
    """
    return subprocess.run(args, env=_build_env(env), **kwargs)


def traced_popen(
    args: Any,
    *,
    env: dict[str, str] | None = None,
    **kwargs: Any,
) -> subprocess.Popen:
    """封装 subprocess.Popen()，自动将当前 trace context 注入子进程环境变量。

    不修改调用者传入的原始 env dict。
    """
    return subprocess.Popen(args, env=_build_env(env), **kwargs)


def restore_trace_from_env() -> contextvars.Token | None:
    """从环境变量（TRACEPARENT）恢复父进程的 trace context，供子进程启动时调用。

    调用后 trace.get_current_span() 返回与父进程相同 trace_id 的 RemoteSpan，
    使子进程日志自动携带正确的 trace_id。

    Usage::

        token = restore_trace_from_env()
        try:
            # 业务逻辑，日志有 trace_id
            pass
        finally:
            if token:
                from opentelemetry import context as context_api
                context_api.detach(token)

    Returns:
        contextvars.Token 供后续 detach；TRACEPARENT 不存在或无效时返回 None。
    """
    try:
        ctx = extract(os.environ, getter=_env_getter)
        span = trace.get_current_span(ctx)
        if not span.get_span_context().is_valid:
            return None
        return context_api.attach(ctx)
    except Exception:
        _logger.warning("restore_trace_from_env: 恢复 trace context 失败，静默忽略", exc_info=True)
        return None
