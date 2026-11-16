"""模型能力枚举与各 adapter 的能力声明。"""

from enum import StrEnum


class Capability(StrEnum):
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"


ADAPTER_CAPABILITIES: dict[str, set[Capability]] = {
    "openai_compatible": {Capability.STREAMING, Capability.STRUCTURED_OUTPUT},
}
