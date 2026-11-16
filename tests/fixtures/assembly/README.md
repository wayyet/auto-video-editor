# assembly 测试 fixtures(P2-5)

本目录存放 assembly 重构的**契约样例 JSON**与**可再生真编码素材**:

## JSON 契约样例(入库)
- `manifest_multi_source.json` —— P1-2 素材 manifest 形态(2 素材,其一
  probe 失败按 degraded 留痕,不静默丢失);
- `qc_report_structural.json` —— P1-5 候选 QC 的 typed issue 形态
  (error=broken_reference 带 clip_id 定位;missing_asset 降级为 warning);
- `repair_history_sample.json` —— P1-6 修复轮次形态(前后 sha/指纹/
  applied_operations/strategy_notes)。

用途:脚本/运维工具的脱敏对照样例、新测试的契约参考。单元测试主要在
tmp 内动态构造,不直接依赖这些文件。

## 真编码素材(不入库,按需再生)
- `media/` 由 `scripts/make_assembly_fixtures.py` 合成(需 PATH 有 ffmpeg):
  black_head.mp4(前段黑帧)/ silent_audio.mp4 / no_audio.mp4 /
  rotated_90.mp4 / hdr_tagged.mp4(色彩标记近似)/ vfr_mixed.mp4 /
  same_name_dir_a|b/clip.mp4(同名双目录)/ corrupted.mp4(截断损坏)。
- 真机验收/手动回归用;真编码自动化用例走 `ASSEMBLY_TEST_REAL_FFMPEG=1` 门禁。

## 证据包
`outputs/<job>/assembly/evidence/<UTCts>/` 由
`scripts/archive_assembly_evidence.py` 归档(脱敏绝对路径 + commit SHA +
schema/开关/模型信息 + 四级 hash 链 + 产物副本 + 可选 draft 对账)。
