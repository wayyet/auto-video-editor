"""scripts/fix_draft_subtitle_wrap.py 专项单测(20261118 审查 P1-05)。

覆盖(设计审查 §7.2 清单):dry-run 不写盘 / 有效回填 / 空文本 / 坏 JSON /
多 style 跳过 / range 更新 / 幂等 / 双目录预检与独立事务 / 写异常回滚 /
布局违规回滚 / 进程 running+unknown 拒绝 / --canvas-width 校验。

全部用 tmp_path 假草稿 + 依赖注入 / monkeypatch,**不触碰真实用户草稿目录**;
进程检测由 conftest autouse fixture 打桩(不依赖本机剪映状态)。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "fix_draft_subtitle_wrap.py"


@pytest.fixture(scope="module")
def mod():
    """按路径加载脚本模块(scripts/ 不是包,与 test_resume_workflow_script 同法)。"""
    spec = importlib.util.spec_from_file_location("fix_draft_subtitle_wrap_script", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _no_jianying_running(mod, monkeypatch):
    """环境守卫:脚本模块的进程探测一律视为"未运行"(conftest 只打桩
    atomic_writer/draft_publisher 的默认门禁,不覆盖动态加载的脚本模块)。
    真实机器剪映开着时,--apply 用例也不环境性抖动;需要 running/unknown
    的用例在测试体内二次 monkeypatch 覆盖本桩。
    """
    monkeypatch.setattr(mod, "probe_jianying_state", lambda: "not_running")


# ---------------------------------------------------------------------------
# 假草稿构造助手
# ---------------------------------------------------------------------------

# 真机 39 字中文 cue(与 test_subtitle_wrap.py 的 ZH39 同源),size 5.0 下超预算
T39 = "我沿着栈桥往里走,白色拱廊、灰色立柱、绿色花纹地毯和金蓝穹顶,一样样收进眼里。"
# 40 个英文词(空格分隔),size 5.0 下超预算
TEN = " ".join(["word"] * 40)


def _native_mat(mid: str, text: str, size: float = 5.0) -> dict:
    """构造一条原生 content 形态的 materials.texts 条目。"""
    style = {"size": size, "range": [0, len(text)], "fill": "#FFFFFF"}
    content = json.dumps({"text": text, "styles": [style]}, ensure_ascii=False)
    return {"id": mid, "content": content, "type": "text", "check_flag": 15}


def _make_draft_dir(
    tmp_path: Path, name: str, mats: list, canvas: int = 1080
) -> Path:
    """写一份最小假草稿(draft_content.json 与 draft_info.json 字节一致)。"""
    d = tmp_path / name
    d.mkdir(parents=True)
    draft = {
        "canvas_config": {"width": canvas, "height": 1920},
        "materials": {"texts": mats},
        "tracks": [],
        "duration": 10_000_000,
    }
    payload = json.dumps(draft, ensure_ascii=False, indent=2)
    (d / "draft_content.json").write_text(payload, encoding="utf-8")
    (d / "draft_info.json").write_text(payload, encoding="utf-8")
    return d


def _read_content(d: Path) -> dict:
    return json.loads((d / "draft_content.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# dry-run / apply 基本语义
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(mod, tmp_path, capsys) -> None:
    """dry-run:打印折行明细但不落盘,退出码 0。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("text-zh-3", T39)])
    before = (d / "draft_content.json").read_bytes()

    rc = mod.main(["--draft-dir", str(d)])

    assert rc == 0
    assert (d / "draft_content.json").read_bytes() == before, "dry-run 不得写盘"
    assert not (d / ".snapshots").exists()
    out = capsys.readouterr().out
    assert "[折行]" in out and "dry-run" in out


def test_apply_wraps_and_updates_range(mod, tmp_path, capsys) -> None:
    """--apply:content 内嵌 \\n、styles[0].range 重置、双文件字节一致。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("text-zh-3", T39)])

    rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 0
    mat = _read_content(d)["materials"]["texts"][0]
    data = json.loads(mat["content"])
    assert "\n" in data["text"]
    assert data["styles"][0]["range"] == [0, len(data["text"])]
    assert (d / "draft_content.json").read_bytes() == (d / "draft_info.json").read_bytes()
    assert "已落盘" in capsys.readouterr().out


def test_apply_idempotent_second_run_no_change(mod, tmp_path, capsys) -> None:
    """同一草稿执行两次 --apply:第二次 changed=0、草稿内容不变。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("text-zh-3", T39)])

    assert mod.main(["--draft-dir", str(d), "--apply"]) == 0
    after_first = (d / "draft_content.json").read_bytes()

    assert mod.main(["--draft-dir", str(d), "--apply"]) == 0
    assert (d / "draft_content.json").read_bytes() == after_first, "幂等:第二次不改变内容"
    out = capsys.readouterr().out
    assert "共需折行 0 条" in out


def test_existing_newlines_never_merged(mod, tmp_path) -> None:
    """已含 \\n 的行永不合并(人工换行尊重):分类为 unchanged,内容不动。"""
    text = "第一行短句\n第二行短句"
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("text-zh-9", text)])

    assert mod.main(["--draft-dir", str(d), "--apply"]) == 0

    data = json.loads(_read_content(d)["materials"]["texts"][0]["content"])
    assert data["text"] == text, "已有换行必须原样保留"


# ---------------------------------------------------------------------------
# 跳过分类计数(§4.2 P2-04)
# ---------------------------------------------------------------------------


def test_plan_draft_counts_all_categories(mod, tmp_path) -> None:
    """六类素材混排 → 分类计数逐项正确(含 non_dict)。"""
    draft = {
        "canvas_config": {"width": 1080, "height": 1920},
        "materials": {
            "texts": [
                _native_mat("a", T39),                                   # wrapped
                _native_mat("b", ""),                                    # empty
                _native_mat("c", "短句"),                                 # unchanged
                _native_mat("d", "已有\n换行"),                            # unchanged
                {"id": "e", "content": "not json {{"},                   # unsupported_format
                {"id": "f", "content": json.dumps(                       # multi_style
                    {"text": T39, "styles": [{"size": 5.0}, {"size": 5.0}]},
                    ensure_ascii=False,
                )},
                "junk-string-entry",                                     # non_dict
            ]
        },
    }

    plan = mod._plan_draft(draft, 1080)

    assert plan["counts"] == {
        mod.KIND_WRAPPED: 1,
        mod.KIND_EMPTY: 1,
        mod.KIND_UNCHANGED: 2,
        mod.KIND_UNSUPPORTED: 1,
        mod.KIND_MULTI_STYLE: 1,
        mod.KIND_NON_DICT: 1,
    }


def test_empty_and_bad_json_safe_skipped_in_apply(mod, tmp_path, capsys) -> None:
    """空文本 / 坏 JSON 素材在 --apply 下安全跳过,不修改文件内容,退出码 0。"""
    d = _make_draft_dir(
        tmp_path, "zh", [_native_mat("keep", "短句"), _native_mat("empty", ""), {"id": "bad", "content": "{{{"}]
    )
    before = (d / "draft_content.json").read_bytes()

    rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "空文本 1 条" in out and "非原生 content 1 条" in out
    # 只有 wrapped 条目引发写入;无 wrapped → 不写入,字节不变
    assert (d / "draft_content.json").read_bytes() == before


def test_multi_style_content_untouched_in_apply(mod, tmp_path, capsys) -> None:
    """多 style 块:跳过并列出原因,不擅自改 range(§7.2)。"""
    mat = {
        "id": "ms",
        "content": json.dumps(
            {"text": T39, "styles": [{"size": 5.0, "range": [0, 10]}, {"size": 6.0, "range": [10, 39]}]},
            ensure_ascii=False,
        ),
    }
    d = _make_draft_dir(tmp_path, "zh", [mat])
    before = (d / "draft_content.json").read_bytes()

    rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 0
    assert (d / "draft_content.json").read_bytes() == before, "多 style 块不得被修改"
    out = capsys.readouterr().out
    assert "多 style 块 1 条" in out


# ---------------------------------------------------------------------------
# Preflight / 多目录(P1-06)
# ---------------------------------------------------------------------------


def test_preflight_missing_second_dir_blocks_first(mod, tmp_path, capsys) -> None:
    """第二目录缺 draft_content.json → 整体拒绝(exit 1),第一目录不得提前写入。"""
    d1 = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    d2 = tmp_path / "en"
    d2.mkdir()

    rc = mod.main(["--draft-dir", str(d1), "--draft-dir", str(d2), "--apply"])

    assert rc == 1
    before = json.loads(
        (d1 / "draft_content.json").read_text(encoding="utf-8")
    )
    assert "\n" not in json.loads(before["materials"]["texts"][0]["content"])["text"]
    assert "[preflight FAIL]" in capsys.readouterr().err


def test_dual_dir_independent_transactions(mod, tmp_path, monkeypatch, capsys) -> None:
    """双目录独立事务:第一份成功落盘,第二份布局违规回滚;exit 2 + 汇总准确。"""
    d1 = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    d2 = _make_draft_dir(tmp_path, "en", [_native_mat("t-en", TEN)])
    calls = {"n": 0}
    real_factory = mod._make_tx_verifier

    def counting_factory(canvas_width):
        calls["n"] += 1
        if calls["n"] >= 2:
            return lambda d: (False, f"{mod._LAYOUT_FAIL_TAG}: 注入超宽行")
        return real_factory(canvas_width)

    monkeypatch.setattr(mod, "_make_tx_verifier", counting_factory)

    rc = mod.main(["--draft-dir", str(d1), "--draft-dir", str(d2), "--apply"])

    assert rc == 2
    # 第一份已落盘(独立事务,不回退);第二份恢复原状
    zh = json.loads(json.loads((d1 / "draft_content.json").read_text(encoding="utf-8"))["materials"]["texts"][0]["content"])
    assert "\n" in zh["text"]
    en_before = json.loads(json.loads((d2 / "draft_content.json").read_text(encoding="utf-8"))["materials"]["texts"][0]["content"])
    assert "\n" not in en_before["text"]
    out = capsys.readouterr().out
    assert "1 落盘, 1 回滚, 0 失败" in out


# ---------------------------------------------------------------------------
# 进程状态门禁(P1-02 三态)
# ---------------------------------------------------------------------------


def test_apply_process_running_refuses(mod, tmp_path, monkeypatch, capsys) -> None:
    """probe=running → 拒绝(exit 1),不快照、不落盘。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    monkeypatch.setattr(mod, "probe_jianying_state", lambda: "running")

    rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 1
    assert not (d / ".snapshots").exists()
    assert "正在运行" in capsys.readouterr().err


def test_apply_process_unknown_refuses(mod, tmp_path, monkeypatch, capsys) -> None:
    """probe=unknown(检测命令失败/超时)→ 一律拒绝(exit 1)。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    monkeypatch.setattr(mod, "probe_jianying_state", lambda: "unknown")

    rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 1
    assert not (d / ".snapshots").exists()
    assert "状态未知" in capsys.readouterr().err


def test_dry_run_skips_process_gate(mod, tmp_path, monkeypatch) -> None:
    """dry-run 不做进程门禁(与旧版一致;probe 返回 running 也不影响)。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    monkeypatch.setattr(mod, "probe_jianying_state", lambda: "running")

    assert mod.main(["--draft-dir", str(d)]) == 0


# ---------------------------------------------------------------------------
# 事务内回滚(P1-01/P1-04)
# ---------------------------------------------------------------------------


def test_apply_write_exception_restores_draft(mod, tmp_path, monkeypatch, capsys) -> None:
    """双写中途失败 → 真实快照回滚,两文件恢复原字节,exit 1 + 失败汇总。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    original = (d / "draft_content.json").read_text(encoding="utf-8")

    real_replace = __import__("os").replace

    def selective_replace(src, dst):
        if Path(dst).name == "draft_info.json":
            raise OSError("simulated info write failure")
        return real_replace(src, dst)

    with patch("draft_ops.atomic_writer.os.replace", side_effect=selective_replace):
        rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 1
    assert (d / "draft_content.json").read_text(encoding="utf-8") == original, "回滚后必须恢复原字节"
    assert (d / "draft_info.json").read_text(encoding="utf-8") == original
    err = capsys.readouterr().err
    assert "simulated info write failure" in err and "已从快照" in err


def test_apply_layout_violation_rolls_back_exit2(mod, tmp_path, monkeypatch, capsys) -> None:
    """布局自校验违规 → 事务内整目录回滚,exit 2,草稿保持原状(P1-04)。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])
    original = (d / "draft_content.json").read_text(encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "_make_tx_verifier",
        lambda w: (lambda dd: (False, f"{mod._LAYOUT_FAIL_TAG}: 注入超宽行")),
    )

    rc = mod.main(["--draft-dir", str(d), "--apply"])

    assert rc == 2
    assert (d / "draft_content.json").read_text(encoding="utf-8") == original, "违规必须回滚到快照"
    err = capsys.readouterr().err
    assert mod._LAYOUT_FAIL_TAG in err and "已从快照" in err


def test_tx_verifier_flags_overwide_line_on_disk(mod, tmp_path) -> None:
    """组合校验器对磁盘上真实超宽行返回 False(布局 tag + 行明细)。"""
    overwide = _native_mat("ow", "超" * 60)  # 60 全角单位 ≫ 26,必超预算
    d = _make_draft_dir(tmp_path, "zh", [overwide])

    ok, reason = mod._make_tx_verifier(1080)(d)

    assert ok is False
    assert mod._LAYOUT_FAIL_TAG in reason
    assert "id=ow" in reason


def test_tx_verifier_passes_wrapped_draft(mod, tmp_path) -> None:
    """正常折行后的草稿 → 组合校验器通过(ok=True)。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])

    assert mod.main(["--draft-dir", str(d), "--apply"]) == 0
    ok, reason = mod._make_tx_verifier(1080)(d)

    assert ok is True and reason == "ok"


# ---------------------------------------------------------------------------
# --canvas-width 校验与口径统一(P2-06 + 口径一致性)
# ---------------------------------------------------------------------------


def test_canvas_width_zero_rejected_by_argparse(mod, tmp_path) -> None:
    """--canvas-width 0 / 负数 → argparse 直接报错(exit 2),不进入处理。"""
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", T39)])

    with pytest.raises(SystemExit) as excinfo:
        mod.main(["--draft-dir", str(d), "--canvas-width", "0"])

    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        mod.main(["--draft-dir", str(d), "--canvas-width", "-720"])


def test_canvas_width_override_changes_wrapping(mod, tmp_path, capsys) -> None:
    """显式画布宽生效:同一文本 1080 下不折、720 下折(预算 = 画布×0.78/字号)。"""
    text = "这" * 20  # 20 单位:1080 预算 26 放得下,720 预算 17 放不下
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", text)])

    assert mod.main(["--draft-dir", str(d)]) == 0
    assert "[unchanged] 无需折行 1 条" in capsys.readouterr().out

    assert mod.main(["--draft-dir", str(d), "--canvas-width", "720"]) == 0
    assert "[折行]" in capsys.readouterr().out


def test_verify_uses_same_canvas_as_wrap(mod, tmp_path) -> None:
    """口径统一:--canvas-width 720 折行后,按 720 口径自校验必须通过。"""
    text = "这" * 40
    d = _make_draft_dir(tmp_path, "zh", [_native_mat("t", text)])

    assert mod.main(["--draft-dir", str(d), "--canvas-width", "720", "--apply"]) == 0

    # 旧版缺陷场景:折行按 720,自校验却按草稿 1080 —— 若口径不一致,720 折出
    # 的行在 1080 口径下必然通过而失去校验意义;这里直接用 720 口径复核。
    ok, reason = mod._make_tx_verifier(720)(d)
    assert ok is True and reason == "ok"
    data = json.loads(_read_content(d)["materials"]["texts"][0]["content"])
    assert "\n" in data["text"]


# ---------------------------------------------------------------------------
# 布局告警接线(P2-04)
# ---------------------------------------------------------------------------


def test_plan_reports_cps_warning_for_short_duration(mod, tmp_path, capsys) -> None:
    """超长文本 + 极短时长 → 折行明细带 [告警] 阅读速度提示;普通字幕不误报。"""
    mat = {
        **_native_mat("fast", TEN),
        "target_timerange": {"start": 0, "duration": 1_000_000},  # 1 秒 157+ 字
    }
    d = _make_draft_dir(tmp_path, "en", [mat])

    assert mod.main(["--draft-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "[告警]" in out and "阅读速度" in out


def test_plan_no_warning_for_normal_cue(mod, tmp_path, capsys) -> None:
    """正常 39 字中文 cue(时长 4s)→ 无任何 [告警](零误报)。"""
    mat = {
        **_native_mat("t", T39),
        "target_timerange": {"start": 0, "duration": 4_000_000},
    }
    d = _make_draft_dir(tmp_path, "zh", [mat])

    assert mod.main(["--draft-dir", str(d)]) == 0
    assert "[告警]" not in capsys.readouterr().out
