# llm-gateway 改进建议（优化清单）

- **日期**: 2026-11-16
- **对象**: `D:\develop\agent_study\week01\llm-gateway`（分层版）
- **依据**: [2026-11-16-llm-gateway-vs-1-7-architecture-comparison.md](./2026-11-16-llm-gateway-vs-1-7-architecture-comparison.md) 的优缺点对比
- **原则**: 只补齐分层版的短板，**不牺牲既有优势**（三层架构、能力路由 + 显式模型硬校验、路由决策留痕、全链路 deadline、并发守卫、129 用例测试金字塔）。每条建议标注优先级、参考来源（1-7 版对应实现）、工作量与验收标准。

---

## P0：安全补齐（当前不可公网部署的硬伤）

### 建议 1：增加 API Key 认证

**现状问题**: 所有端点（含管理面）零认证，任何能访问端口的人都能调用模型、查看审计。

**方案**（参考 1-7 版 `app/core/security.py`）:

1. `core` 层新增 `security.py`：
   - `authenticate(request, authorization, x_api_key)` 依赖函数，支持 `Authorization: Bearer` 与 `x-api-key` 两种头
   - 用 `hmac.compare_digest` 常量时间比较，防时序攻击
   - 返回 key 的不可逆指纹（sha256 前 16 位）作为审计 `api_key_hash` 字段
2. `AppConfig` 增加 `api_keys: list[SecretStr]`；密钥只从环境变量读取（延续分层版"配置卫生"原则：YAML 中只写 `${ENV_VAR}` 引用，加载时展开）
3. `api/routes.py` 所有端点挂 `Depends(authenticate)`；管理面端点可选独立 key 集合，无独立 key 时回退主 key 或强制关闭
4. `CallTrace` 增加 `api_key_hash` 字段（白名单原则：只存指纹）

**收益**: 补上公网部署的前提条件；审计可按调用方区分。

**工作量**: 小（约 100 行 + 测试）。

**验收**: 无 key 请求 401；错误 key 401；正确 key 通过且审计记录指纹；测试含时序攻击无关性（不同长度 key 比较无早期返回）。

### 建议 2：增加速率限流（令牌桶）

**现状问题**: `CapacityGuard` 只限制**并发**，不限制**速率**；单 key 高频小请求仍可打爆上游预算。

**方案**（参考 1-7 版 `app/core/rate_limit.py`）:

1. `core/rate_limit.py`：进程内令牌桶（`requests_per_minute` + `burst` 容量），`asyncio.Lock` 保护，惰性创建每 key 桶
2. 配置项 `limits.rate: {enabled, requests_per_minute, burst}`；接口边界独立，多副本时替换为 Redis 实现
3. 在认证依赖之后作为第二个 `Depends` 执行（1-7 版的 `limited_identity` 组合依赖模式）
4. 超限返回 `GatewayError("rate_limit_exceeded", ..., 429)`，响应头带 `Retry-After`

**收益**: 与并发守卫互补，形成"并发 + 速率"双层防护。

**工作量**: 小（约 60 行 + 测试）。

**验收**: 突发量内放行、超突发 429、`Retry-After` 正确；时间注入测试（复用分层版可注入 clock 风格，不真等待）。

---

## P1：持久化与流式语义补齐

### 建议 3：审计/指标/路由决策落盘（SQLite 实现类）

**现状问题**: `InMemoryAuditStore` / `InMemoryMetricsRecorder` / `RoutingDecisionLog` 全是内存态，重启即失；无法做真实计费与历史复盘。

**方案**（参考 1-7 版 `app/services/usage.py` 的 aiosqlite 模式，但保留分层版的 Protocol 接口不动）:

1. 新增 `core/audit_sqlite.py`：`SQLiteAuditStore(AuditStore)`，表结构对齐 `CallTrace` 白名单字段（不存正文）
2. `SQLiteMetricsRecorder(MetricsRecorder)`：定期聚合快照落盘，避免每请求写库
3. `RoutingDecisionLog` 同样加持久化实现
4. `app.py` 装配处按配置选择实现（`storage.backend: memory | sqlite`），默认仍 memory 保证测试零 I/O；lifespan 中初始化建表、退出时关闭
5. 写库失败不阻断请求（参考 1-7 版 `_record_usage_safely` 的 try/except + 日志）

**收益**: 审计可跨重启查询；成本账本可用于计费；决策留痕可做历史复盘。

**工作量**: 中（约 250 行 + 测试，接口已存在）。

**验收**: 同一测试双跑 memory/sqlite 后端；重启后 `/v1/traces` 仍可查；写库异常不影响请求响应。

### 建议 4：流式客户端断开检测与 `StreamCancelled` 闭环

**现状问题**: `StreamCancelled` 事件已在 schema 与 api 格式化层处理，但 **service 从不 emit**；客户端断开后生成器被 asyncio 取消，审计无 cancelled 记录，上游连接释放依赖垃圾回收。

**方案**（参考 1-7 版 `gateway.py` 的 `request.is_disconnected()` 轮询 + `aclose()`）:

1. `stream()` 工厂方法把 `Request` 或一个 `is_disconnected: Callable[[], Awaitable[bool]]` 传入 `_stream`
2. 每个 delta 转发前检查断开：断开 → 记 `trace.status="cancelled"`（审计 + metrics）→ yield `StreamCancelled` → 主动关闭上游迭代（`aclose()`）
3. 外层 `_stream_guarded` 捕获 `asyncio.CancelledError`：补记 cancelled 审计后 re-raise（保证取消路径也有审计记录）
4. api 层 `StreamCancelled` 格式化维持现状（不 emit 网关 SSE；OpenAI facade 直接断流）

**收益**: 补齐断链语义；及时释放上游连接与 Token 消耗；审计准确反映取消率。

**工作量**: 小（约 40 行 + 测试）。

**验收**: 测试用假 `is_disconnected` 在第二个 delta 返回 True，断言事件序列含 cancelled、审计 status=cancelled、上游迭代器被关闭。

### 建议 5：流式真实 Token 统计（估算降级为兜底）

**现状问题**: 流式 usage 用 `len(text)/2.0` 估算，误差大（尤其中文/代码），成本与配额统计失真。

**方案**（参考 1-7 版 `_collect_stream_data` 解析上游 usage payload）:

1. `Provider` 协议扩展：`stream()` 返回值由 `AsyncIterator[str]` 改为 `AsyncIterator[StreamChunk]`（`text: str | None, usage: Usage | None`），或提供独立的 `stream_with_usage`
2. `OpenAICompatibleProvider.stream` 携带 `stream_options={"include_usage": True}`，解析末块 usage chunk（`chunk.usage`）
3. 服务层收到真实 usage 时覆盖估算值；未收到时保留 chars/token 估算作为兜底
4. `StreamUsageEvent` 携带的 usage 标注来源（`estimated: bool` 字段），审计如实记录

**收益**: 成本与用量统计准确；对上层暴露估算标记，避免误用。

**工作量**: 中（协议变更影响 2 个适配器 + FakeProvider + 相关测试）。

**验收**: 契约测试断言 usage chunk 被解析；无 usage 的上游 fallback 到估算且 `estimated=true`。

### 建议 6：断线后内容找回（流检查点，默认关闭）

**现状问题**: 长输出中途断线，客户端只能重新生成，Token 浪费。

**方案**（参考 1-7 版 `stream_checkpoint` + `stream_checkpoints` 表）:

1. 配置 `limits.stream_checkpoint: {enabled: false, flush_interval_seconds, max_chars}`，默认关闭（延续 1-7 的隐私评估前置原则）
2. 开启时按间隔把已解析文本增量写入持久层（复用建议 3 的存储后端），`GET /v1/streams/{request_id}/checkpoint` 查询
3. 与建议 4 的断开检测共享状态

**收益**: 断线恢复能力，长文本场景显著省 Token。

**工作量**: 中（依赖建议 3 的持久层）。

**验收**: 开启后中途断开可查询到部分内容，状态流转 `streaming → success/error/cancelled`。

---

## P2：协议面与能力扩展

### 建议 7：补齐 `/v1/models` 与健康检查端点

**现状问题**: OpenAI SDK 客户端习惯性调用 `/v1/models` 探测模型；无健康检查端点导致容器编排/K8s 无法就绪探测。

**方案**（参考 1-7 版 `routes.py` 的 `models/healthz/readyz`）:

1. `GET /v1/models`：返回配置中全部逻辑模型名（`ModelObject` 形状对齐 OpenAI）
2. `GET /healthz`（存活）与 `GET /readyz`（就绪，检查配置加载与存储后端可用性），`include_in_schema=False`
3. 均走认证（healthz 可豁免，供 LB 使用）

**收益**: SDK 兼容性 + 部署可用性。

**工作量**: 小（约 40 行 + 测试）。

**验收**: models 列表与配置一致；readyz 在存储后端异常时返回 503。

### 建议 8：facade 被忽略字段显式化

**现状问题**: `temperature/top_p/n/stop/seed/user` 等字段被静默忽略（仅 debug 日志），调用方无感知，产生"设了温度没生效"的困惑。

**方案**:

1. 忽略字段从 `extra="allow"` 的 `model_extra` 中显式枚举，response 头部或响应体附加 `ignored_fields: [...]` 字段（非流式）
2. 日志从 debug 升到 warning（一次性聚合）
3. 未来若上游适配器支持透传，在 `ProviderCall` 加 `extra_params` 透传通道

**收益**: 行为透明；为后续参数透传预留扩展点。

**工作量**: 小。

**验收**: 非流式响应含 `ignored_fields`；日志可见 warning。

### 建议 9：结构化输出可选"反馈修复"开关

**现状问题**: L4 校验失败直接 502（本地有界修复策略），对 `json_object` 模式 + 弱模型场景通过率低。1-7 版的 LLM 修复循环成功率更高但有成本/延迟不可控问题。

**方案**（取两者之长，默认保持分层版零成本策略）:

1. 配置 `structured_output.repair: {enabled: false, max_llm_repair_attempts: 1}`，默认关闭
2. 开启且本地修复失败时，参考 1-7 版 `repair_instruction()`：把模型输出 + 精确校验错误作为新消息追加，重生成，次数有界（建议上限 1，避免成本失控）
3. 修复调用计入 deadline（复用全链路时间预算，剩余不足 → `insufficient_time_budget`）；审计记录 `repair_attempts` 字段

**收益**: 保留默认零成本策略的同时，为高成功率场景提供选项；比 1-7 版多了 deadline 兜底，成本有界。

**工作量**: 中（约 80 行 + 测试）。

**验收**: 默认关闭时行为不变（回归测试）；开启时失败一次后修复成功；修复超预算 → 429。

### 建议 10：加权路由策略（weighted_round_robin）

**现状问题**: 路由排序是确定性 priority → cost，同优先级模型流量完全集中到最低价模型，无法分摊负载；且同价模型永远是声明顺序的第一个。

**方案**（参考 1-7 版 `router.py` 的加权轮询）:

1. `ModelConfig` 增加 `weight: int = 1`
2. `ModelRouter.plan()` 在 priority 相同、cost 相近（可配置容差）的候选组内，按 weight 加权轮转首选项；其余候选保留为 fallback
3. 轮转计数复用 1-7 版 `itertools.count` 按模型组键控；决策留痕中记录 `weight` 与轮转 offset，保持可复盘

**收益**: 同别名多供应商负载均衡能力；决策留痕不退化。

**工作量**: 中（纯函数改动 + 测试）。

**验收**: 权重 3:1 的候选在多次请求中首选项分布约为 3:1；留痕含轮转信息。

---

## P3：运营与工程化

### 建议 11：Prompt 运行时激活（保留 YAML 契约 + AST 白名单）

**现状问题**: 发布/回滚 Prompt 需改 YAML + 重启进程，运营灵活性差（1-7 版有 SQLite 版本 + 运行时激活 API）。

**方案**（取两者之长）:

1. 保留 YAML 资产作为**发布源**（声明式契约、AST 白名单校验不变）
2. 增加轻量激活层：`PromptRegistry` 增加激活版本索引（`active: {name: version}`），启动时从 SQLite/JSON 激活表恢复
3. 新增管理端点 `POST /v1/prompts/activate`（管理面开关控制 + 独立认证）：切换激活版本，不重启
4. 请求未显式指定 version 时走激活版本；显式 version 仍可用（不破坏现有契约）

**收益**: 不停机发布/回滚 Prompt；不牺牲模板安全治理。

**工作量**: 中（约 120 行 + 测试）。

**验收**: 激活切换立即生效；显式 version 语义不变；无认证时激活端点 401/不可达。

### 建议 12：配置增加 providers 独立段（可选复用）

**现状问题**: 同一供应商多模型时 `base_url`/`api_key_env` 重复声明，配置膨胀且易漂移（1-7 版 providers/models 两段式）。

**方案**:

1. `AppConfig` 增加可选 `providers: dict[str, ProviderConfig]`（base_url/api_key_env/timeout 等）
2. `ModelConfig` 支持 `provider: <name>` 引用（缺省回退到内嵌字段，向后兼容现有 config.yaml）
3. 启动校验：引用未知 provider 报错（参考 1-7 版 `validate_routes`）

**收益**: 配置去重；按供应商统一管理超时/密钥。

**工作量**: 中（兼容性要求高）。

**验收**: 现有 config.yaml 不加改动全部通过；新增 providers 段后内嵌字段可省略；引用未知 provider 启动报错。

### 建议 13：工程化补齐（uv / ruff / Docker / CI）

**现状问题**: 仅 `requirements.txt`（无版本约束），无 lint、无容器、无 CI，交付链路不完整（1-7 版完整）。

**方案**（照搬 1-7 版工程化骨架）:

1. `pyproject.toml`（hatchling/uv）+ `uv.lock`：依赖版本锁定，dev 依赖含 pytest/pytest-asyncio/ruff
2. `ruff` 配置：line-length 110，select E/F/I/B/UP
3. `Dockerfile`：python:3.12-slim、非 root 用户（10001）、`uvicorn --factory app.app:create_app`（注意分层版是工厂函数，与 1-7 版启动命令不同）
4. `docker-compose.yml` + `Makefile`（test/lint/build 目标）
5. 健康检查：容器 HEALTHCHECK 指向新增的 `/healthz`

**收益**: 可复现构建、质量门禁、一键部署。

**工作量**: 小（模板化工作）。

**验收**: `uv run pytest -q` 129 用例全绿；`uv run ruff check .` 无告警；`docker build` 成功且容器健康检查通过。

### 建议 14：错误响应 OpenAI 兼容模式（可选开关）

**现状问题**: 错误形状 `{"detail": {"code", "message"}}` 与 OpenAI SDK 客户端解析不兼容（SDK 期待 `{"error": {...}}`），facade 端点错误会以异常形式抛出而非结构化。

**方案**（参考 1-7 版 `error_payload` + `gateway_error_handler`）:

1. `api/errors.py` 增加响应形状开关：`api.error_shape: gateway | openai`，配置驱动
2. OpenAI 形状：`{"error": {"message", "type", "param", "code"}}`，同时保留稳定 code 映射（`error.code → type`）
3. 流式错误时 OpenAI facade 发 `data: {"error": ...}` + `[DONE]`（参考 1-7 版 `_sse_error`）

**收益**: SDK 客户端错误处理友好度对齐 OpenAI。

**工作量**: 中。

**验收**: 两种形状各跑一遍 API 错误测试；流式错误序列正确。

---

## 不建议采纳的 1-7 版设计（防回归清单）

| 1-7 版做法 | 不采纳原因 |
|---|---|
| `gateway.py` 单类集中编排（重试/结构化/流式三循环嵌套） | 正是分层版要消除的"上帝类"，保持 service 层按关注点拆分 |
| 结构化修复循环作为**默认唯一**策略 | 成本/延迟不可控；分层版本地有界修复为默认，修复循环仅为可选项（建议 9） |
| 别名路由替代能力路由 | 能力路由 + 显式模型硬校验是分层版核心优势，只做加法（建议 10 加权） |
| 透传式流式替代事件流 | 事件状态机（终态唯一）语义更清晰；透传仅作为未来工具调用流式的补充事件类型 |
| 调用方零认证开发模式放行 | 生产化建议 1 保持强制认证；开发模式可用显式 `dev_api_key` 配置 |
| Prompt 运行时 API 替代 YAML 资产 | YAML 契约 + AST 白名单安全更强；运行时 API 只做激活层（建议 11） |
| SQLite 每事件开连接写入 | 采用单连接/批量缓冲写，避免请求路径阻塞 |

## 实施顺序建议

1. **第一批（安全基线）**: 建议 1、2、7 —— 完成后才具备任何部署条件
2. **第二批（数据与流式）**: 建议 3、4、5 —— 依赖第一批的审计字段
3. **第三批（能力与体验）**: 建议 8、9、10、14 —— 独立可并行
4. **第四批（运营与交付）**: 建议 6、11、12、13 —— 其中 6 依赖 3，11 依赖 3 的持久层

每批完成即跑全量测试回归（当前 129 用例 + 新增用例），保证分层版既有优势（架构、留痕、deadline、测试金字塔）不退化。
