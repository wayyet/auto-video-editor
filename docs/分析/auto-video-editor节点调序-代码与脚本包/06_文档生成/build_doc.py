#!/usr/bin/env python3
"""用真实运行输出 / 图 / 清单填充设计文档模板,并对引用的数字做断言(数字不对就直接报错)。

用法:
    python build_doc.py <包根目录> <inventory_raw.json> <输出md>
例:
    python build_doc.py . .\\06_文档生成\\inventory_raw.json .\\设计执行计划.md

包根目录需包含:
    01_代码改动/graph.py      03_实证脚本/运行输出/*.txt     04_分析脚本/graph_extract_*.json、reach_*.txt
    05_图表/图*.mmd           06_文档生成/template.md        07_测试记录/*.txt
    04_分析脚本/ret_kinds_new.txt
"""
import ast
import json
import re
import sys
import textwrap
from pathlib import Path

PKG, INV, DST = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
OUT = PKG / "03_实证脚本" / "运行输出"
ANA = PKG / "04_分析脚本"
LOG = PKG / "07_测试记录"
T = (PKG / "06_文档生成" / "template.md").read_text(encoding="utf-8")


def body(name):
    t = (OUT / name).read_text(encoding="utf-8")
    return t.split("\n", 2)[2] if t.startswith("#") else t


def parse_steps(name):
    steps = {}
    for ln in body(name).splitlines():
        m = re.match(r"^step\s+(\d+):\s+(\[.*\])\s*$", ln)
        if m:
            seen = []
            for n in ast.literal_eval(m.group(2)):
                if n not in seen:
                    seen.append(n)
            steps[int(m.group(1))] = seen
    return steps


def first(steps):
    f = {}
    for s in sorted(steps):
        for n in steps[s]:
            f.setdefault(n, s)
    return f


bn = parse_steps("2_整图超步顺序_调序前_normal.txt")
an = parse_steps("2_整图超步顺序_调序后_normal.txt")
ac = parse_steps("2_整图超步顺序_调序后_cp3.txt")
B, A, C = first(bn), first(an), first(ac)

# ---- 事实断言:模板里引用的 step 号必须与实测输出一致 ----
exp_b = {"node_08_add_subtitles": 17, "fork_draft_for_english_branch": 17, "node_09_inject_fx": 18, "node_16a_translate_and_check": 18,
         "node_10_inject_text_fx": 19, "node_17_inject_english_tts": 19, "node_11_inject_sticker": 20, "node_12_human_add_bgm": 21,
         "node_13_adjust_volume": 22, "node_14_make_covers": 23, "node_15_localize_covers_en": 24, "join_before_delivery": 25}
for k, v in exp_b.items():
    assert B[k] == v, ("调序前", k, B[k], v)
exp_a = {"node_08_add_subtitles": 17, "node_10_inject_text_fx": 18, "fork_draft_for_english_branch": 18, "node_14_make_covers": 19,
         "node_16a_translate_and_check": 19, "node_08b_add_subtitles_en": 20, "node_15_localize_covers_en": 20, "node_10b_inject_text_fx_en": 21,
         "node_17_inject_english_tts": 22, "node_09_inject_fx": 23, "node_11_inject_sticker": 24, "node_12_human_add_bgm": 25,
         "node_13_adjust_volume": 26, "join_before_delivery": 27}
for k, v in exp_a.items():
    assert A[k] == v, ("调序后", k, A[k], v)
assert C["node_checkpoint3_layout_review"] < C["node_10b_inject_text_fx_en"] < C["node_17_inject_english_tts"] < C["node_09_inject_fx"] \
    < C["node_12_human_add_bgm"] < C["node_13_adjust_volume"] < C["join_before_delivery"]
# 屏障:09 严格晚于 15 与 17,且只出现 1 次
assert A["node_09_inject_fx"] > max(A["node_15_localize_covers_en"], A["node_17_inject_english_tts"])
assert sum(n.count("node_09_inject_fx") for n in an.values()) == 1

rep = {
    "S_AFTER_09": A["node_09_inject_fx"], "S_AFTER_15": A["node_15_localize_covers_en"], "S_AFTER_17": A["node_17_inject_english_tts"],
    "S_BEFORE_16A": B["node_16a_translate_and_check"], "S_BEFORE_08": B["node_08_add_subtitles"],
    "S_BEFORE_09": B["node_09_inject_fx"], "S_BEFORE_17": B["node_17_inject_english_tts"],
    "S_CP3_CP3": C["node_checkpoint3_layout_review"], "S_CP3_10B": C["node_10b_inject_text_fx_en"], "S_CP3_17": C["node_17_inject_english_tts"],
    "S_CP3_09": C["node_09_inject_fx"], "S_CP3_12": C["node_12_human_add_bgm"], "S_CP3_13": C["node_13_adjust_volume"],
    "S_CP3_JOIN": C["join_before_delivery"],
}
rep = {k: str(v) for k, v in rep.items()}

MIN_STEP = B["bridge_snapshot2"]          # 只展示快照②及之后的超步
rep["BEFORE_MIN_STEP"] = str(MIN_STEP)
rep["BEFORE_STEPS"] = "\n".join(f"step {s}: {' ‖ '.join(bn[s])}" for s in sorted(bn) if s >= MIN_STEP)

m = re.search(r"出错时最后记录的超步 step (\d+)", body("2_整图超步顺序_调序前_cp3.txt"))
assert m, "调序前 / 关卡③ 路径没有记录到并发写冲突"
rep["S_BEFORE_CP3_ERR"] = m.group(1)
assert "InvalidUpdateError" in body("2_整图超步顺序_调序前_cp3.txt")

rows = [("node_08_add_subtitles", "node_08 中文字幕"), ("node_10_inject_text_fx", "node_10 花字(中)"), ("fork_draft_for_english_branch", "fork 英文分支草稿"),
        ("node_14_make_covers", "node_14 封面"), ("node_16a_translate_and_check", "node_16a 英文翻译 + 布局检查"),
        ("node_08b_add_subtitles_en", "node_08b 英文字幕写入(新增)"), ("node_15_localize_covers_en", "node_15 封面英文本地化"),
        ("node_10b_inject_text_fx_en", "node_10b 花字(英)(新增)"), ("node_17_inject_english_tts", "node_17 英文配音"),
        ("node_09_inject_fx", "node_09 转场 / 特效"), ("node_11_inject_sticker", "node_11 贴纸"), ("node_12_human_add_bgm", "node_12 关卡② BGM"),
        ("node_13_adjust_volume", "node_13 音量"), ("join_before_delivery", "join_before_delivery(QA)")]
tb = ["| 节点 | 调序前 step | 调序后 step | 变化 |", "|---|---|---|---|"]
for k, lab in rows:
    b, a = B.get(k), A.get(k)
    ch = "新增" if b is None else ("不变" if a == b else f"{a - b:+d}")
    tb.append(f"| {lab} | {b if b is not None else '—'} | {a} | {ch} |")
rep["STEP_TABLE"] = "\n".join(tb)


def zh_counts(name):
    mm = re.search(r"中文草稿 materials\.texts 条数 = (\d+) \| transitions=(\d+) \| video_effects=(\d+) \| stickers=(\d+)", body(name))
    return mm.groups()


zb, za = zh_counts("2_整图超步顺序_调序前_normal.txt"), zh_counts("2_整图超步顺序_调序后_normal.txt")
assert zb == za, (zb, za)
rep["ZH_COUNTS"] = " / ".join(za)

# ---- 并行安全:主链节点是"整份 state"还是"只返回变更字段"(来自 ret_kinds_new.txt),并核对同超步分组 ----
rk = {}
for ln in (ANA / "ret_kinds_new.txt").read_text(encoding="utf-8").splitlines():
    if ln.startswith("#") or not ln.strip():
        continue
    parts = ln.split()
    rk[parts[0]] = parts[1]
main_rk = {k: v for k, v in rk.items() if not k.startswith(("storyline_", "assembly_"))}
FULL_SET = {"clean_cache", "launch_openstoryline", "open_preview", "checkpoint0_storyline_plan", "import_video", "get_storyboard_plan",
            "generate_draft", "node_06_human_reorder", "node_07_speed_fit", "node_08_add_subtitles", "node_09_inject_fx",
            "node_10_inject_text_fx", "node_11_inject_sticker", "node_12_human_add_bgm", "node_13_adjust_volume", "bridge_snapshot2",
            "escalate_guardrail_failure"}
DELTA_SET = {"fork_draft_for_english_branch", "node_14_make_covers", "node_15_localize_covers_en", "node_16a_translate_and_check",
             "node_08b_add_subtitles_en", "node_checkpoint3_layout_review", "node_10b_inject_text_fx_en", "node_17_inject_english_tts",
             "join_before_delivery"}
assert {k for k, v in main_rk.items() if v == "FULL"} == FULL_SET, main_rk
assert {k for k, v in main_rk.items() if v == "DELTA"} == DELTA_SET, main_rk
assert len(main_rk) == 26 == len(FULL_SET) + len(DELTA_SET)


def parallel_groups(steps):
    """step >= MIN_STEP 里,同超步含 2 个及以上不同节点的分组;并断言没有两个"整份 state"节点同超步。"""
    out = []
    for st in sorted(steps):
        if st < MIN_STEP:
            continue
        grp = sorted(set(steps[st]))
        if len(grp) >= 2:
            assert sum(main_rk[n] == "FULL" for n in grp) <= 1, ("同超步有 2 个以上整份 state 节点", st, grp)
            out.append((st, grp))
    return out


exp_groups = [(A["node_10_inject_text_fx"], ["fork_draft_for_english_branch", "node_10_inject_text_fx"]),
              (A["node_14_make_covers"], ["node_14_make_covers", "node_16a_translate_and_check"]),
              (A["node_15_localize_covers_en"], ["node_08b_add_subtitles_en", "node_15_localize_covers_en"])]
assert parallel_groups(an) == exp_groups, parallel_groups(an)
assert [g for _, g in parallel_groups(ac)] == [g for _, g in exp_groups], parallel_groups(ac)
mixed = [g for _, g in exp_groups if {main_rk[n] for n in g} == {"FULL", "DELTA"}]
assert mixed == [["fork_draft_for_english_branch", "node_10_inject_text_fx"]], mixed
# 调序前的 08 ‖ fork 同一超步(正文引用)
assert B["node_08_add_subtitles"] == B["fork_draft_for_english_branch"]
assert main_rk["node_08_add_subtitles"] == "FULL" and main_rk["fork_draft_for_english_branch"] == "DELTA"
rep.update(RK_MAIN_N=str(len(main_rk)), RK_FULL_N=str(len(FULL_SET)), RK_DELTA_N=str(len(DELTA_SET)),
           S_AFTER_10=str(A["node_10_inject_text_fx"]))

# ---- 图结构数字:来自 AST 提取 JSON 与 reach.py 输出 ----
old_j = json.loads((ANA / "graph_extract_old.json").read_text(encoding="utf-8"))
new_j = json.loads((ANA / "graph_extract_new.json").read_text(encoding="utf-8"))
N_OLD, N_NEW = len(old_j["nodes"]), len(new_j["nodes"])
assert N_NEW == N_OLD + 2, (N_OLD, N_NEW)
rep.update(N_OLD=str(N_OLD), N_NEW=str(N_NEW), N_MAIN_OLD=str(N_OLD - 27), N_MAIN_NEW=str(N_NEW - 27),
           E_OLD=str(len(old_j["edges"])), C_OLD=str(len(old_j["cond"])))
ro = (ANA / "reach_old.txt").read_text(encoding="utf-8")
rn = (ANA / "reach_new.txt").read_text(encoding="utf-8")
m = re.search(r"QC_GATE=True\s+mode=human 可达=\s*(\d+)", ro)
rep["R_HUMAN"] = m.group(1)
m = re.search(r"QC_GATE=True\s+mode=auto\s+可达=\s*(\d+)", ro)
rep["R_AUTO"] = m.group(1)
assert re.search(r"四种配置都不可达 4\b", ro) and re.search(r"四种配置都不可达 4\b", rn)
assert "'clean_cache', 'storyline_search_web_topic', 'storyline_generate_ai_transition', 'storyline_plan_timeline_ai_transition'" in ro

# ---- 测试数字:来自 07_测试记录 ----
def summ(name, kind):
    t = (LOG / name).read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    nums = {k: int(v) for v, k in re.findall(r"(\d+) (failed|passed|skipped|deselected|xfailed|xpassed)", t)}
    if kind == "unit":
        txt = f"{nums['passed']} passed / {nums['failed']} failed / {nums['skipped']} skipped"
    else:
        txt = (f"{nums['passed']} passed / {nums.get('failed', 0)} failed / {nums['skipped']} skipped / {nums['deselected']} deselected"
               f" / {nums['xfailed']} xfailed / {nums['xpassed']} xpassed")
    return nums, txt


def fails(name):
    return sorted(ln.split(" - ")[0].strip() for ln in (LOG / name).read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("FAILED"))


ub, ub_t = summ("before_unit.txt", "unit")
ua, ua_t = summ("after_unit.txt", "unit")
ib, ib_t = summ("before_integration.txt", "int")
ia, ia_t = summ("after_integration.txt", "int")
assert fails("before_unit.txt") == fails("after_unit.txt") and len(fails("before_unit.txt")) == 3, "单元失败名单必须调序前后一致(3 条)"
assert fails("before_integration.txt") == fails("after_integration.txt") and len(fails("before_integration.txt")) == 1, "集成失败名单必须一致(1 条)"
assert ub == ua, (ub, ua)
assert ia["passed"] == ib["passed"] + 4 and {k: v for k, v in ia.items() if k != "passed"} == {k: v for k, v in ib.items() if k != "passed"}
rep.update(UNIT_BEFORE=ub_t, UNIT_AFTER=ua_t, INT_BEFORE=ib_t, INT_AFTER=ia_t,
           UNIT_BEFORE_PASSED=str(ub["passed"]), UNIT_AFTER_PASSED=str(ua["passed"]),
           INT_BEFORE_PASSED=str(ib["passed"]), INT_AFTER_PASSED=str(ia["passed"]))
env = dict(ln.split("=", 1) for ln in (LOG / "环境.txt").read_text(encoding="utf-8").splitlines() if "=" in ln)
rep.update(PY_VER=env["python"], LG_VER=env["langgraph"], PT_VER=env["pytest"])

# ---- 关键接线:取自包内的 graph.py ----
g = (PKG / "01_代码改动" / "graph.py").read_text(encoding="utf-8")
i, j = g.index("    # ---- 2026-10 调序"), g.index("    # join → END")
rep["CODE_EDGES"] = textwrap.dedent(g[i:j]).rstrip()

for n in range(1, 5):
    f = next((PKG / "05_图表").glob(f"图{n}_*.mmd"))
    rep[f"DIAGRAM_{n}"] = "```mermaid\n" + f.read_text(encoding="utf-8").rstrip() + "\n```"

# ---- 全节点清单 ----
inv = {r["name"]: r for r in json.loads(INV.read_text(encoding="utf-8"))}
MAIN = [
    ("clean_cache", "清理缓存;START 无入边,仅 Web UI 按钮触发", "未接线"),
    ("launch_openstoryline", "启动 OpenStoryline 服务并等待就绪", "不变"),
    ("open_preview", "打开 OpenStoryline Web 预览;按模式分流", "不变"),
    ("checkpoint0_storyline_plan", "关卡⓪:人工确认分镜计划(人工模式)", "不变"),
    ("import_video", "等网页【导入视频】按钮(轮询握手文件,只发请求 + 等结果,不碰媒体)", "不变(基线新增节点)"),
    ("get_storyboard_plan", "读 OpenStoryline 的 plan_timeline_pro 产物,拍平为 CanonicalTimeline", "不变(基线新增节点)"),
    ("generate_draft", "生成剪映初始草稿(`graph.py` 内嵌包装,调 `nodes/node_05_generate_draft.py`)", "不变"),
    ("node_06_human_reorder", "关卡①:人工调整分镜顺序", "不变"),
    ("node_07_speed_fit", "变速压缩到 ≤35s;未达标自循环重试", "不变"),
    ("escalate_guardrail_failure", "护栏重试耗尽后的兜底终止", "不变"),
    ("bridge_snapshot2", "把快照②路径写入 state", "不变"),
    ("node_08_add_subtitles", "ASR + 中文字幕写入;写出 `asr_segments_zh`", "代码调整(行为不变)"),
    ("node_10_inject_text_fx", "花字 / 字幕动画(中文草稿)", "顺序调整 + 代码调整"),
    ("node_14_make_covers", "三比例封面并发渲染", "顺序调整(提前)"),
    ("node_15_localize_covers_en", "封面英文本地化", "顺序调整(提前)"),
    ("fork_draft_for_english_branch", "复制快照②为英文分支草稿", "顺序调整(入边改为 08)"),
    ("node_16a_translate_and_check", "英文字幕翻译 + 布局检查", "不变(出边改为 → 08b)"),
    ("node_08b_add_subtitles_en", "英文字幕写入英文分支草稿", "**新增**"),
    ("node_checkpoint3_layout_review", "关卡③:人工校对英文字幕排版(条件触发)", "不变(入边改为来自 08b)"),
    ("node_10b_inject_text_fx_en", "花字 / 字幕动画(英文草稿)", "**新增**"),
    ("node_17_inject_english_tts", "英文配音:合成音频文件(不写草稿)", "不变"),
    ("node_09_inject_fx", "转场 / 视频特效注入", "顺序调整(推迟到 17 之后)"),
    ("node_11_inject_sticker", "贴纸注入", "顺序调整(推迟)"),
    ("node_12_human_add_bgm", "关卡②:人工添加 BGM", "顺序调整(推迟)"),
    ("node_13_adjust_volume", "音量 / 淡入淡出", "顺序调整(推迟)"),
    ("join_before_delivery", "交付前 QA 闸门:检查交付物是否齐全", "顺序调整(改为尾链末端)+ 说明更新"),
]
QC = [("assembly_discover_and_probe", "发现并探测素材", "不变"), ("assembly_asr_and_visual_observe", "ASR 与视觉观察", "不变"),
      ("assembly_build_timeline", "构建时间线", "不变"), ("assembly_validate_render_qc", "校验 / 渲染 / QC", "不变"),
      ("assembly_repair_loop", "QC 未通过时的修复循环", "不变"), ("assembly_write_report", "写 QC 报告", "不变")]
ST = [("storyline_load_media", "加载素材", "不变"), ("storyline_search_media", "搜索素材(已配 Pexels Key 时才走)", "不变"),
      ("storyline_search_web_topic", "搜索话题", "未接线(无任何边)"), ("storyline_split_shots", "镜头切分", "不变"),
      ("storyline_local_asr", "本地 ASR", "路由永不选中"), ("storyline_speech_rough_cut", "口播粗剪", "路由永不选中"),
      ("storyline_generate_ai_transition", "AI 转场生成", "未接线(无任何边)"), ("storyline_understand_clips", "理解片段", "不变"),
      ("storyline_filter_clips", "筛选片段", "不变"), ("storyline_group_clips", "片段分组", "不变"), ("storyline_generate_script", "生成文案", "不变"),
      ("storyline_script_template_recommendation", "文案模板推荐(叶子节点,无出边)", "不变"), ("storyline_generate_voiceover", "生成配音", "不变"),
      ("storyline_select_bgm", "选择 BGM", "不变"), ("storyline_recommend_transition", "推荐转场", "不变"), ("storyline_recommend_text", "推荐花字", "不变"),
      ("storyline_plan_timeline_pro", "排时间线(pro)", "不变"), ("storyline_plan_timeline_ai_transition", "排时间线(AI 转场)", "未接线(无任何边)"),
      ("storyline_render_video", "渲染视频", "不变"), ("storyline_qa_gate", "QA 闸门:需回退时回到分组重跑,否则经渲染到汇合", "不变"),
      ("storyline_join", "汇合:反序列化 timeline_plan 到 storyline_plan", "不变")]


def table(items, seq):
    out = ["| " + ("序 | " if seq else "") + "节点 | 文件 | 职责 | 状态 |", "|" + ("---|" if seq else "") + "---|---|---|---|"]
    for i2, (name, role, st) in enumerate(items, 1):
        f = inv[name]["file"]
        f = "`graph.py` 内嵌" if f.startswith("graph.py") else f"`{f}`"
        out.append("| " + (f"{i2} | " if seq else "") + f"`{name}` | {f} | {role} | {st} |")
    return "\n".join(out)


names_all = [n for n, _, _ in MAIN + QC + ST]
assert len(MAIN) == N_NEW - 27 == 26 and len(QC) == 6 and len(ST) == 21 and len(set(names_all)) == N_NEW == 53
assert set(names_all) == set(inv), set(inv) ^ set(names_all)
assert set(names_all) == {nd["name"] for nd in new_j["nodes"]}
rep["INV_MAIN"], rep["INV_QC"], rep["INV_STORY"] = table(MAIN, True), table(QC, False), table(ST, False)

# ---- 实证原文 ----
rep["EVID1"] = body("1_英文草稿缺字幕_调序前.txt").strip()
rep["EVID2_BEFORE_CP3"] = body("2_整图超步顺序_调序前_cp3.txt").strip()


def trim(name):
    keep = []
    for ln in body(name).splitlines():
        mm = re.match(r"^step\s+(\d+):", ln)
        if not mm or int(mm.group(1)) >= MIN_STEP:
            keep.append(ln)
    return "\n".join(keep).strip()


rep["EVID2_AFTER_CP3"] = trim("2_整图超步顺序_调序后_cp3.txt")
rep["EVID3_2"] = body("3_在途线程_旧版挂起在关卡②_新版恢复.txt").strip()
rep["EVID3_1"] = body("3_在途线程_旧版挂起在关卡①_新版恢复.txt").strip()
rep["EVID4"] = body("4_subtitle_scout_report键被丢弃.txt").strip()

doc = T
for k, v in rep.items():
    doc = doc.replace("{{" + k + "}}", v)
left = re.findall(r"\{\{[A-Z0-9_]+\}\}", doc)
assert not left, left
DST.write_text(doc, encoding="utf-8")
print("文档已生成:", DST, "| 行数", doc.count("\n") + 1, "| 字符", len(doc))
