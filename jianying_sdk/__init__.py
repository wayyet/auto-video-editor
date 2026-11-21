"""jianying_sdk:pyJianYingDraft 适配层对外入口(第10期效果模块迁移)。

职责边界(迁移设计文档 §1.2/§8.1):LangGraph 主图、业务节点、资源准入、
安全发布全部留在项目内;本层只负责 **DraftBuildSpec → ScriptFile → 载荷**
的对象化构建。发布一律走 :func:`jianying_sdk.publisher.publish_compiled_draft`
(内部接 ``safe_write_draft``),禁止直接调用 ``ScriptFile.save()``。
"""
from __future__ import annotations

from jianying_sdk._sdk_loader import SDK_COMMIT, SDK_ROOT, SDK_VERSION, ensure_sdk_on_path, sdk
from jianying_sdk.compiler import (
    COMPILER_VERSION,
    CompileResult,
    FingerprintUnchangedError,
    compile_spec,
    new_script,
)
from jianying_sdk.fingerprint import compute_build_fingerprint, needs_compile
from jianying_sdk.publisher import publish_compiled_draft, validate_sdk_payload
from jianying_sdk.spec import DraftBuildSpec

__all__ = [
    "COMPILER_VERSION",
    "SDK_COMMIT",
    "SDK_ROOT",
    "SDK_VERSION",
    "CompileResult",
    "DraftBuildSpec",
    "FingerprintUnchangedError",
    "compile_spec",
    "compute_build_fingerprint",
    "ensure_sdk_on_path",
    "needs_compile",
    "new_script",
    "publish_compiled_draft",
    "sdk",
    "validate_sdk_payload",
]
