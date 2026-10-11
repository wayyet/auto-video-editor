#!/usr/bin/env python3
"""从 AST 提取的真实边生成 Mermaid 图,并做"画的边 == 图里节点间真实存在的边"双向校验。

用法:
    python gen_mermaid.py <graph_extract_old.json> <graph_extract_new.json> <输出目录>

输出:  <输出目录>/diagrams.json  (键: 图1..图4,值: Mermaid 源码)
       <输出目录>/图1_现状主链.mmd ... 图4_自动模式子图.mmd
校验失败(漏画或多画)会直接抛 AssertionError,不会静默产出错图。
"""
import json
import sys
from pathlib import Path

OLD_JSON, NEW_JSON, OUT_DIR = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
OUT_DIR.mkdir(parents=True, exist_ok=True)


def load(path):
    d = json.loads(path.read_text(encoding="utf-8"))
    edges = set()
    for e in d["edges"]:
        for s in (e["src"] if isinstance(e["src"], list) else [e["src"]]):
            edges.add((s, e["dst"]))
    for c in d["cond"]:
        for _, v in (c["map"] or {}).items():
            edges.add((c["src"], v))
    norm = lambda x: {"$START": "START", "$END": "END_"}.get(x, x)
    return {(norm(a), norm(b)) for a, b in edges}


OLD, NEW = load(OLD_JSON), load(NEW_JSON)

CLASSDEF = """  classDef same fill:#e8f1fb,stroke:#3b6ea5,color:#111
  classDef gate fill:#ffe0b2,stroke:#e65100,color:#111
  classDef newn fill:#c8f7c5,stroke:#2e7d32,stroke-width:2px,color:#111
  classDef moved fill:#fff3b0,stroke:#b8860b,stroke-width:2px,color:#111
  classDef dead fill:#eeeeee,stroke:#888,stroke-dasharray:4 3,color:#555
"""


def build(title, nodes, edges, classes, subgraphs, edge_set, extra_ok=(), explicit=None):
    """nodes: {id: 标签}; edges: [(src,dst,标签,kind)] kind: '-' 实线 '.' 虚线(条件) '=' 粗线(汇合屏障)"""
    ids = set(nodes) | {"START", "END_"}
    explicit = set(explicit) if explicit is not None else ids
    drawn = {(s, d) for s, d, _, _ in edges}
    expected = {(a, b) for a, b in edge_set if a in explicit and b in explicit}
    missing = expected - drawn
    extra = drawn - expected - set(extra_ok)
    assert not missing, f"[{title}] 漏画的真实边: {sorted(missing)}"
    assert not extra, f"[{title}] 画了但真实图里没有的边: {sorted(extra)}"
    shapes = classes.get("_shape", {})
    members_all = {i for _, _, m, _ in subgraphs for i in m}

    def node_line(i, ind="  "):
        if i == "START":
            return f"{ind}START([START])\n"
        if i == "END_":
            return f"{ind}END_([END])\n"
        lab, shp = nodes[i], shapes.get(i, "rect")
        if shp == "hex":
            return f'{ind}{i}{{{{"{lab}"}}}}\n'
        if shp == "sub":
            return f'{ind}{i}[["{lab}"]]\n'
        return f'{ind}{i}["{lab}"]\n'

    out = ["flowchart TD\n"]
    for i in ("START", "END_"):
        if i in ids:
            out.append(node_line(i))
    for i in nodes:
        if i not in members_all:
            out.append(node_line(i))
    for sid, lab, members, direction in subgraphs:
        out.append(f'  subgraph {sid}["{lab}"]\n    direction {direction}\n')
        for m in members:
            out.append(node_line(m, "    "))
        out.append("  end\n")
    arrows = {"-": "-->", "." : "-.->", "=": "==>"}
    for s, d, lab, k in edges:
        out.append(f'  {s} {arrows[k]}|"{lab}"| {d}\n' if lab else f"  {s} {arrows[k]} {d}\n")
    out.append(CLASSDEF)
    for c in ("same", "gate", "newn", "moved", "dead"):
        if classes.get(c):
            out.append(f"  class {','.join(classes[c])} {c}\n")
    return "".join(out)


L = {
    "clean_cache": "node_01 清理缓存<br/>(START 无入边)",
    "launch_openstoryline": "node_02 启动 OpenStoryline",
    "open_preview": "node_03 打开预览",
    "checkpoint0_storyline_plan": "关卡⓪ 确认分镜计划",
    "import_video": "node_04a 导入视频<br/>等网页按钮<br/>(默认最长 600s)",
    "get_storyboard_plan": "node_04b 读取分镜<br/>规划产物",
    "generate_draft": "node_05 生成剪映初始草稿",
    "node_06_human_reorder": "关卡① node_06<br/>人工调整分镜顺序",
    "node_07_speed_fit": "node_07 变速适配≤35s",
    "escalate_guardrail_failure": "护栏失败升级",
    "bridge_snapshot2": "快照②",
    "node_08_add_subtitles": "node_08 中文字幕写入",
    "node_09_inject_fx": "node_09 转场/视频特效",
    "node_10_inject_text_fx": "node_10 花字/字幕动画(中)",
    "node_11_inject_sticker": "node_11 贴纸",
    "node_12_human_add_bgm": "关卡② node_12<br/>人工添加 BGM",
    "node_13_adjust_volume": "node_13 音量/淡入淡出",
    "node_14_make_covers": "node_14 三比例封面",
    "node_15_localize_covers_en": "node_15 封面英文本地化",
    "fork_draft_for_english_branch": "fork 复制英文分支草稿",
    "node_16a_translate_and_check": "node_16a 英文字幕翻译<br/>+ 布局检查",
    "node_checkpoint3_layout_review": "关卡③ 人工校对<br/>英文字幕排版",
    "node_17_inject_english_tts": "node_17 英文配音<br/>(产出音频文件)",
    "join_before_delivery": "join_before_delivery",
    "node_08b_add_subtitles_en": "node_08b 英文字幕写入<br/>英文草稿(新增)",
    "node_10b_inject_text_fx_en": "node_10b 花字/字幕动画<br/>(英,新增)",
    "AUTO": "storyline 自动模式子图<br/>21 节点(见图4)",
    "QC": "素材 QC 子图<br/>6 节点(见图3)",
}
SHAPE = {"checkpoint0_storyline_plan": "hex", "node_06_human_reorder": "hex", "node_12_human_add_bgm": "hex",
         "node_checkpoint3_layout_review": "hex", "AUTO": "sub", "QC": "sub"}
GATES = ["checkpoint0_storyline_plan", "node_06_human_reorder", "node_12_human_add_bgm", "node_checkpoint3_layout_review"]
HEAD = [
    ("START", "launch_openstoryline", "", "-"), ("launch_openstoryline", "open_preview", "", "-"),
    ("open_preview", "checkpoint0_storyline_plan", "human(默认)", "."), ("open_preview", "AUTO", "auto", "."),
    ("checkpoint0_storyline_plan", "import_video", "", "-"), ("import_video", "get_storyboard_plan", "", "-"),
    ("get_storyboard_plan", "generate_draft", "有计划", "."), ("get_storyboard_plan", "END_", "无计划", "."),
    ("AUTO", "generate_draft", "", "-"), ("generate_draft", "QC", "QC 开关开(默认)", "-"),
    ("QC", "node_06_human_reorder", "", "-"),
    ("generate_draft", "node_06_human_reorder", "QC 开关关闭时直达", "."),
    ("node_06_human_reorder", "node_07_speed_fit", "", "-"),
    ("node_07_speed_fit", "node_07_speed_fit", "未达标重试", "."),
    ("node_07_speed_fit", "escalate_guardrail_failure", "重试耗尽", "."), ("escalate_guardrail_failure", "END_", "", "-"),
    ("node_07_speed_fit", "bridge_snapshot2", "达标", "."),
]
COLLAPSED_OK = {("open_preview", "AUTO"), ("AUTO", "generate_draft"), ("generate_draft", "QC"), ("QC", "node_06_human_reorder")}
OUT = {}

# ---------------- 图1: 现状主链 ----------------
n1 = {k: v for k, v in L.items() if k not in ("node_08b_add_subtitles_en", "node_10b_inject_text_fx_en")}
zh1 = ["node_08_add_subtitles", "node_09_inject_fx", "node_10_inject_text_fx", "node_11_inject_sticker",
       "node_12_human_add_bgm", "node_13_adjust_volume", "node_14_make_covers", "node_15_localize_covers_en"]
en1 = ["fork_draft_for_english_branch", "node_16a_translate_and_check", "node_checkpoint3_layout_review", "node_17_inject_english_tts"]
e1 = HEAD + [
    ("bridge_snapshot2", "node_08_add_subtitles", "", "-"), ("bridge_snapshot2", "fork_draft_for_english_branch", "", "-"),
    ("node_08_add_subtitles", "node_09_inject_fx", "", "-"), ("node_09_inject_fx", "node_10_inject_text_fx", "", "-"),
    ("node_10_inject_text_fx", "node_11_inject_sticker", "", "-"), ("node_11_inject_sticker", "node_12_human_add_bgm", "", "-"),
    ("node_12_human_add_bgm", "node_13_adjust_volume", "", "-"), ("node_13_adjust_volume", "node_14_make_covers", "", "-"),
    ("node_14_make_covers", "node_15_localize_covers_en", "", "-"),
    ("fork_draft_for_english_branch", "node_16a_translate_and_check", "", "-"),
    ("node_16a_translate_and_check", "node_checkpoint3_layout_review", "有排版异常", "."),
    ("node_16a_translate_and_check", "node_17_inject_english_tts", "无异常", "."),
    ("node_checkpoint3_layout_review", "node_17_inject_english_tts", "", "-"),
    ("node_15_localize_covers_en", "join_before_delivery", "汇合", "="), ("node_17_inject_english_tts", "join_before_delivery", "汇合", "="),
    ("join_before_delivery", "END_", "", "-"),
]
exp1 = (set(n1) - {"AUTO", "QC"}) | {"START", "END_"}
OUT["图1"] = build("图1", n1, e1, {
    "_shape": SHAPE, "gate": GATES, "dead": ["clean_cache", "AUTO", "QC"],
    "moved": ["node_09_inject_fx", "node_10_inject_text_fx", "node_11_inject_sticker", "node_13_adjust_volume"],
    "same": [k for k in n1 if k not in GATES + ["clean_cache", "AUTO", "QC", "node_09_inject_fx", "node_10_inject_text_fx",
                                                 "node_11_inject_sticker", "node_13_adjust_volume"]]},
    [("ZH", "中文线(串行 8 节点)", zh1, "TB"), ("EN", "英文线(4 节点,与中文线并行)", en1, "TB")],
    OLD, extra_ok=COLLAPSED_OK, explicit=exp1)

# ---------------- 图2: 目标主链 ----------------
zh2 = ["node_08_add_subtitles", "node_10_inject_text_fx", "node_14_make_covers", "node_15_localize_covers_en"]
en2 = ["fork_draft_for_english_branch", "node_16a_translate_and_check", "node_08b_add_subtitles_en",
       "node_checkpoint3_layout_review", "node_10b_inject_text_fx_en", "node_17_inject_english_tts"]
tail = ["node_09_inject_fx", "node_11_inject_sticker", "node_12_human_add_bgm", "node_13_adjust_volume", "join_before_delivery"]
n2 = dict(L)
n2["join_before_delivery"] = "join_before_delivery<br/>交付前 QA 闸门"
e2 = HEAD + [
    ("bridge_snapshot2", "node_08_add_subtitles", "", "-"),
    ("node_08_add_subtitles", "node_10_inject_text_fx", "", "-"), ("node_10_inject_text_fx", "node_14_make_covers", "", "-"),
    ("node_14_make_covers", "node_15_localize_covers_en", "", "-"),
    ("node_08_add_subtitles", "fork_draft_for_english_branch", "显式依赖 asr_segments_zh", "-"),
    ("fork_draft_for_english_branch", "node_16a_translate_and_check", "", "-"),
    ("node_16a_translate_and_check", "node_08b_add_subtitles_en", "", "-"),
    ("node_08b_add_subtitles_en", "node_checkpoint3_layout_review", "有排版异常", "."),
    ("node_08b_add_subtitles_en", "node_10b_inject_text_fx_en", "无异常", "."),
    ("node_checkpoint3_layout_review", "node_10b_inject_text_fx_en", "", "-"),
    ("node_10b_inject_text_fx_en", "node_17_inject_english_tts", "", "-"),
    ("node_15_localize_covers_en", "node_09_inject_fx", "汇合屏障:两线都完成才触发", "="),
    ("node_17_inject_english_tts", "node_09_inject_fx", "汇合屏障", "="),
    ("node_09_inject_fx", "node_11_inject_sticker", "", "-"), ("node_11_inject_sticker", "node_12_human_add_bgm", "", "-"),
    ("node_12_human_add_bgm", "node_13_adjust_volume", "", "-"), ("node_13_adjust_volume", "join_before_delivery", "", "-"),
    ("join_before_delivery", "END_", "", "-"),
]
exp2 = (set(n2) - {"AUTO", "QC"}) | {"START", "END_"}
OUT["图2"] = build("图2", n2, e2, {
    "_shape": SHAPE, "gate": GATES, "dead": ["clean_cache", "AUTO", "QC"],
    "newn": ["node_08b_add_subtitles_en", "node_10b_inject_text_fx_en"],
    "moved": ["node_09_inject_fx", "node_11_inject_sticker", "node_12_human_add_bgm", "node_13_adjust_volume",
              "node_10_inject_text_fx", "node_14_make_covers", "node_15_localize_covers_en", "join_before_delivery",
              "fork_draft_for_english_branch"],
    "same": [k for k in n2 if k not in GATES + ["clean_cache", "AUTO", "QC", "node_08b_add_subtitles_en", "node_10b_inject_text_fx_en",
                                                 "node_09_inject_fx", "node_11_inject_sticker", "node_13_adjust_volume",
                                                 "node_10_inject_text_fx", "node_14_make_covers", "node_15_localize_covers_en",
                                                 "join_before_delivery", "fork_draft_for_english_branch"]]},
    [("ZH", "中文线(4 节点)", zh2, "TB"), ("EN", "英文线(6 节点,与中文线并行)", en2, "TB"),
     ("TAIL", "尾链(两线汇合后串行,作用于中文主草稿)", tail, "TB")],
    NEW, extra_ok=COLLAPSED_OK, explicit=exp2)

# ---------------- 图3: 素材 QC 子图 ----------------
n3 = {
    "generate_draft": "node_05 生成剪映初始草稿", "node_06_human_reorder": "关卡① node_06",
    "assembly_discover_and_probe": "1/6 发现与探测素材", "assembly_asr_and_visual_observe": "2/6 ASR 与视觉观察",
    "assembly_build_timeline": "3/6 构建时间线", "assembly_validate_render_qc": "4/6 校验 / 渲染 / QC",
    "assembly_repair_loop": "5/6 修复循环", "assembly_write_report": "6/6 写 QC 报告",
}
e3 = [
    ("generate_draft", "assembly_discover_and_probe", "", "-"),
    ("assembly_discover_and_probe", "assembly_asr_and_visual_observe", "", "-"),
    ("assembly_asr_and_visual_observe", "assembly_build_timeline", "", "-"),
    ("assembly_build_timeline", "assembly_validate_render_qc", "", "-"),
    ("assembly_validate_render_qc", "assembly_repair_loop", "未通过且未到重试上限", "."),
    ("assembly_validate_render_qc", "assembly_write_report", "通过,或重试已达上限<br/>(软降级)", "."),
    ("assembly_repair_loop", "assembly_validate_render_qc", "", "-"),
    ("assembly_write_report", "node_06_human_reorder", "", "-"),
    ("generate_draft", "node_06_human_reorder", "QC 开关关闭时直达", "."),
]
OUT["图3"] = build("图3", n3, e3, {"gate": ["node_06_human_reorder"],
    "same": [k for k in n3 if k.startswith("assembly_") or k == "generate_draft"]},
    [("QCG", "素材 QC 子图(ASSEMBLY_QC_GATE_ENABLED,默认开)", [k for k in n3 if k.startswith("assembly_")], "TB")],
    NEW, explicit={k for k in n3} )
OUT["图3"] = OUT["图3"].replace("flowchart TD\n  START([START])\n  END_([END])\n", "flowchart TD\n")

# ---------------- 图4: 自动模式 storyline 子图 ----------------
S = {
    "storyline_load_media": "加载素材", "storyline_search_media": "搜索素材", "storyline_split_shots": "镜头切分",
    "storyline_understand_clips": "理解片段", "storyline_filter_clips": "筛选片段", "storyline_group_clips": "分组",
    "storyline_generate_script": "生成文案", "storyline_script_template_recommendation": "文案模板推荐<br/>(叶子,无出边)",
    "storyline_generate_voiceover": "生成配音", "storyline_select_bgm": "选 BGM",
    "storyline_recommend_transition": "推荐转场", "storyline_recommend_text": "推荐花字",
    "storyline_plan_timeline_pro": "排时间线(pro)", "storyline_qa_gate": "QA 闸门",
    "storyline_render_video": "渲染视频", "storyline_join": "汇合 → 反序列化到 storyline_plan",
    "storyline_local_asr": "本地 ASR<br/>(路由永不选中)", "storyline_speech_rough_cut": "口播粗剪<br/>(路由永不选中)",
    "storyline_search_web_topic": "搜索话题<br/>(无任何边)", "storyline_generate_ai_transition": "AI 转场生成<br/>(无任何边)",
    "storyline_plan_timeline_ai_transition": "排时间线(AI 转场)<br/>(无任何边)",
}
n4 = dict(S)
n4["open_preview"] = "node_03 打开预览"
n4["generate_draft"] = "node_05 生成剪映初始草稿"
main4 = [k for k in S if k not in ("storyline_local_asr", "storyline_speech_rough_cut", "storyline_search_web_topic",
                                  "storyline_generate_ai_transition", "storyline_plan_timeline_ai_transition")]
dead4 = ["storyline_local_asr", "storyline_speech_rough_cut", "storyline_search_web_topic",
         "storyline_generate_ai_transition", "storyline_plan_timeline_ai_transition"]
e4 = [
    ("open_preview", "storyline_load_media", "auto", "."),
    ("storyline_load_media", "storyline_search_media", "已配 Pexels Key", "."), ("storyline_load_media", "storyline_split_shots", "默认", "."),
    ("storyline_search_media", "storyline_split_shots", "", "-"),
    ("storyline_split_shots", "storyline_understand_clips", "默认", "."), ("storyline_split_shots", "storyline_local_asr", "(永不选中)", "."),
    ("storyline_local_asr", "storyline_speech_rough_cut", "", "-"),
    ("storyline_understand_clips", "storyline_filter_clips", "", "-"), ("storyline_filter_clips", "storyline_group_clips", "", "-"),
    ("storyline_group_clips", "storyline_generate_script", "", "-"), ("storyline_group_clips", "storyline_recommend_transition", "", "-"),
    ("storyline_generate_script", "storyline_script_template_recommendation", "", "-"),
    ("storyline_generate_script", "storyline_generate_voiceover", "", "-"), ("storyline_generate_script", "storyline_select_bgm", "", "-"),
    ("storyline_generate_script", "storyline_recommend_text", "", "-"),
    ("storyline_generate_voiceover", "storyline_select_bgm", "", "-"), ("storyline_select_bgm", "storyline_recommend_transition", "", "-"),
    ("storyline_split_shots", "storyline_plan_timeline_pro", "汇合", "="), ("storyline_group_clips", "storyline_plan_timeline_pro", "汇合", "="),
    ("storyline_generate_script", "storyline_plan_timeline_pro", "汇合", "="), ("storyline_generate_voiceover", "storyline_plan_timeline_pro", "汇合", "="),
    ("storyline_select_bgm", "storyline_plan_timeline_pro", "汇合", "="),
    ("storyline_plan_timeline_pro", "storyline_qa_gate", "汇合", "="), ("storyline_recommend_transition", "storyline_qa_gate", "汇合", "="),
    ("storyline_recommend_text", "storyline_qa_gate", "汇合", "="),
    ("storyline_qa_gate", "storyline_group_clips", "QA 触发回退重跑", "."), ("storyline_qa_gate", "storyline_render_video", "通过 / 失败 / 空 plan", "."),
    ("storyline_render_video", "storyline_join", "", "-"),
    ("storyline_join", "generate_draft", "有计划", "."), ("storyline_join", "END_", "无计划", "."),
]
exp4 = set(S) | {"open_preview", "generate_draft", "END_"}
OUT["图4"] = build("图4", n4, e4, {"dead": dead4, "same": main4},
    [("DEAD", "未接线 / 路由永不选中(5 节点)", dead4, "TB")],
    NEW, explicit=exp4)
OUT["图4"] = OUT["图4"].replace("  START([START])\n", "")

names = {"图1": "图1_现状主链", "图2": "图2_目标主链", "图3": "图3_素材QC子图", "图4": "图4_自动模式子图"}
for k, v in OUT.items():
    (OUT_DIR / f"{names[k]}.mmd").write_text(v, encoding="utf-8")
(OUT_DIR / "diagrams.json").write_text(json.dumps(OUT, ensure_ascii=False, indent=1), encoding="utf-8")
print("四张图生成并通过双向校验;字符数:", {k: len(v) for k, v in OUT.items()})
