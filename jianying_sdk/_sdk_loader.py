"""pyJianYingDraft vendored SDK 的统一加载入口(第10期效果模块迁移 阶段1)。

上游 SDK 以**零修改 vendor** 引入在 ``vendor/pyJianYingDraft/sdk``
(来源与补丁台账见该目录 ``PROVENANCE.md``);上游该 commit 无打包文件,
不能 pip 安装锁定版本,因此用 sys.path 注入方式导入。

全仓只有本模块允许触碰 SDK 的 sys.path;其余模块一律经由 :func:`sdk` /
:func:`load_sdk_module` 取用,保证:

- 路径解析基于本文件位置(与工作目录无关,测试/服务/节点三种入口一致);
- 注入幂等(重复 import 不叠加 sys.path);
- 失败信息可定位(缺目录/缺包分别报错)。
"""
from __future__ import annotations

import sys
from pathlib import Path

#: vendored SDK 根(jianying_sdk/ 的上级 / vendor/pyJianYingDraft/sdk)
SDK_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "pyJianYingDraft" / "sdk"

#: 锁定的上游 commit(PROVENANCE.md 同源;禁止跟随上游浮动 main)
SDK_COMMIT = "1e228479dda361e312e85f6037002346cf6dca9a"
SDK_VERSION = "0.3.1"

_path_injected = False


def ensure_sdk_on_path() -> None:
    """把 vendored SDK 根注入 ``sys.path``(幂等;目录缺失 fail-fast)。"""
    global _path_injected
    if _path_injected:
        return
    package_dir = SDK_ROOT / "pyJianYingDraft"
    if not package_dir.is_dir():
        raise RuntimeError(
            f"vendored pyJianYingDraft 缺失: {package_dir} 不存在;"
            "第10期阶段0 引入的 vendor 目录被破坏,请核对 git 状态"
        )
    path = str(SDK_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)
    _path_injected = True


def sdk():
    """导入并返回 ``pyJianYingDraft`` 模块对象。"""
    ensure_sdk_on_path()
    import pyJianYingDraft as _sdk

    return _sdk


__all__ = ["SDK_COMMIT", "SDK_ROOT", "SDK_VERSION", "ensure_sdk_on_path", "sdk"]
