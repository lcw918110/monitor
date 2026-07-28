"""主机类型归一化：仅 auto / cpu / gpu。"""

from __future__ import annotations

# 历史值兼容
_LEGACY = {
    "app": "cpu",  # 原「应用机」→ CPU 类
    "npu": "gpu",  # 原单独 NPU 类型并入加速卡（gpu）类
}


def normalize_host_type(value: str | None) -> str:
    v = (value or "auto").strip().lower()
    v = _LEGACY.get(v, v)
    if v not in ("auto", "cpu", "gpu"):
        return "auto"
    return v


def resolve_auto_host_type(has_accelerators: bool) -> str:
    return "gpu" if has_accelerators else "cpu"
