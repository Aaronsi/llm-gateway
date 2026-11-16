# Mini LLM Gateway 架构与实现对比：1-7 参考实现 vs week01/llm-gateway 分层重构版

- **日期**: 2026-11-16
- **对比对象 A**: `D:\develop\ai-agent-fullstack-training\course_code\week01\1-7\llm-gateway`（课程 1-7 参考实现，下文简称 **1-7 版**）
- **对比对象 B**: `D:\develop\agent_study\week01\llm-gateway`（由 1-6 单文件网关重构而来的分层版，下文简称 **分层版**）
- **结论先行**: 两者是同一类产品（OpenAI-compatible 多模型 LLM 网关）的两种价值取向。1-7 版是"**生产可用性优先**"的成品——认证、限流、SQLite 账本、双协议、容器化一应俱全；分层版是"**架构与工程质量优先**"的作品——严格三层架构、能力路由、可解释路由决策、129 个测试用例，但缺少认证、限流与持久化。两者互为镜像：1-7 版缺的是分层版擅长的，分层版缺的是 1-7 版擅长的。

## 1. 总体定位对比

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 定位 | 面向 Agent Engineering Platform 的统一模型入口，可部署的 FastAPI 服务端 | 1-6 单文件网关的**重构 + 扩展**，教学/工程范式示范 |
| 上游协议 | Chat Completions + Responses 双协议 | 仅 Chat Completions（Responses/tool_calling 明确 YAGNI） |
| 对下协议 | 纯 HTTP 透传（httpx），不依赖 SDK | OpenAI SDK 适配器（AsyncOpenAI），连接池复用 |
| 面向调用方 | 完全 OpenAI 兼容（SDK 无缝） | 网关原生 `/v1/llm` + OpenAI facade |
| 持久化 | SQLite（用量账本、流检查点、Prompt 版本） | 无（审计/指标全部内存态） |
| 安全 | Bearer API Key + 令牌桶限流 | 无认证、无限流（仅并发守卫） |
| 测试 | 6 个集成用例（单文件） | 129 个用例（12 个文件，分层测试金字塔） |
| 部署 | Dockerfile + docker-compose + Makefile + uv + ruff | requirements.txt 手动安装 |

## 2. 架构与分层

### 1-7 版：功能分组，松分层

```
app/
├── api/routes.py        # 端点（含认证/限流依赖注入）
├── core/                # errors、security、rate_limit
├── services/
│   ├── gateway.py       # 编排中心（381 行）：流式/JSON 调用、重试、结构化纠错
│   ├── upstream.py      # httpx 上游客户端（112 行）
│   ├── router.py        # 路由 + 熔断（84 行）
│   ├── prompts.py       # SQLite Prompt 版本 + Jinja2 渲染
│   ├── structured.py    # JSON 提取 + Schema 校验
│   └── usage.py         # SQLite 用量账本
├── config.py / schemas.py / main.py
```

特点：按**功能**分组而不是按层。`services/gateway.py` 承担了调用编排、流式转发、重试循环、结构化纠错、SSE 解析、检查点写入等几乎所有核心逻辑；路由、重试、结构化纠错三条控制流交织在一个 `while True` 循环里（`call_json` 中 candidates 循环嵌套 attempts 循环，外层再套 structured_attempt 循环）。

### 分层版：严格三层，依赖单向

```
api（端点/协议翻译）→ service（业务编排）→ core（基础设施抽象）
依赖方向单向：core 不 import service/api；service 不 import api；装配只发生在 app/app.py
```

| 层 | 模块 | 职责 |
|---|---|---|
| api | routes / schemas_openai / streaming / errors | 端点、OpenAI facade 翻译、SSE 格式化、错误映射 |
| service | llm_service / retry | 路由计划执行、重试/降级、L4 校验、流终态状态机 |
| core | router / health / limits / providers / prompts / validation / audit / metrics / logging / schemas / errors / config / capabilities | 16 个单一职责模块 |

特点：core 层大量**纯函数与纯决策**（`router.plan()` 同输入同输出、`retry.backoff_delay()` 纯函数、`validation` 策略列表）；可注入的 `sleep`/`clock`/`providers`/`prompts_dir` 使 service 层可以脱离 I/O 测试。`app.py` 是唯一允许跨层 wiring 的地方。

### 对比评价

| 项目 | 1-7 版 | 分层版 |
|---|---|---|
| 模块边界 | 松（gateway.py 是事实上的"上帝类"） | 严格、单一职责清晰 |
| 依赖方向 | 无明确约束 | 单向强制，装配点唯一 |
| 路由决策可解释性 | 弱（`candidates()` 只返回可用列表，不解释为什么拒绝） | 强（`RoutingDecision` 记录全部候选 + `reject_reason`，可复盘） |
| 可测试性 | 中（依赖 app.state 服务定位器） | 强（构造函数注入 + 时间/睡眠/供应商替身） |
| 上手成本 | 低（文件少，读两遍能懂） | 较高（16 个 core 模块 + 协议抽象） |

## 3. 端点与协议面

### 1-7 版端点

- `POST /v1/chat/completions`（OpenAI 兼容）
- `POST /v1/responses`（Responses API，路由按 `api: chat/responses/both` 协议过滤）
- `GET /v1/models`
- `POST/GET /v1/prompts`、`GET /v1/prompts/{id}`、`POST /v1/prompts/{id}/render`（Prompt 运行时管理）
- `GET /admin/usage`、`GET /admin/routes`
- `GET /v1/streams/{request_id}/checkpoint`（断线后内容找回）
- `GET /healthz`、`GET /readyz`

### 分层版端点

- `POST /v1/llm`（网关原生）、`POST /v1/llm/stream`（便捷别名）
- `POST /v1/chat/completions`（OpenAI facade）
- `GET /v1/traces`、`GET /v1/routing`、`GET /v1/metrics`（管理面，`admin_endpoints.enabled` 开关控制，默认关闭）

### 对比评价

1-7 版协议面更广（双上游协议 + models 端点 + Prompt 运行时管理 + 健康检查），且对调用方完全 OpenAI 兼容。分层版多了路由决策留痕与指标快照的管理面，但缺 `/v1/models` 与健康检查；OpenAI facade 接受 `temperature/top_p/n/stop/seed/user` 等字段后**静默忽略**（debug 日志），不支持的 SDK 参数不会报错但也不生效，与 1-7 版"原样透传一切字段"相比是信息损耗。

## 4. 路由模型

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 模型组织 | **别名 → 路由组**（如 `smart: [openai/gpt-5.2, deepseek/deepseek-chat]`），每个别名配 `strategy: priority / weighted_round_robin` + 权重 | **全局模型池 + 能力路由**：无别名概念，`model` 显式指定或留空按能力自动选 |
| 选择策略 | 优先级顺序 / 加权轮询（`itertools.count` 轮转） | priority 降序 → cost 升序 → 声明顺序（确定性排序） |
| 显式模型语义 | 别名不存在 → 404 | **硬校验**：模型不在白名单 → `unknown_model` 400；能力不满足 → 400，**绝不静默换模型**（降级只发生在路由候选层面） |
| 能力感知 | 无能力模型，只有 `api`（chat/responses/both）协议过滤 | `Capability` 枚举（streaming/structured_output）+ 适配器能力声明启动校验 |
| 拒绝原因 | 无 | 每个候选带 `reject_reason`（capability_missing / unhealthy / capacity_full），写入决策日志 |

评价：1-7 版的别名 + 加权轮询对"负载均衡"场景更实用（同一逻辑模型挂多个供应商分摊流量）；分层版的能力路由对 Agent 平台更友好（模型语义化命名、缺省自动选型），且**显式模型硬校验**是一个重要的语义增强——客户端指定 `chat-pro` 时绝不会因为熔断被偷偷换成 `chat-fast`。但分层版缺少加权负载均衡策略，1-7 版缺少决策留痕。

## 5. 弹性机制（重试 / 降级 / 熔断 / 限流 / 预算）

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 重试判定 | HTTP 状态码集合（408/409/429/5xx 可配置） | 分层异常类属性（`ProviderError.retryable` / `fallback_allowed`）声明式驱动 |
| 重试粒度 | 每条路由 `max_attempts_per_route` 次 | 每模型 `max_attempts_per_model` 次 |
| 退避 | 指数退避 + 抖动（0.75~1.25） | 指数退避 + 抖动（0.5~1.5），纯函数可测 |
| 熔断 | 每供应商连续失败阈值 + 冷却 | 每模型连续失败阈值 + 冷却（同构） |
| 超时模型 | 每供应商 `timeout_seconds`/`connect_timeout_seconds`（每次上游调用独立超时） | **全链路 deadline**：`timeout_seconds` 是跨重试与降级共享的总预算，剩余不足 → `insufficient_time_budget`(429) |
| 并发控制 | 无 | 全局 + 每模型并发池（`CapacityGuard`），模型池满 → 换候选，全局满 → 429 |
| 调用方限流 | 每 API Key 令牌桶（进程内） | 无 |
| 降级语义 | 重试后按序换下一路由 | 按 `fallback_allowed` 属性决定；L4 校验失败**不重试不降级**（本地有界修复） |

评价：两者熔断/退避思路同源。1-7 版的优势在**入口限流**与可配置状态码重试；分层版的优势在**全链路时间预算**（1-7 版最坏情况 N 条路由 × M 次重试 × 120s 超时，总延迟无上限）与**并发守卫**（保护上游不被打爆）。分层版缺入口速率限流是生产硬伤。

## 6. 结构化输出

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| Schema 透传 | `response_format.json_schema` / `text.format` 原样传给上游 | 按 `structured_output_mode` 选择 `json_schema` 原生透传或 `json_object` + system 注入 schema 提示 |
| 本地校验 | 单一 `jsonschema` 校验 + Markdown 围栏剥离 | **Pydantic 动态构建校验器优先**（常见关键字子集，顺带做类型归一化）+ jsonschema 兜底 |
| 校验失败处理 | **LLM 修复循环**：把上游输出 + 精确校验错误作为新消息回传给模型重生成，最多 `structured_output_retries` 次 | **本地有界修复**：直接解析 → 剥围栏 → 提取首个平衡 JSON 对象，全败 → `invalid_json`(502)；**不触发上游重试** |
| 流式 + Schema | 允许组合，但流式无法无损纠错（README 明示建议非流式） | **禁止组合**（请求级校验报错） |

评价：这是两者最本质的策略分歧之一。1-7 版用**额外 LLM 调用换成功率**（成本与延迟不可预测，且 `structured_output_retries` 上限后仍 422）；分层版用**零成本本地修复 + 严格失败**（延迟与成本有界，但严格 schema 的通过率依赖上游原生 json_schema 能力）。对成本敏感的 Agent 平台，分层版策略更稳健；对成功率优先的抽取任务，1-7 版策略更实用。分层版的 Pydantic 优先校验器还额外提供了**类型归一化输出**（`parsed` 字段），这是 1-7 版没有的增值能力。

## 7. 流式处理

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 转发方式 | **字节级透明转发**：上游 SSE 原样 yield（仅旁路解析提取 usage/正文用于账本） | **解析-重发射**：上游文本 delta 解析为 `StreamDelta`，再由 api 层编码为网关 SSE 或 OpenAI chunk |
| 信息保真 | 100%（工具调用、reasoning、任意扩展事件全部透传） | 仅文本（`delta.content`），其余 delta 类型丢失 |
| 首块边界语义 | 首块前失败可重试/降级；首块后失败发 SSE error + `[DONE]` | 同构：首 delta 前可降级；首 delta 后只发 `failed` 终态（**终态唯一，绝不重新生成**） |
| 终态模型 | 隐式（`[DONE]` / error event） | **显式状态机**：delta → usage → completed/failed，Pydantic 判别联合 |
| 客户端断开 | `request.is_disconnected()` 轮询 → 关闭上游连接（`aclose`）→ 记录 `cancelled`(499) | **无断开检测**：`StreamCancelled` 事件已定义但 service 从不 emit；生成器被取消后审计无 cancelled 记录 |
| 断线找回 | `stream_checkpoint` 按间隔持久化已生成文本，可用 `X-Request-ID` 查询 | 无 |
| 流式 Token 统计 | 解析上游 usage（需要 `stream_options.include_usage`） | **估算**：`len(text) / chars_per_token(2.0)`，无真实 usage |

评价：1-7 版流式是"**透明代理**"（保真 + 断开感知 + 检查点找回，生产语义完整）；分层版流式是"**结构化事件流**"（事件模型干净、终态唯一、格式转换内聚在 api 层），但丢失非文本 delta 与真实 Token 数，且缺断开处理闭环。若分层版需要支持工具调用流式，必须重写 provider 的 stream 契约。

## 8. Prompt 管理

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 存储 | SQLite 表，`POST /v1/prompts` 运行时发布新版本、`activate` 激活 | YAML 资产文件（`prompts/**/*.yaml`），启动时加载，仅 `status: published` 可用 |
| 版本 | 整数自增，激活版本 + 显式 version 查询 | 字符串版本（如 `v1`），无激活概念 |
| 渲染安全 | Jinja2 Sandbox + StrictUndefined | Jinja2 Sandbox + StrictUndefined + **AST 级禁用节点**（For/Macro/Import/Filter/Call/Getattr/Getitem） |
| 变量治理 | 自由变量（无声明式校验） | **声明式变量契约**：type/max_length 校验、控制字符拒绝、缺失变量 → `missing_prompt_variable`、渲染结果 Context 预算检查 |
| 引用方式 | `gateway_prompt` 注入，可 prepend/append，支持 chat 与 responses | `prompt` 选择（name+version+variables），仅 prepend system |
| 注入防护 | 依赖沙箱 + README 提示 | 沙箱 + AST 白名单 + "模板只能管理员发布，调用方不能提交正文" |

评价：1-7 版胜在**运营性**——不改代码、不停机即可发布/回滚/激活 Prompt 版本；分层版胜在**安全与治理**——变量声明契约、上下文预算、AST 节点白名单构成纵深防御，但发布新 Prompt 需改 YAML + 重启。两者结合（YAML 资产 + 运行时激活 API）是理想形态。

## 9. 可观测性与成本

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 用量账本 | **SQLite 持久化**：request_id、token、TTFT、延迟、重试、fallback、成本、prompt_id/version、错误类型，`/admin/usage` 查询 | 内存 `CallTrace`（白名单字段，不存正文），`/v1/traces` 查询；重启即失 |
| 成本计算 | 按供应商模型定价（含 cached 折扣），未定价记 0 | 分项成本 `CostBreakdown`（input/cached/output），绑定模型配置 |
| 指标聚合 | 无 | 请求数/错误率/错误码分布/Token 累计/p50-p99 延迟/TTFT p50，`/v1/metrics` 快照 |
| 关联 ID | `X-Request-ID`（单级） | **两级**：`X-Request-Id`(call_id) + `X-Trace-Id`(trace_id)，ContextVar 自动附加到每条日志 |
| 日志安全 | 基础 logging | JSON 格式化 + `SensitiveDataFilter` 脱敏（sk-/Bearer/密钥类键）+ 200 字符截断 |
| 路由复盘 | 仅熔断状态 `/admin/routes` | **RoutingDecision 全量留痕**（候选、拒绝原因、attempts、final_error_code、config_version） |

评价：1-7 版的可观测性**跨重启存活**（账本持久化），适合真实计费与审计；分层版的观测**维度更丰富**（指标聚合、两级关联、决策留痕、日志脱敏），但全是易失状态。分层版的白名单审计原则（不存正文）比 1-7 版更保守安全；1-7 版的检查点会持久化模型正文（默认关闭，文档有隐私提示）。

## 10. 安全

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 认证 | Bearer API Key（hmac.compare_digest 防时序攻击）+ `x-api-key` 头；无配置 key 时开发模式放行（按客户端 IP 记指纹） | **无** |
| 调用方限流 | 进程内令牌桶（每 key） | 无（并发守卫 ≠ 速率限流） |
| 密钥管理 | SecretStr + `${ENV}` 展开（配置可含密钥或变量引用） | **只存环境变量名**（`api_key_env`），密钥运行时读取——更严格的配置卫生 |
| 敏感信息 | 仅存 API key 指纹（sha256 前 16 位）；不存 Prompt/消息正文 | 白名单 schema + 日志脱敏兜底；审计不存正文 |
| 管理面隔离 | `/admin/*` 与业务端点同认证（README 建议拆分内网） | `admin_endpoints.enabled` 开关（默认关），但开启后**无独立认证** |

评价：1-7 版具备完整的安全闭环（认证 + 限流 + 指纹化审计）；分层版在**配置卫生**（密钥不入配置）与**数据最小化**上更严格，但"零认证 + 无限流"意味着当前不可公网部署。两者安全设计互补。

## 11. 配置设计

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 结构 | providers 独立声明（base_url/api_key/超时/额外头）→ models 别名引用 provider | models 直接内嵌 adapter/base_url/api_key_env/pricing（无独立 providers 段） |
| 环境变量 | `${VAR:-default}` 展开（含默认值语法） | 仅 `api_key_env` 变量名引用 |
| 启动校验 | 路由引用未知 provider 报错；ProviderConfig URL 归一化 | `extra="forbid"` 严格模式 + 能力 ⊆ 适配器能力校验 + 未知 adapter 报错 |
| 密钥类型 | SecretStr | 无（配置里根本没密钥） |

评价：1-7 版的 providers/models 两段式更接近真实供应商管理（同一供应商多 key、额外头、按供应商超时）；分层版的模型内嵌式配置更扁平，但同一供应商多模型时 base_url/key 重复声明。分层版的严格校验（forbid + 能力交叉校验）能更早暴露配置错误。

## 12. 工程化（测试 / 部署 / 工具链）

| 维度 | 1-7 版 | 分层版 |
|---|---|---|
| 测试规模 | 6 个集成用例（`test_gateway.py`，215 行） | **129 个用例 / 12 文件**：适配器契约、路由、重试、流式、结构化、可观测、编排、API（原生 + facade）、配置、审计日志 |
| 测试手段 | `httpx.MockTransport` 模拟上游 + ASGI 客户端 | 同款 MockTransport 契约测试 + `FakeProvider` 注入 + `fake_sleep` 零等待重试测试 + 可注入 clock |
| 依赖管理 | uv + pyproject（版本约束）+ dev 依赖（ruff） | requirements.txt（无版本约束） |
| 代码质量 | ruff（E/F/I/B/UP） | 无 linter 配置 |
| 容器化 | Dockerfile（非 root 用户）+ docker-compose + Makefile | 无 |
| 文档 | 详尽 README（含生产化建议清单） | README + 677 行设计文档（§12 分阶段测试计划等） |

评价：测试是两者差距最大的维度。分层版 129 个用例覆盖了 1-7 版 6 个用例的全部场景并远超出（重试退避不真等待、熔断时间注入、审计白名单、脱敏等 1-7 版根本没有覆盖的行为）；1-7 版则在交付工程化（容器、lint、锁文件、一键构建）上完整得多。

## 13. 优缺点总结

### 1-7 版（课程参考实现）

**优点**

1. **生产可用性闭环完整**：认证、限流、SQLite 持久账本、健康/就绪检查、容器化、非 root 运行，开箱可部署
2. **协议面广**：Chat Completions + Responses 双上游协议，`/v1/models`、Prompt 运行时管理 API 齐全，对 OpenAI SDK 完全兼容
3. **流式透明转发**：字节级保真（不丢失工具调用等非文本事件）、客户端断开感知并关闭上游、断线检查点找回——流式语义比分层版更完整
4. **结构化输出成功率更高**：LLM 修复循环利用精确校验错误反馈重生成
5. **路由策略实用**：加权轮询支持同别名多供应商负载均衡，按状态码重试粒度细
6. **Prompt 运营性强**：运行时发布/激活/回滚版本，无需重启

**缺点**

1. **架构松耦合**：`services/gateway.py` 381 行集中了编排、重试、结构化纠错、SSE 解析、检查点写入等多职责，三层控制流嵌套循环，扩展与维护成本高
2. **路由决策不透明**：候选被拒原因（熔断/协议不匹配）不可见，难以复盘"为什么走了这个模型"
3. **无全链路时间预算**：最坏延迟 = 路由数 × 重试数 × 单次超时，无上限；也无并发守卫，上游可能被打爆
4. **无能力路由**：每个别名需人工配置路由组合；显式指定模型不存在即 404，无"按需自动选型"
5. **测试严重不足**：6 个用例对 380+ 行核心编排逻辑的覆盖有限（README 声称的覆盖范围与实际测试文件规模不匹配）
6. **结构化修复成本不可控**：修复循环的额外 Token 与延迟随重试次数线性放大
7. **流式 Token 统计依赖上游配合**：需要 `include_usage`，未传时账本缺 Token/成本数据

### 分层版（week01/llm-gateway）

**优点**

1. **严格三层架构**：依赖单向、装配点唯一、core 层大量纯函数——扩展新适配器/新路由策略的成本远低于 1-7 版
2. **能力路由 + 显式模型硬校验**：缺省按能力自动选型，显式指定绝不静默换模型，语义正确性高
3. **路由决策可复盘**：全量候选 + 拒绝原因 + 最终错误码写入决策日志，运维可解释
4. **全链路时间预算**：跨重试与降级的 deadline 保证最坏延迟有界（`insufficient_time_budget`）
5. **并发守卫**：全局 + 每模型池，模型池满优雅降级而非失败
6. **L4 校验有界且零成本**：本地多策略修复（剥围栏/提取平衡对象）+ Pydantic 归一化输出，不产生额外 LLM 调用
7. **测试与可测性**：129 用例、时间/睡眠/供应商全可注入、契约测试——回归风险极低
8. **观测维度丰富**：两级关联 ID、分项成本、指标分位数、路由留痕、日志脱敏
9. **Prompt 安全纵深**：AST 节点白名单、变量声明契约、上下文预算、管理员发布模型
10. **配置卫生**：密钥不进配置（只存环境变量名）、严格模式启动校验

**缺点**

1. **无认证、无速率限流**：当前不可直接暴露公网，安全闭环缺失
2. **无持久化**：审计、指标、决策日志全部内存态，重启即失，无法做真实计费
3. **流式能力受限**：只重发文本 delta（工具调用等丢失）、Token 按字符数估算、无客户端断开检测（`StreamCancelled` 定义了但从未 emit）、无断线找回
4. **协议面窄**：无 Responses API、无 `/v1/models`、无健康检查端点；facade 静默忽略 temperature/top_p 等字段
5. **结构化输出无 LLM 修复**：严格 schema 通过率完全依赖上游原生 json_schema 能力，失败即 502
6. **Prompt 发布需重启**：无运行时管理 API，运营灵活性差
7. **无加权负载均衡**：排序确定性高但无法按权重分摊流量
8. **工程化缺位**：无 Docker/锁文件/lint/CI 配置，交付链路不完整

## 14. 互补建议（如果合并演进）

1. **把 1-7 版的认证 + 限流移植进分层版**：`api` 层加 `Depends(authenticate)` + `core/rate_limit.py`，与分层版并发守卫形成完整防护
2. **把 1-7 版的 SQLite 账本移植进分层版**：`InMemoryAuditStore`/`MetricsRecorder` 已是 Protocol 接口，只需实现持久化实现类，审计白名单原则天然满足合规
3. **把分层版的决策留痕移植进 1-7 版**：`ModelRouter.candidates()` 返回拒绝原因，`/admin/routes` 升级为完整复盘视图
4. **给 1-7 版加全链路 deadline**：跨路由共享剩余预算，补 `insufficient_time_budget`
5. **给分层版加断开检测**：在 `_stream` 循环中轮询 `request.is_disconnected()`（需把 Request 传入 service 或由 api 层处理），补 emit `StreamCancelled` 闭环
6. **流式契约升级**：若分层版需要工具调用流式，参考 1-7 版的透明转发方案，把 `StreamDelta` 扩展为 `StreamRawChunk` 事件
7. **Prompt 管理合流**：保留 YAML 资产（声明式契约 + AST 白名单）作为"发布源"，加一个运行时"激活"表（SQLite）实现不停机切换版本

## 附录：关键事实速查

| 事实 | 1-7 版 | 分层版 |
|---|---|---|
| 核心编排文件行数 | `services/gateway.py` 381 行 | `service/llm_service.py` 409 行（但路由/重试/校验/格式化已外置） |
| 测试用例数（实际收集） | 6 | 129 |
| 熔断实现 | 每供应商 CircuitState | 每模型 HealthTracker（同构） |
| 重试可判定来源 | HTTP 状态码集合 | 异常类属性（retryable/fallback_allowed） |
| 错误响应形状 | OpenAI 风格 `{"error": {message/type/param/code}}` | `{"detail": {"code", "message"}}` |
| 流式 SS 格式 | 透传上游 | 自有 `content.delta` / `response.usage` / `response.completed` / `response.failed` |
| 上游 Token 来源 | 解析上游 usage payload | 非流式取 SDK usage；流式按 chars/2.0 估算 |
| 供应商抽象 | 单一 `UpstreamClient`（所有供应商同构 httpx） | `Provider` Protocol + 适配器注册表（可插拔 SDK 适配器） |
| 客户端连接复用 | 单个共享 `httpx.AsyncClient`（可注入） | 按 (base_url, api_key_env) 键控缓存 AsyncOpenAI 客户端 |
