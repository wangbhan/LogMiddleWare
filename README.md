# LogMiddleWare

基于 OpenTelemetry 的 Sanic 全链路追踪日志中间件 SDK。

通过注册中间件的方式，为 Sanic 服务自动注入 `trace_id`、`span_id`、`parent_span_id`，支持多服务跨进程链路传播，无需在业务代码中手动传递追踪上下文。

---

## 功能特性

- **零侵入接入**：像注册 Sanic 中间件一样一行接入，业务代码无需改动
- **自动日志注入**：所有层（controller / service / repository）的日志自动携带 `trace_id`、`span_id`、`parent_span_id`
- **跨服务链路传播**：基于 W3C TraceContext 标准，多个服务共享同一 `trace_id`
- **aiohttp 自动拦截**：出站 HTTP 请求自动注入 `traceparent` 头，无需手动调用
- **分层架构透明**：asyncio ContextVar 机制保证 controller → service → repository 全链路透传
- **自定义 Resource**：支持自定义 Span 的 resource 属性（服务版本、环境、自定义标签等）
- **可配置导出**：支持控制台、文件、不导出等多种 Span 导出方式

---

## 安装

```bash
uv add logmiddleware
# 或
pip install logmiddleware
```

**依赖要求**：Python >= 3.14

---

## 快速开始

### 最简接入

```python
import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("my-service")

# 1. 注册追踪中间件
SanicTraceMiddleware(app, service_name="my-service")

# 2. 配置日志格式（自动注入 trace 字段）
setup_trace_logging()

logger = logging.getLogger("my-service")

@app.get("/api/hello")
async def hello(request):
    logger.info("处理请求")  # 自动携带 trace_id、span_id、parent_span_id
    return sanic.response.json({"message": "hello"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, single_process=True)
```

**日志输出：**
```
[2026-07-03 16:34:37,493] INFO [trace_id=7b2ae787... span_id=4ed03c7a... parent_span_id=0000...] [my-service] 处理请求
```

---

## 多服务全链路追踪

多个服务之间通过 HTTP 调用时，`trace_id` 自动透传，无需手动注入请求头。

**service_a.py（上游服务，port 8000）**

```python
import aiohttp
import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("service-a")
SanicTraceMiddleware(app, service_name="service-a")
setup_trace_logging()

logger = logging.getLogger("service-a")

@app.get("/api/users")
async def get_users(request):
    logger.info("开始处理请求")

    # 直接调用下游服务，SDK 自动注入 traceparent 头
    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:8001/internal/data") as resp:
            data = await resp.json()

    logger.info("请求处理完成")
    return sanic.response.json({"users": data})
```

**service_b.py（下游服务，port 8001）**

```python
import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("service-b")
SanicTraceMiddleware(app, service_name="service-b")
setup_trace_logging()

logger = logging.getLogger("service-b")

@app.get("/internal/data")
async def internal_data(request):
    logger.info("收到内部请求")  # trace_id 与 service_a 完全相同
    return sanic.response.json({"items": [1, 2, 3]})
```

**日志效果：**
```
# service_a 日志
[INFO] [trace_id=4bf92f35... span_id=00f067aa... parent_span_id=0000...] [service-a] 开始处理请求

# service_b 日志（trace_id 相同，span_id 不同，parent_span_id = service_a 的 span_id）
[INFO] [trace_id=4bf92f35... span_id=b9c7c989... parent_span_id=00f067aa...] [service-b] 收到内部请求
```

---

## 分层架构（controller / service / repository）

SDK 基于 asyncio ContextVar 机制，同一请求的所有 `await` 调用链天然共享同一追踪上下文，无需任何额外配置。

```python
class UserRepository:
    async def find_all(self):
        repo_logger.info("[repo] 查询数据")    # trace_id 与 controller 层相同
        return [...]

class UserService:
    async def get_users(self):
        svc_logger.info("[service] 开始处理")  # trace_id 与 controller 层相同
        return await UserRepository().find_all()

@app.get("/api/users")
async def get_users(request):
    ctrl_logger.info("[controller] 收到请求")  # 根 span
    return sanic.response.json(await UserService().get_users())
```

所有层的 `trace_id` 和 `span_id` 完全一致，无需传参。

---

## 配置项

通过 `TraceConfig` 自定义所有行为：

```python
from log_middleware import SanicTraceMiddleware, TraceConfig, setup_trace_logging

config = TraceConfig(
    service_name="my-service",

    # Span 导出方式："none"（不导出）| "console"（打印 JSON）| "file"（写文件）| "both"
    exporter_type="none",

    # exporter_type 为 "file" 或 "both" 时指定输出路径
    log_file_path="/var/log/spans.jsonl",

    # 是否自动拦截 aiohttp 出站请求（默认 True）
    auto_instrument_aiohttp=True,

    # 自定义 Span resource 属性
    resource_attributes={
        "service.version": "1.0.0",
        "deployment.environment": "production",
    },

    # 日志格式（标准 Python logging 格式字符串）
    log_format=(
        "[%(asctime)s] %(levelname)s "
        "[trace_id=%(trace_id)s span_id=%(span_id)s parent_span_id=%(parent_span_id)s] "
        "[%(name)s] %(message)s"
    ),

    # 日志级别（默认 logging.DEBUG）
    log_level=20,  # logging.INFO
)

SanicTraceMiddleware(app, service_name="my-service", config=config)
setup_trace_logging(config=config)
```

---

## 手动创建子 Span

如需在业务层记录更细粒度的链路节点：

```python
from log_middleware import get_tracer

tracer = get_tracer(__name__)

async def my_service_method():
    with tracer.start_as_current_span("db.query") as span:
        span.set_attribute("db.table", "users")
        result = await do_query()
    return result
```

---

## 公共 API

| 名称 | 类型 | 说明 |
|------|------|------|
| `SanicTraceMiddleware` | class | 核心中间件，初始化追踪并注册到 Sanic |
| `setup_trace_logging` | function | 配置日志格式，自动注入 trace 字段 |
| `TraceConfig` | dataclass | 全局配置项 |
| `get_tracer` | function | 获取 OTel Tracer，用于手动创建子 Span |
| `inject_trace_headers` | function | 向 headers dict 注入 traceparent（非 aiohttp 场景） |
| `TraceContextFilter` | class | 日志 Filter，可单独挂载到自定义 Handler |

---

## 运行示例

```bash
# 安装依赖
uv sync

# 终端 1：启动下游服务
.venv/bin/python examples/service_b.py

# 终端 2：启动上游服务（分层架构版本）
.venv/bin/python examples/service_a_layered.py

# 终端 3：发起请求
curl http://localhost:8000/api/users
```
