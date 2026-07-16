# LogMiddleWare

基于 OpenTelemetry 的 Sanic 全链路追踪日志中间件 SDK。

通过注册中间件的方式，为 Sanic 服务自动注入 `trace_id`、`span_id`、`parent_span_id`，支持多服务跨进程链路传播，无需在业务代码中手动传递追踪上下文。

---

## 功能特性

- **零侵入接入**：像注册 Sanic 中间件一样一行接入，业务代码无需改动
- **自动日志注入**：所有层（controller / service / repository）的日志自动携带 `trace_id`、`span_id`、`parent_span_id`
- **跨服务链路传播**：基于 W3C TraceContext 标准，多个服务共享同一 `trace_id`
- **HTTP 客户端自动追踪**：支持 aiohttp、httpx、requests 客户端自动注入 traceparent 头
- **异步上下文完整追踪**：支持 asyncio.create_task 和线程池的完整链路追踪
- **分层架构透明**：asyncio ContextVar 机制保证 controller → service → repository 全链路透传
- **日志落盘**：支持将带 trace 字段的日志文本写入文件，内置按大小自动轮转，目录不存在时自动创建
- **自定义 Resource**：支持自定义 Span 的 resource 属性（服务版本、环境、自定义标签等）
- **可配置导出**：支持控制台、文件、不导出等多种 Span 导出方式
- **Sanic 日志统一接管**：使用 `from sanic.log import logger` 记录的日志自动纳入 SDK 格式，无重复输出，含 trace_id
- **零值日志优化**：自动将启动服务时的零值trace转换为空字符串，配合Filebeat实现ES存储优化

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
from log_middleware import SanicTraceMiddleware, setup_trace_logging, TraceConfig

app = Sanic("my-service")

# 1. 注册追踪中间件
SanicTraceMiddleware(app, service_name="my-service")

# 2. 配置对应文件
config = TraceConfig(
    log_file_path="logs/service.jsonl",
    log_output_path="logs/service.log"
)

# 3. 配置日志格式（自动注入 trace 字段）
setup_trace_logging(config)

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
[2026-07-03 16:34:37,493] INFO [7b2ae787... - 4ed03c7a... - ] [my-service] 处理请求
```

---

## 使用 sanic.log.logger

SDK 会自动接管 Sanic 框架自身的 logger（`sanic.root`、`sanic.access`、`sanic.server` 等），无需任何额外配置。`SanicTraceMiddleware` 初始化时自动移除 Sanic 的原生格式 handler，使框架日志统一经 root logger 的 SDK handler 输出。

```python
from sanic.log import logger as sanic_logger

@app.get("/api/hello")
async def hello(request):
    sanic_logger.info("使用 sanic.log.logger")  # 自动包含 trace_id，无重复输出
    return sanic.response.json({"message": "hello"})
```

**输出效果（请求处理期间）：**
```
[2026-07-15 13:54:05,774] INFO [9239a2bc... - 2e48d98b... - ] [sanic.root] 使用 sanic.log.logger
```

**访问日志**（`sanic.access`）也会以 SDK 格式输出，包含请求信息：
```
[2026-07-15 13:54:05,775] INFO [ -  - ] [sanic.access] 127.0.0.1:51785 GET http://localhost:8765/api/hello 200 21 1.0ms
```

若需保留 Sanic 原生格式（会产生重复输出，不推荐），可关闭此行为：
```python
config = TraceConfig(auto_configure_sanic_loggers=False)
```

---

## 零值日志处理与ES优化

### 问题背景

在服务启动时，由于没有HTTP请求上下文，OpenTelemetry的trace context无效，导致日志中的trace字段被填充为零值：
- `trace_id`: 32个零字符（`00000000000000000000000000000000`）
- `span_id`: 16个零字符（`0000000000000000`）
- `parent_span_id`: 16个零字符

这些零值日志约占总体日志的70%，对Elasticsearch存储和检索都无意义。

### 解决方案

本SDK采用**生成端转换 + 采集端过滤**的组合方案：

#### 1. 日志生成端（SDK自动处理）

SDK的`TraceContextFilter`会自动将零值trace字段转换为空字符串：

```python
# 启动日志（无trace上下文）
[2026-07-08 10:22:43,015] INFO [ -  - ] [sanic.root] Sanic v25.12.1

# 业务日志（有trace上下文）
[2026-07-08 10:23:45,123] INFO [4bf92f35... - 00f067aa... - ] [my-service] 处理请求
```

**实现机制**：当检测到trace context无效时，SDK将trace字段设置为空字符串而不是零值。

#### 2. Filebeat采集端（用户配置）

通过Filebeat的条件过滤，只将有效的业务日志采集到ES：

```yaml
processors:
  # 解析日志格式
  - dissect:
      tokenizer: '[%{ts}] %{level} [%{trace_id} - %{span_id} - %{parent_span_id}] [%{logger}] %{message}'
      field: message
      target_prefix: ""
      overwrite_keys: true
      ignore_failure: true

  # 只有当trace字段中任意一个有值时才采集到ES
  - drop_event:
      when:
        and:
          - equals:
              trace_id: ""
          - equals:
              span_id: ""
          - equals:
              parent_span_id: ""
```

**过滤逻辑**：当三个trace字段都为空时，丢弃该日志事件，不采集到ES。

### 优势效果

- **本地日志完整**: 所有日志（包括启动日志）都保存在本地文件，便于调试和审计
- **ES存储优化**: 只有包含业务trace信息的日志存入ES，节省约70%存储空间
- **查询效率**: ES中的日志都是有效的业务追踪日志，提升查询性能
- **维护简单**: 配置清晰，逻辑分离，便于维护和调试

---

## 多服务全链路追踪

多个服务之间通过 HTTP 调用时，`trace_id` 自动透传，无需手动注入请求头。

**方式一：使用自动追踪包装类（推荐）**

SDK提供了`Traced*`系列包装类，直接替换原生客户端即可自动注入traceparent：

```python
import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging, TracedClientSession

app = Sanic("service-a")
SanicTraceMiddleware(app, service_name="service-a")
setup_trace_logging()

logger = logging.getLogger("service-a")

@app.get("/api/users")
async def get_users(request):
    logger.info("开始处理请求")

    # 使用 TracedClientSession 替代原生 aiohttp.ClientSession
    async with TracedClientSession() as session:
        async with session.get("http://localhost:8001/internal/data") as resp:
            data = await resp.json()

    logger.info("请求处理完成")
    return sanic.response.json({"users": data})
```

**支持的客户端包装类：**
- `TracedClientSession` - 替代 `aiohttp.ClientSession`
- `TracedAsyncClient` - 替代 `httpx.AsyncClient`
- `TracedClient` - 替代 `httpx.Client`
- `TracedSession` - 替代 `requests.Session`

**方式二：使用全局自动追踪**

在应用启动时调用全局patch函数，所有原生客户端调用都会自动注入traceparent：

```python
from log_middleware import patch_requests

# 在应用启动时调用一次即可
patch_requests()

# 之后所有的 requests 调用都会自动注入 traceparent
import requests
response = requests.get("http://localhost:8001/internal/data")
```

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
[INFO] [4bf92f35... - 00f067aa... - ] [service-a] 开始处理请求

# service_b 日志（trace_id 相同，span_id 不同，parent_span_id = service_a 的 span_id）
[INFO] [4bf92f35... - b9c7c989... - 00f067aa...] [service-b] 收到内部请求
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

## 异步并发场景追踪

SDK 基于 OpenTelemetry 的上下文传播机制，支持多种异步并发场景的完整链路追踪。

### asyncio.create_task 追踪

显式创建的子任务会自动建立独立的 child span，维持父子关系：

```python
import asyncio
import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("async-task-service")
SanicTraceMiddleware(app, service_name="async-task-service")
setup_trace_logging()

logger = logging.getLogger("async-task-service")

@app.get("/api/parallel")
async def parallel_requests(request):
    logger.info("开始并行请求")  # 父 span

    async def fetch_user(user_id):
        # 每个任务有独立的 span_id，parent_span_id 指向父 span
        logger.info(f"获取用户 {user_id}")
        await asyncio.sleep(0.1)
        return {"id": user_id, "name": f"User{user_id}"}

    # 创建多个子任务，每个任务自动建立 child span
    tasks = [
        asyncio.create_task(fetch_user(1)),
        asyncio.create_task(fetch_user(2)),
        asyncio.create_task(fetch_user(3))
    ]

    results = await asyncio.gather(*tasks)
    logger.info("并行请求完成")
    return sanic.response.json({"users": results})
```

**日志效果：**
```
[INFO] [abc123 - parent_span - ] [async-task-service] 开始并行请求
[INFO] [abc123 - child_span_1 - parent_span] [async-task-service] 获取用户 1
[INFO] [abc123 - child_span_2 - parent_span] [async-task-service] 获取用户 2
[INFO] [abc123 - child_span_3 - parent_span] [async-task-service] 获取用户 3
[INFO] [abc123 - parent_span - ] [async-task-service] 并行请求完成
```

### 线程池追踪

使用 `loop.run_in_executor` 执行同步函数时，线程内的日志会自动继承当前的 OTel 上下文：

```python
import asyncio
import logging
from sanic import Sanic
import sanic.response
from log_middleware import SanicTraceMiddleware, setup_trace_logging

app = Sanic("executor-service")
SanicTraceMiddleware(app, service_name="executor-service")
setup_trace_logging()

logger = logging.getLogger("executor-service")

def sync_blocking_function(user_id):
    """同步阻塞函数，在线程池中执行"""
    logger.info(f"在线程中处理用户 {user_id}")  # 自动携带 trace_id
    return {"id": user_id, "processed": True}

@app.get("/api/process")
async def process_request(request):
    logger.info("开始处理请求")  # 主协程中

    loop = asyncio.get_event_loop()
    # 将同步函数提交到线程池执行
    result = await loop.run_in_executor(None, sync_blocking_function, 123)

    logger.info("处理完成")
    return sanic.response.json(result)
```

**日志效果：**
```
[INFO] [abc123 - main_span - ] [executor-service] 开始处理请求
[INFO] [abc123 - executor_span - main_span] [executor-service] 在线程中处理用户 123
[INFO] [abc123 - main_span - ] [executor-service] 处理完成
```

### 异步场景速查表

| 场景 | trace_id | span_id | parent_span_id |
|------|----------|---------|----------------|
| 普通 handler 内 `logger.info()` | ✅ | ✅ | 父 span 才有 |
| Controller → Service → Repository | ✅ 相同 | ✅ 相同 | 父 span 才有 |
| `asyncio.create_task()` 子任务 | ✅ | 子任务独立 span_id | 指向父 span |
| `loop.run_in_executor()` 线程池 | ✅ | 线程独立 span_id | 指向父 span |
| `asyncio.gather()` 并发协程 | ✅ 相同 | ✅ 相同 | 父 span 才有 |
| Sanic 流式响应回调 | ✅ | ✅ | 父 span 才有 |
| 后台任务（无请求上下文） | 空字符串 | 空字符串 | 空字符串 |

---

## 配置项

通过 `TraceConfig` 自定义所有行为：

```python
from log_middleware import SanicTraceMiddleware, TraceConfig, setup_trace_logging

config = TraceConfig(
    service_name="my-service",

    # Span 导出方式："none"（不导出）| "console"（打印 JSON）| "file"（写文件）| "both"
    exporter_type="none",

    # exporter_type 为 "file" 或 "both" 时指定 Span JSONL 输出路径
    log_file_path="/var/log/spans.jsonl",

    # 日志文本落盘：指定路径后，带 trace_id 的日志同时写入文件（目录不存在时自动创建）
    log_output_path="logs/my-service.log",
    log_max_bytes=10 * 1024 * 1024,  # 单文件上限，默认 10 MB
    log_backup_count=5,              # 保留旧文件数，默认 5 个

    # 是否自动拦截 aiohttp 出站请求（默认 True）
    auto_instrument_aiohttp=True,

    # 自定义 Span resource 属性
    resource_attributes={
        "service.version": "1.0.0",
        "deployment.environment": "production",
    },

    # 日志格式（标准 Python logging 格式字符串）
    # 注意：启动时的零值trace会自动转换为空字符串，便于ES采集端过滤
    log_format=(
        "[%(asctime)s] %(levelname)s "
        "[%(trace_id)s - %(span_id)s - %(parent_span_id)s] "
        "[%(name)s] %(message)s"
    ),

    # 日志级别（默认 logging.DEBUG）
    log_level=20,  # logging.INFO

    # 自动接管 sanic.* loggers（默认 True，消除 Sanic 原生格式重复输出）
    # 设为 False 可保留 Sanic 原生格式，但会产生双重日志输出
    auto_configure_sanic_loggers=True,
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
| `inject_trace_headers` | function | 向 headers dict 注入 traceparent（非自动追踪场景） |
| `TraceContextFilter` | class | 日志 Filter，可单独挂载到自定义 Handler |
| `TracedClientSession` | class | aiohttp.ClientSession 包装类，自动注入 traceparent |
| `TracedAsyncClient` | class | httpx.AsyncClient 包装类，自动注入 traceparent |
| `TracedClient` | class | httpx.Client 包装类（同步），自动注入 traceparent |
| `TracedSession` | class | requests.Session 包装类（同步），自动注入 traceparent |
| `patch_requests` | function | 全局 patch requests 客户端，自动注入 traceparent |

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
