# pyJianYingDraft vendored 来源记录(第10期 效果模块迁移 阶段0)

## 上游信息

| 项目 | 值 |
|---|---|
| 上游仓库 | https://github.com/GuanYixuan/pyJianYingDraft |
| 锁定 commit | `1e228479dda361e312e85f6037002346cf6dca9a` |
| 版本 | 0.3.1(2026-10-14) |
| 引入日期 | 2026-11-21 |
| 许可证 | Apache License 2.0(见本目录 `LICENSE`) |
| 引入方式 | 整包源码 vendor(上游该 commit 无 pyproject.toml/setup.py,无法 pip 安装锁定 commit) |

## 目录

- `pyJianYingDraft/` — 上游包原样拷贝(**零修改**)。含 `assets/draft_content_template.json`、
  `assets/draft_meta_info.json`(DraftFolder.create_draft 模板)与全部 `metadata/` 枚举。
- 旧的 `vendor/pyJianYingDraft/metadata/`(仓库根另一份 5 文件旧版元数据)**保持不动**:
  它是 `scripts/build_resource_library.py` 的只读 AST 扫描源,升级会令旧 GUI 实证对
  新二进制不成立(scripts/build_resource_library.py "vendor 同名换资源"警示)。

## 运行时依赖(不在本目录,装进 .venv)

上游 requirements.txt 声明 `pymediainfo` / `imageio` / `uiautomation>=2`:

- `pymediainfo==7.0.1`(win_amd64 wheel 自带 MediaInfo.dll,`MediaInfo.can_parse()=True` 已验证)
- `imageio`(仅 gif 时长探测用到)
- `uiautomation`(纯 Python;`pyJianYingDraft/__init__.py:23` 顶层 import
  `jianying_controller` → 必须 presence 安装;本项目只 import 包不调用 GUI 自动导出,
  项目最终导出走 FFmpeg)

## 本地补丁台账

| 日期 | 补丁 | 说明 |
|---|---|---|
| 2026-11-21 | (无) | 原样引入,零修改 |

## 版本核查记录

| 日期 | 结论 |
|---|---|
| 2026-11-21 | 核查上游 main 分支(共 221 commits):最新提交即 `1e22847`(0.3.1 版本
  bump,2026-10-14),此后无新提交 —— **本目录锁定的就是上游最新分支 + 最新发布版**。
  维持钉版,不浮动 main。升级 SOP:换 commit → 重跑
  `scripts/build_sdk_compatibility.py` 资源交叉表 → 跑
  `tests/unit/test_jianying_sdk_phase1~7.py` 全部单测 → GUI 实证重验(vendor 换资源
  后旧 GUI 实证对新二进制不成立)→ 在本表登记。升级触发条件:①上游修复已知限制
  (花字 effect_id/resource_id 双 ID 分离接口、「快门」「水墨」枚举);②需要支持
  剪映 10.8 新版草稿(FallbackLoader)。 |

约束(迁移设计文档 §8.3):扩展 SDK(如花字 effect_id/resource_id 双 ID 接口)必须
在此表登记补丁差异并配回归测试;禁止跟随上游浮动 main。
