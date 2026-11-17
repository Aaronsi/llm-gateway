# week01/llm-gateway 学习指南 —— 从全局到局部的分层架构认知路线

> **写给刚毕业的你**：这份指南不假设你有网关开发经验。我们通过"一个请求的旅程"来认识这个项目，最终目标是：**闭上眼睛能画出架构图，说出每个模块管什么、为什么存在、错了会怎样**。
>
> **提示**：`docs/` 下的设计文档与对比文档描述的是重构早期状态；当前代码已在其基础上演进（新增了认证、限流、SQLite 存储、流检查点、加权轮询等）。学习时**以代码为准**，文档用于理解动机。

---

## 一、【全景视图】

### 1.1 这个项目是干什么的？

一句话：**一个"统一模型入口"**——你的应用（Agent）不用关心背后到底是 deepseek 还是别的模型，只需把请求交给网关，网关负责：选模型、翻译协议、失败重试、换备用模型、校验输出格式、记账审计。

类比：它像一家**餐厅的总台点餐系统**。客人（调用方）只说要什么菜（请求），总台决定哪个厨师（模型路由）来做、菜送错格式了要不要补救（结构化输出修复）、并记下每一单（审计）。

### 1.2 目录地图（先建立"位置感"）

```
week01/llm-gateway/
├── config.yaml                  # 一切的"旋钮"：模型、价格、重试、限流、存储
├── prompts/                     # Prompt 模板资产（YAML，可版本化）
├── app/
│   ├── app.py                   # ★ 入口：装配工厂 create_app()，唯一允许跨层接线的地方
│   ├── api/                     # 第 1 层：路由 + 协议翻译（门面）
│   │   ├── routes.py            #   端点定义、认证/限流依赖
│   │   ├── schemas_openai.py    #   OpenAI 形状 ↔ 内部模型 的翻译
│   │   ├── streaming.py         #   内部事件 → SSE / OpenAI chunk 两种"方言"
│   │   └── errors.py            #   GatewayError → HTTP 响应
│   ├── service/                 # 第 2 层：业务编排（指挥部）
│   │   ├── llm_service.py       #   ★ 核心编排：路由计划执行、重试/降级、L4 校验、审计
│   │   └── retry.py             #   重试策略（纯函数）
│   └── core/                    # 第 3 层：基础设施抽象（工具箱）
│       ├── schemas.py           #   ★ 数据契约（领域模型）
│       ├── config.py            #   ★ 配置模型 + 启动校验
│       ├── router.py            #   ★ 路由纯决策
│       ├── health.py            #   熔断器（健康状态机）
│       ├── limits.py            #   并发守卫
│       ├── providers/           #   供应商适配器（协议 + OpenAI 实现）
│       ├── validation.py        #   L4 校验 + 有界修复
│       ├── audit.py / metrics.py#   审计与指标
│       ├── security.py / rate_limit.py  # 认证 / 限流
│       ├── storage.py / checkpoint.py   # SQLite 持久化 / 流检查点
│       └── prompts.py / prompt_activation.py / logging.py / errors.py / capabilities.py
└── tests/                       # 26 个测试文件，129+ 用例——最好的"说明书"
```

### 1.3 三条必须记住的"铁律"

1. **依赖单向**：`api → service → core`。core 不认识 service/api，service 不认识 api。
2. **装配点唯一**：跨层对象接线只发生在 `app/app.py` 的 `create_app()`。
3. **决策与执行分离**：路由、退避计算等是**纯函数**（同输入必同输出）；真正发请求、睡觉等副作用在 service 层。

---

## 二、【核心概念与架构图】

### 2.1 一次请求穿越三层的完整旅程

```
 调用方（Agent / OpenAI SDK）
        │  POST /v1/llm  {model, messages, response_schema, ...}
        ▼
┌─────────────────────────── api 层（门面：接待 + 翻译）───────────────────────────┐
│ ① 认证（api_keys 配置了才校验，hmac 常量时间比较）→ 失败 invalid_api_key(401)    │
│ ② 每身份限流（令牌桶）→ 超限 429                                                │
│ ③ Pydantic 校验请求体 → 非法 422（含 stream+response_schema 互斥）               │
│ ④ OpenAI 形状 ↔ LLMRequest 翻译（to_internal）                                   │
│ ⑤ 响应反向翻译 + 关联 ID 响应头（X-Request-Id / X-Trace-Id）                     │
└─────────────────────────────────┬────────────────────────────────────────────────┘
                                  │ LLMRequest（内部统一语言）
                                  ▼
┌─────────────────────────── service 层（指挥部：编排）────────────────────────────┐
│ ⑥ 落定 trace_id/call_id；开 start 计时 → deadline = now + timeout_seconds（总预算）│
│ ⑦ Prompt 引擎：选包 → 变量校验 → 沙箱渲染 → 预算检查（超限 400）                 │
│ ⑧ ModelRouter.plan()：显式模型硬校验 → 能力/健康过滤 → priority/cost 排序        │
│      → RoutingDecision 全量留痕（谁被拒、为什么）                                │
│ ⑨ 逐候选执行：                                                                    │
│     CapacityGuard.acquire（池满→换候选；全局满→429）→ 剩余预算检查（不足→429）    │
│     → provider.complete()【内部：重试 ≤2 次，指数退避 + jitter】                  │
│     → L3 协议检查 → L4 校验 + 有界修复（失败不重试不降级）                        │
│ ⑩ 全程维护 CallTrace（用量/延迟/弹性/成本）→ audit + metrics                     │
└─────────────────────────────────┬────────────────────────────────────────────────┘
                                  │ ProviderCall（适配器数据类）
                                  ▼
┌─────────────────────────── core 层（工具箱：决策与适配）─────────────────────────┐
│ router（纯决策）· health（熔断）· limits（容量）· validation（校验）               │
│ audit · metrics · storage（SQLite）· prompts · security · rate_limit              │
│ providers：OpenAICompatibleProvider                                             │
│    - L1/L2：SDK 异常翻译成分层异常（带 retryable / fallback_allowed 属性）        │
│    - 客户端按 (base_url, api_key) 缓存 → 连接池复用                              │
└─────────────────────────────────┬────────────────────────────────────────────────┘
                                  │ HTTPS（复用连接池）
                                  ▼
                        DeepSeek 等上游服务
```

### 2.2 四层检查点（L1–L4）：网关的"安检体系"

这是本项目错误模型的灵魂。请求从网关出发到拿到结果，依次经过四道安检：

| 关卡 | 查什么 | 谁负责 | 失败后可以做什么 |
|---|---|---|---|
| **L1 网络** | DNS/连接/超时 | adapter 捕获 SDK 异常 | ✅ 重试 ✅ 换模型 |
| **L2 HTTP** | 429/5xx/401/403/4xx | adapter 翻译为分层异常 | 429、5xx：✅✅；4xx：❌❌ |
| **L3 协议** | 响应结构是否合规（choices 是否为空） | adapter 响应后校验 | ❌ 重试 ✅ 换模型（同请求重试大概率复现） |
| **L4 Schema** | JSON 能否解析、是否符合 schema | service 本地校验 | ❌ 重试 ❌ 降级，只做**本地有界修复** |

> 记住一个关键设计判断：**"重试"是给"可能暂时坏"的错误（网络抖了、服务繁忙）；"换模型"是给"这个供应商不行"的错误；L4 是"模型答错了格式"，重新生成大概率还是错，且重试会双倍计费，所以只在本地"文本手术"，绝不白花上游的钱。**

### 2.3 六个高频概念的"人话版"

| 概念 | 类比 | 代码位置 |
|---|---|---|
| **熔断（Circuit Breaker）** | 家里保险丝：电器短路 → 保险丝熔断 → 冷却后才能恢复。连续失败 ≥3 次（`consecutive_failure_threshold`）→ 该模型被"拉闸"30 秒（`cooldown_seconds`），期间路由不再选它，避免雪崩 | `core/health.py` |
| **指数退避 + 抖动** | 打客服占线：第 1 次等 0.1s 重拨、第 2 次等 0.2s、第 3 次 0.4s（封顶 2s），每次再乘随机系数 [0.5,1.5]——防止一万个客户端"整点同时重拨"再次打爆服务 | `service/retry.py` |
| **全链路 deadline** | 项目总工期：给你 30 秒预算，换几个承包商（fallback）、返工几次（retry）都从这 30 秒里扣；剩 <1 秒就不开工，直接报 `insufficient_time_budget`(429) | `llm_service.py::_attempt_with_retries` |
| **结构化输出修复** | 校对三步曲：① 直接读 → ② 剥掉 ```json 围栏 → ③ 从文本里找第一对成对花括号。三步都不行才判失败（可选"请 AI 自己重写一遍"，成本高、默认关） | `core/validation.py` |
| **Facade（OpenAI 兼容面）** | 同声翻译：内部只说一种语言（`LLMRequest`/`StreamEvent`），对外按听众切换方言——OpenAI SDK 听到的是 `chat.completion.chunk` + `[DONE]` | `api/schemas_openai.py`、`api/streaming.py` |
| **连接池复用** | 常驻班车 vs 每次打车：基线每次调用 `new AsyncOpenAI()`（重新握手），新版按 `(base_url, api_key)` 缓存客户端单例，多次调用共享底层连接 | `core/providers/openai_compatible.py` |

---

## 三、【分阶段详解（含设计理由）】

### 3.1 第 0 站：`app/app.py` —— 装配车间（5 分钟读懂全局）

```python
def create_app(config=None, providers=None, prompts_dir=None, sleep=asyncio.sleep, clock=time.perf_counter):
    # ① 加载配置（YAML → AppConfig 启动校验）
    # ② 按 storage.backend 选存储实现（sqlite / memory）
    # ③ 组装 LLMService —— 把所有 core 组件作为构造参数注入（依赖注入）
    # ④ 挂载业务路由；admin_endpoints.enabled 才挂管理路由
    # ⑤ lifespan：启动初始化 SQLite → 退出时关闭所有供应商连接池（close_all_providers）
```

**读懂它的三个要点**：
1. **工厂函数 + 可注入参数**（`sleep`、`clock`、`providers`）是"可测性接缝"——测试可以传入假睡眠、假时钟、假供应商，让 service 层脱离真实 I/O 被测试。
2. `uvicorn app.app:create_app --factory` 就是这么启动的（`--factory` 表示函数返回应用对象）。
3. **为什么这样设计**：单文件时代所有组件互相可见，改一处牵全身；装配点唯一后，替换存储实现（memory↔sqlite）只需改 `_build_stores()` 一处。

### 3.2 第 1 站：api 层 —— 接待与翻译（谁都能看懂的"前台"）

**做了什么控制**：
- **认证**：`routes.py` 中 `Identity = Annotated[str, Depends(limited_identity)]` —— `authenticate` 校验 Bearer/x-api-key（`hmac.compare_digest` 防时序攻击）；未配置 `api_keys` 时是"开放模式"（按客户端 IP 记指纹）。认证 + 限流被组合成一个依赖 `limited_identity`，每个端点一行注解即生效。
- **限流**：`rate_limiter.check(identity)` 按身份令牌桶，超限 429。
- **协议翻译**：`ChatCompletionRequest.to_internal()` 把 OpenAI 的 `response_format` 翻译为内部 `response_schema`；`temperature/top_p` 等不支持字段**接受后忽略**（保证任意 OpenAI SDK 调用不炸，而非报错）。
- **流式格式化**：`to_gateway_sse()`（网关方言：`content.delta`/`response.usage`/`response.completed`/`response.failed`）与 `iter_openai_chunks()`（OpenAI 方言：`chat.completion.chunk` + 末块 usage + `[DONE]`）是同一批 `StreamEvent` 的两种编码——**协议差异被关在 api 层，service 层零感知**。

**为什么这样设计**：见 `docs/2026-11-16-llm-gateway-vs-1-7-architecture-comparison.md`——1-7 版把认证/限流散落在路由里，facade 与业务交织；分层版把"接待"和"办事"分开，新增一个对外协议只需加一个翻译模块，不动指挥部。

### 3.3 第 2 站：service 层 —— 指挥部（本项目的"心脏"，建议精读 3 遍）

核心文件 `llm_service.py`（569 行），两个入口：`complete()`（非流式）与 `stream()`（流式）。

**（A）`complete()` 的执行节奏**：

```
_prepare()：Prompt 应用 → 推导所需能力（stream→STREAMING；response_schema→STRUCTURED_OUTPUT）
          → router.plan() → RoutingDecision 留痕 → 过滤可执行候选
for plan in executable:                          # 逐候选执行
    CapacityGuard.acquire（模型池满 → 记 capacity_full → 下一个候选；全局满 → 429）
    _attempt_with_retries()：                     # 单模型内重试循环
        while attempts < max_attempts_per_model:  # 默认 2 次
            剩余预算 = deadline - now（再与 provider 超时取 min）
            剩余 < 1s → insufficient_time_budget(429)
            provider.complete(call)
            失败且 retryable → 退避等待 → 再试
            失败且 fallback_allowed → 交回外层循环换候选
    L4 校验：response_schema 存在时 validate_with_repair()
        本地修复策略逐个尝试（剥围栏/提取平衡对象）
        可选 LLM 修复循环（structured_output.repair.enabled，默认关）
    成功 → 填 CallTrace → audit + metrics → 返回
全败 → model_unavailable(502)
```

**（B）`stream()` 的三条铁律**：
1. **首块边界**：第一个 delta 发出**前**失败 → 允许重试/换模型；发出**后**失败 → 只发 `StreamFailed` 终态，**绝不重新生成、绝不拼接**（拼接两个模型的前后半句话 = 输出污染）。
2. **终态唯一**：`COMPLETED/FAILED/CANCELLED` 互斥，emit 任一终态后立即 return。
3. **断开感知**：每收到一个 delta 检查 `is_disconnected()`，客户端断开 → 记 `cancelled` 审计、关闭上游迭代、发 `StreamCancelled`；期间内容按间隔写入检查点（`/v1/streams/{id}/checkpoint` 可找回）。

**（C）retry.py —— 三行代码但值得背诵**：

```python
def is_retryable_error(exc):  return bool(getattr(exc, "retryable", False))
def allow_fallback(exc):      return bool(getattr(exc, "fallback_allowed", False))
def backoff_delay(attempt, policy):
    delay = min(policy.base_delay_seconds * (policy.backoff_factor ** (attempt - 1)),
                policy.max_delay_seconds)
    if policy.jitter:
        delay *= random.uniform(0.5, 1.5)
    return delay
```

**为什么这样设计**（对照基线缺陷，来自设计文档 §15）：

| 基线缺陷（1-6/gateway.py） | 重构后的修复 | 落点 |
|---|---|---|
| 每次调用 `new AsyncOpenAI()`，丢失连接池复用 | 客户端按 `(base_url, key)` 缓存单例 | `providers/openai_compatible.py` |
| 硬编码 `"general-backup"` 备用模型名 | 配置驱动的候选列表 + 能力路由 | `config.yaml` + `router.py` |
| 成本表 KeyError 在**调用成功后**才抛 500 | 启动校验 pricing 完整性，启动即报错 | `config.py` 校验器 |
| 5xx 一律不重试 | 异常类声明 `retryable=True` | `providers/base.py` |
| 流式审计缺 usage/TTFT | CallTrace 全字段 + `ttft_ms` + 估算 usage | `llm_service.py` |
| `timeout_seconds` 每次上游调用都能用满（总延迟无上限） | 全链路总预算 deadline | `llm_service.py` |
| 能力不足时**静默换模型**（欺骗调用方） | 显式模型硬校验：不存在→`unknown_model`(400)，能力不满足→400，绝不静默切换 | `router.py` |

### 3.4 第 3 站：core 层 —— 工具箱（每个模块都小，逐个击破）

| 模块 | 一句话职责 | 关键看什么 |
|---|---|---|
| `schemas.py` | **数据契约**：全项目共享的语言 | `LLMRequest` 的组合校验（stream+response_schema 互斥）；`StreamEvent` 判别联合；`CallTrace` 白名单字段 |
| `config.py` | YAML → 严格校验的配置对象 | `extra="forbid"`（多写一个字段就报错）；`${ENV}` 展开；`SecretStr`；**启动校验**：能力⊆adapter 能力、provider 引用存在 |
| `router.py` | **纯决策**：谁可以干、谁先干 | `plan()` 五步：显式硬校验 → 能力过滤 → 健康过滤 → priority 降序/cost 升序/声明序 → 显式模型排首位（可选加权轮询） |
| `health.py` | 熔断器 | 连续失败计数 + 冷却时钟（可注入，测试不用真等 30 秒） |
| `limits.py` | 并发守卫 | 全局池 + 每模型池；池满优雅降级而非失败 |
| `providers/base.py` | **适配器协议** + 分层异常 | `ProviderError` 的 `retryable`/`fallback_allowed` 类属性是重试/降级决策的唯一依据 |
| `validation.py` | L4 校验 + 有界修复 | Pydantic 动态校验器优先（顺带类型归一化）+ jsonschema 兜底；修复策略列表；流式 JSON 状态机 |
| `prompts.py` | Prompt 资产引擎 | Jinja2 沙箱 + AST 白名单（禁用 For/Macro/Import）+ 变量契约校验 |
| `security.py` / `rate_limit.py` | 认证指纹 / 令牌桶 | `hmac.compare_digest`；审计只落 sha256 前 16 位指纹 |
| `storage.py` / `checkpoint.py` | SQLite 五表共库 / 流检查点 | 与内存实现共享同一 Protocol 接口（可替换性） |
| `audit.py` / `metrics.py` / `logging.py` | 审计白名单 / 指标聚合 / 脱敏日志 | 不存 Prompt 正文与回答内容 |

---

## 四、【推荐学习路线】

### 4.1 纵向（深度）：五步台阶，每一步都有"验收测试"

> 原则：**先学数据契约，再学决策，最后学编排**。数据契约是"语法"，决策是"词法"，编排是"文章"。

**第 1 台阶 · 数据契约（半天）**
- 读：`config.yaml` → `core/config.py` → `core/schemas.py` → `core/errors.py` → `core/capabilities.py`
- 自问：`LLMRequest` 为什么禁止 stream+response_schema？`api_key_env` 为什么不直接存密钥？`extra="forbid"` 防什么？
- 验收：`python -m pytest tests/test_core_config.py tests/test_provider_config.py -v`

**第 2 台阶 · 决策与状态机（半天）**
- 读：`core/router.py` → `core/health.py` → `core/limits.py` → `service/retry.py`
- 自问：显式指定 `chat-pro` 但它被熔断了，路由会怎么办？（答案：仍排首位但带 `reject_reason=unhealthy`，执行时降级到等价候选——**能力欺骗与弹性降级的区别**）退避延迟的上限是多少？
- 验收：`python -m pytest tests/test_router.py tests/test_health.py tests/test_retry.py tests/test_weighted_routing.py -v`

**第 3 台阶 · 供应商适配与校验（一天）**
- 读：`core/providers/base.py` → `core/providers/openai_compatible.py` → `core/validation.py`
- 自问：L1 和 L2 的异常分别在哪里被翻译？为什么 `ProviderInvalidResponseError` 只降级不重试？修复策略为什么有上限？
- 验收：`python -m pytest tests/test_adapter_contract.py tests/test_structured_output.py tests/test_structured_repair.py -v`

**第 4 台阶 · 编排与出口（一天，重点）**
- 读：`service/llm_service.py`（建议：先 `complete()` 后 `stream()`）→ `api/routes.py` → `api/streaming.py` → `api/schemas_openai.py` → 回到 `app/app.py`
- 自问：首块前失败和首块后失败，代码路径在哪里分岔？`StreamCancelled` 什么时候 emit？估算 usage 与真实 usage 如何切换？
- 验收：`python -m pytest tests/test_service.py tests/test_streaming.py tests/test_api.py tests/test_api_openai_compat.py tests/test_stream_cancel.py tests/test_stream_usage.py -v`

**第 5 台阶 · 周边系统（半天）**
- 读：`security.py` → `rate_limit.py` → `storage.py` → `checkpoint.py` → `prompt_activation.py` → `audit.py` → `logging.py`
- 验收：`python -m pytest tests/test_security.py tests/test_rate_limit.py tests/test_storage.py tests/test_checkpoint.py tests/test_prompt_activation.py tests/test_core_audit_logging.py tests/test_observability.py -v`

### 4.2 横向（广度）：为什么要分层？—— 一份对比文档读懂工程判断

**先读基线**：`week01/1-6/gateway.py`（439 行单文件）。找一找前面表格里的 7 个缺陷，建立"问题感"。

**再读对比**：`docs/2026-11-16-llm-gateway-vs-1-7-architecture-comparison.md`，核心观点提炼：

| 维度 | 旧版（1-6 单文件） | 1-7 参考版 | 分层版（本项目） |
|---|---|---|---|
| 组织方式 | 一个文件装一切 | 按功能分组，`gateway.py` 381 行"上帝类" | 按层分组，16 个单一职责 core 模块 |
| 路由决策 | 静态主备 | 别名+策略，但**不解释为什么拒绝** | 全量候选 + `reject_reason` 留痕，可复盘 |
| 可测试性 | 难（真实网络） | 中（服务定位器） | 强（构造注入 + `fake_sleep`/`MockTransport`/`FakeProvider`） |
| 最坏延迟 | 无上限 | 路由数×重试数×超时，无上限 | 有界（全链路 deadline） |

**最有价值的教学点**：对比文档写于 2026-11-16，当时它还指出分层版"缺认证、缺限流、缺持久化、缺检查点"（第 14 节互补建议）。而**当前代码已经把这些建议全部落地**——认证（`security.py`）、限流（`rate_limit.py`）、SQLite 账本（`storage.py`）、流检查点（`checkpoint.py`）、`/v1/models`、`/healthz`、`/readyz`、运行时 Prompt 激活（`prompt_activation.py`）都已存在。**这恰恰是分层最大的回报**：因为 `AuditStore` 是 Protocol，加 SQLite 实现 = 新增一个类 + 改一行装配，不动任何业务逻辑；因为依赖是单向的，加认证 = 新增一个 `security.py` + 一个 `Depends`，指挥部毫发无伤。

**"为什么要分层"的最终答案**（面试也可以这样答）：
1. **单一职责**：每个模块小到能在一屏内读懂、说清、测全。
2. **决策可测试**：纯函数 + 依赖注入 → 129 个用例零真实网络、零真实等待。
3. **变更局部化**：换存储、换协议、加安全，都是"加一个模块"，而非"改一个大文件"。
4. **语义显式化**：`retryable`/`fallback_allowed` 属性、`reject_reason` 留痕，让系统行为可以被**解释**（运维能回答"为什么这次走了 chat-pro"）。

---

## 五、【动手实践建议】

### 5.1 环境准备（Windows PowerShell）

```powershell
cd D:\develop\agent_study\week01\llm-gateway
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # 或 uv sync（项目含 pyproject.toml + uv.lock）

# 真实调用需要 Key（测试不需要）
$env:DEEPSEEK_API_KEY = "sk-..."

# 启动
uvicorn app.app:create_app --factory --port 8000
# 换配置：$env:GATEWAY_CONFIG = "config.yaml"
```

### 5.2 把测试当"说明书"读（性价比最高的学习法）

```powershell
python -m pytest -q                          # 全量 129+ 用例，应全绿
python -m pytest tests/test_router.py -v     # 看路由决策的每一条断言
python -m pytest tests/test_streaming.py -v  # 看流式事件的每一个形状
```

技巧：**每个测试函数名就是一条行为契约**。例如 `test_router.py` 里的 `test_requested_unknown_model_raises_unknown_model`、`test_health_tracker_recovers_after_cooldown`；`test_streaming.py` 里的 `test_openai_failed_stream_truncates_without_done`。先读测试名 → 猜实现 → 再读实现 → 最后跑测试验证猜测，一轮就真正"学会"了。

### 5.3 五个递进式实验

1. **改配置，看启动校验**（不需要 Key）：给 `config.yaml` 的 `chat-fast` 加一个 adapter 不支持的能力（如 `tool_calling`）→ 启动 → 观察报错。再删掉 `pricing` → 观察报错。体会"配置即契约"。
2. **打开管理面，看决策留痕**：`admin_endpoints.enabled: true` → 启动 → 用 `httpx.MockTransport` 场景或测试里打印 `RoutingDecision`，观察 `reject_reason` 字段如何解释"为什么没选某个模型"。
3. **追踪一次完整调用**（无 Key 版）：在 `tests/conftest.py` 找 `MockTransport`/`FakeProvider` 的构造方式，用 `create_app(providers={"openai_compatible": FakeProvider(...)})` 在本地脚本里发起一次调用，断点停在 `_attempt_with_retries`，单步走完 ③~⑩ 的全流程。
4. **模拟熔断与降级**：用 `FakeProvider` 连续抛 `ProviderServerError` 3 次，观察 `HealthTracker` 熔断后路由如何自动跳过该模型（跑 `test_health.py` 看预期行为）。
5. **小改造挑战**（改代码验证理解）：
   - 注册一个新的 Provider 实现（实现 `complete/stream/close` 三个方法）→ 只改注册表和 config，不动 service；
   - 给 `providers/base.py` 加一个新异常类（如 `ProviderQuotaError`，`retryable=False, fallback_allowed=True`）→ 验证服务层无需改动即获得新语义；
   - 把 `storage.backend` 从 `memory` 切到 `sqlite` → 观察 `data/gateway.db` 五张表，理解 Protocol 接口的可替换性。

### 5.4 自测清单（毕业标准）

能脱口而出这些，说明你已建立架构模型：

1. `api → service → core` 每层的一句话职责和依赖方向。
2. L1–L4 分别在哪里被检测、失败后允许什么操作。
3. 显式指定模型被熔断时，会发生什么、为什么不算"静默换模型"。
4. 首块 delta 为什么是流式的"不归点"。
5. `timeout_seconds` 为什么叫"总预算"而非"单次超时"。
6. 连接池复用的缓存键是什么。
7. `RoutingDecision` 存在的意义。
8. 修复策略为什么"有界"且"不触发上游重试"。
9. `fake_sleep`/`MockTransport`/`FakeProvider` 分别替换了什么副作用。
10. 对比文档第 14 节的建议中，哪些已在当前代码落地、落地为什么这么便宜。

---

**学习节奏建议**：全程约 3–4 天。第 1 天走完【全景视图】+ 第 1~2 台阶；第 2 天第 3~4 台阶（精读 `llm_service.py`）；第 3 天横向对比 + 5.3 实验；第 4 天自测清单 + 小改造挑战。祝学习顺利，这个项目的分层思想值得刻进肌肉记忆。
