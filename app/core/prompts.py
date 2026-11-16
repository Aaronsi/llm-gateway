"""Prompt 模板资产：包/变量模型、沙箱渲染、注册表与引擎。

模板只能由网关管理员发布（YAML 资产），调用方仅可选择模板 + 提交变量，
绝不能提交或覆盖模板正文。
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import jinja2.nodes
import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import GatewayError
from app.core.schemas import PromptSelection


class PromptVariable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "str"
    max_length: int = Field(default=1000, ge=1)


class PromptPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    status: str = "published"
    system_template: str
    variables: dict[str, PromptVariable] = Field(default_factory=dict)
    context_budget: int | None = None


class PromptRegistry:
    def __init__(self) -> None:
        self._packages: dict[tuple[str, str], PromptPackage] = {}
        # 激活索引：{name: version}；未显式指定 version 的请求走激活版本。
        self._active: dict[str, str] = {}

    def register(self, package: PromptPackage) -> None:
        self._packages[(package.name, package.version)] = package

    def get(self, name: str, version: str) -> PromptPackage | None:
        return self._packages.get((name, version))

    def activate(self, name: str, version: str) -> str:
        # 运行时激活：只允许切换到已注册版本；同进程共享引用，切换立即生效。
        if (name, version) not in self._packages:
            raise GatewayError(
                "unknown_prompt_version", f"未注册的模板版本: {name}:{version}", 400
            )
        self._active[name] = version
        return version

    def active_version(self, name: str) -> str | None:
        return self._active.get(name)

    def restore_active(self, active: dict[str, str]) -> None:
        # 启动恢复：忽略已下线的 (name, version)，只恢复仍然注册的组合。
        for name, version in active.items():
            if (name, version) in self._packages:
                self._active[name] = version

    def snapshot_active(self) -> dict[str, str]:
        return dict(self._active)

    @classmethod
    def load(cls, assets_dir: str | Path) -> "PromptRegistry":
        # 递归加载 YAML 资产；仅发布态（published）模板对外可用。
        registry = cls()
        for path in sorted(Path(assets_dir).rglob("*.yaml")):
            package = PromptPackage.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            if package.status == "published":
                registry.register(package)
        return registry


# 仅允许输出/变量/条件分支等纯渲染节点；循环、宏、导入、函数调用、属性/下标访问一律拒绝。
_FORBIDDEN_NODES = ("For", "Macro", "Import", "Filter", "Call", "Getattr", "Getitem")


class SafePromptRenderer:
    def __init__(self) -> None:
        self._env = SandboxedEnvironment(undefined=StrictUndefined)

    def render(self, template: str, variables: dict[str, object]) -> str:
        try:
            ast = self._env.parse(template)
        except Exception as exc:
            raise GatewayError("invalid_prompt_template", f"模板语法非法: {exc}", 400) from exc
        for node in ast.find_all(jinja2.nodes.Node):
            if type(node).__name__ in _FORBIDDEN_NODES:
                raise GatewayError(
                    "invalid_prompt_template",
                    f"模板包含禁用节点: {type(node).__name__}",
                    400,
                )
        try:
            return self._env.from_string(template).render(**variables)
        except Exception as exc:
            raise GatewayError("invalid_prompt_template", f"模板渲染失败: {exc}", 400) from exc


@dataclass(frozen=True)
class AppliedPrompt:
    package: PromptPackage
    rendered: str
    hash: str


class PromptEngine:
    # 变量校验（缺失/控制字符/超长）→ 沙箱渲染 → Context 预算检查。
    def __init__(
        self,
        registry: PromptRegistry,
        chars_per_token_estimate: float = 2.0,
    ) -> None:
        self._registry = registry
        self._renderer = SafePromptRenderer()
        self._chars_per_token = chars_per_token_estimate

    def apply(self, selection: PromptSelection) -> AppliedPrompt:
        version = selection.version
        if version is None:
            version = self._registry.active_version(selection.name)
        if version is None:
            raise GatewayError(
                "unknown_prompt_template",
                f"未注册的模板: {selection.name}（未指定 version 且无激活版本）",
                400,
            )
        package = self._registry.get(selection.name, version)
        if package is None:
            raise GatewayError(
                "unknown_prompt_template",
                f"未注册的模板: {selection.name}:{selection.version}",
                400,
            )
        for name in package.variables:
            if name not in selection.variables:
                raise GatewayError("missing_prompt_variable", f"缺少变量: {name}", 400)
        rendered_variables: dict[str, str] = {}
        for name, variable in package.variables.items():
            value = selection.variables[name]
            if any(ord(ch) < 32 for ch in value):
                raise GatewayError("invalid_prompt_variable", f"变量 {name} 含控制字符", 400)
            if len(value) > variable.max_length:
                raise GatewayError("prompt_variable_too_long", f"变量 {name} 超出长度上限", 400)
            rendered_variables[name] = value
        rendered = self._renderer.render(package.system_template, rendered_variables)
        if package.context_budget is not None:
            estimated_tokens = len(rendered) / self._chars_per_token
            if estimated_tokens > package.context_budget:
                raise GatewayError("context_budget_exceeded", "渲染结果超出 Context 预算", 400)
        return AppliedPrompt(
            package=package,
            rendered=rendered,
            hash=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )
