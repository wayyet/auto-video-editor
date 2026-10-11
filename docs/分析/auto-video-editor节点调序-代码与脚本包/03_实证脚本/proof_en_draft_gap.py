"""实证:英文分支草稿(fork 自 snapshot②)里到底有没有英文字幕文本素材?
只用仓库真实函数 + 临时目录,不碰任何外部服务。

用法: python proof_en_draft_gap.py <仓库根目录>
预期(未改动的 main): 16a 之后英文草稿 materials.texts = 0;关卡③ resume 路径把 SRT 重写成 0 字节。
"""
import json, sys, tempfile, os
from pathlib import Path
REPO = sys.argv[1]
os.chdir(REPO)
sys.path.insert(0, REPO)
os.environ.setdefault('AUTO_VIDEO_EDITOR_DRAFT_DIR', tempfile.mkdtemp())

from jy_common.translate_client import MockTranslateClient, set_default_client
from nodes.node_fork_english_branch import fork_draft_for_english_branch
from nodes.node_16a_translate_and_check import node_16a_translate_and_check
from nodes.node_16_translate_subtitles import _read_segments_from_draft, write_srt_from_segments

root = Path(tempfile.mkdtemp())
# 1) 模拟 node_05 产出的草稿:materials.texts = [](node_05 源码第106行即如此)
main_dir = root / 'drafts' / 'job1'
(main_dir).mkdir(parents=True)
(main_dir / 'draft_content.json').write_text(json.dumps(
    {"canvas_config": {}, "materials": {"videos": [], "audios": [], "texts": []}, "tracks": []}), encoding='utf-8')
# 2) 模拟 node_07 的快照②(draft_path.parent.parent/snapshots/snapshot2)
snap = root / 'snapshots' / 'snapshot2'
snap.mkdir(parents=True)
(snap / 'draft_content.json').write_text((main_dir / 'draft_content.json').read_text(encoding='utf-8'), encoding='utf-8')

state = {"session_id": "proofsess", "snapshot2_path": str(snap / 'draft_content.json'),
         "_draft_dir_override": str(root / 'drafts' / 'default')}
# 3) 真实 fork 节点
out = fork_draft_for_english_branch(state)
en_dir = Path(out['draft_dir_en_branch'])
print('[fork] en_branch =', en_dir.name[:30], '...')
d0 = json.loads((en_dir / 'draft_content.json').read_text(encoding='utf-8'))
print('[fork] 英文草稿 materials.texts 条数 =', len(d0['materials']['texts']))

# 4) 真实 16a(mock 翻译);asr_segments_zh 由 node_08 产出,这里手工给 2 段
set_default_client(MockTranslateClient())
asr = [{"index": 0, "start_ms": 0, "end_ms": 2000, "text_zh": "你好"},
       {"index": 1, "start_ms": 2000, "end_ms": 4000, "text_zh": "世界"}]
st = {"asr_segments_zh": asr, "draft_dir_en_branch": str(en_dir), "status_log": [], "error_log": []}
r = node_16a_translate_and_check(st)
print('[16a ] 返回键 =', sorted(r.keys()))
print('[16a ] subtitle_segments_en 条数 =', len(r.get('subtitle_segments_en') or []))
d1 = json.loads((en_dir / 'draft_content.json').read_text(encoding='utf-8'))
print('[16a ] 之后英文草稿 materials.texts 条数 =', len(d1['materials']['texts']), '  <-- 关键')
print('[16a ] 之后英文草稿 tracks 条数 =', len(d1.get('tracks', [])))
print('[16a ] subtitle_en.srt 存在 =', (en_dir / 'subtitle_en.srt').exists(),
      '| 行数 =', len((en_dir / 'subtitle_en.srt').read_text(encoding='utf-8').splitlines()) if (en_dir / 'subtitle_en.srt').exists() else None)

# 5) 关卡③ resume 后的路径:cp3 会 _read_segments_from_draft → write_srt_from_segments
segs = _read_segments_from_draft(en_dir)
print('[cp3 ] resume 后从草稿还原的字幕段数 =', len(segs))
write_srt_from_segments(en_dir, segs)
print('[cp3 ] 重写后 subtitle_en.srt 字节数 =', len((en_dir / 'subtitle_en.srt').read_bytes()))
