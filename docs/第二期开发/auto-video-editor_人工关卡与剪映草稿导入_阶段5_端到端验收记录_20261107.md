# auto-video-editor 人工关卡与剪映草稿导入 阶段5 端到端验收记录(2026-11-07)

> 计划:`docs/第二期开发/auto-video-editor_人工关卡与剪映草稿生成导入_设计与分阶段执行计划.md`(无日期主版)§8 阶段 5
> 执行会话:ZCode(阶段 4-6 接力会话);实机 GUI 步骤留操作员执行
> 关联交付:阶段 4(`9940806`)、阶段 6(`9130d38`)

---

## 1. 结论摘要

| 项目 | 结果 |
|---|---|
| 单元测试(tests/unit) | **1442 通过 / 3 跳过 / 0 失败**(含本阶段新增 60 例) |
| 集成测试(tests/integration) | **1 个既有失败**,其余全绿(3 跳过为 PG 相关环境性跳过) |
| 前端(node mjs 21 例 + console/monitor 前端测试) | 全绿 |
| 剪映实机端到端(GUI) | **待操作员执行**(清单见 §4;代码侧已就绪) |
| 素材准备 | 已完成:`outputs/e2e-assets/clip_a.mp4` + `clip_b.mp4`(脚本可复现) |
| 导入失败修复回归(2026-11-07 晚,§6) | unit 全量通过 / integration 仅剩 §3 既有失败 / 前端 mjs 36 例全绿 |

既有失败与本阶段改动**无关**:在阶段 4 提交前的 `736e7b7`(干净树)上同样失败,根因见 §3。

---

## 2. 本阶段新增能力的自动化验证

### 2.1 阶段 4(双语草稿与交付前 QA)

- `tests/unit/test_draft_integrity.py`(7 例):双文件一致性 / 根索引 / 素材路径
  (URI、空串、占位跳过)/ 悬空引用 / 镜像两侧为空通过 / 英侧多轨告警 /
  注册判定开关。
- `tests/unit/test_node_join_before_delivery.py`(17 例):既有封面/SRT/尾链
  契约保留;新增中文侧草稿缺失、双文件不一致、素材路径缺失 → QA 明确失败
  (`passed=False`),镜像破坏报 `BGM镜像`,发布开启 + 草稿库根下未注册根索引 →
  失败、已注册 → 通过,warning 只进 `join_qa_report` 不污染
  `join_qa_issues/error_log`。
- fork:`copytree` 排除 `.snapshots`(对齐 draft_publisher),既有 fork 用例回归通过。
- 创建台:`web/console.html` 交付状态四态卡(草稿生成/草稿发布/交付 QA/MP4 导出),
  事件归约 + SSE 重连重放重建;console/monitor 前端测试与 node mjs 全绿。

### 2.2 阶段 6(可选最终视频导出)

- `tests/unit/test_draft_exporter.py`(10 例):渲染计划(排序/变速/mapper 风格
  音轨段/无视频段报错/素材路径缺失报错)、argv 结构(overlay 保留时间轴空隙、
  atempo 链、amix、textfile 零转义注入、enable 逗号单转义)、真 ffmpeg 渲染
  (无 BGM / 有 BGM 循环混音,90x160 画布,时长/分辨率/编码/音轨/可播放性
  校验全过,SHA-256 落报告,报告可 JSON 序列化)、渲染失败显式 failed。
- `tests/unit/test_node_18_export_video.py`(6 例):开关关闭显式 skipped、缺草稿
  /不可读/DraftExportError/意外异常 → 显式 failed 不炸工作流、成功路径回填
  `final_video_path` 与报告。
- 图接线:`tests/integration/test_graph_compiles.py`、
  `test_unreachable_nodes_wiring.py` 等通过(join → export_final_video → END;
  节点注册表已登记,`export_final_video` 出现在节点拓扑)。

---

## 3. 既有集成失败(非本阶段引入,已根因定位)

`tests/integration/test_reorder_topology.py::test_tail_chain_applies_to_both_drafts`

- **现象**:断言 `英文草稿 transitions 条数 > 0` 失败;中英两份草稿 transitions
  均为 0 条(特效 / 贴纸注入正常)。
- **根因**:该测试的 `_seed` 只给了 1 个 shot;`node_09` 的
  `_boundary_count = len(shots) - 1 = 0`,即无剪辑边界可注入转场 —— 转场为空是
  种子数据与断言自相矛盾,不是尾链失效。
- **引入点**:`5fd5d9d`(阶段 3 图拓扑 auto-only 收敛)调整了 `_seed` 与图结构
  之后即如此;在阶段 4 提交前的干净树上可复现。
- **修复建议**(后续单独处理,不混入本阶段):`_seed` 的 `shot_plan.shots` 与
  草稿 video track 增至 ≥2 段,或断言改为 `>= 0` + 单独用例覆盖多边界注入。
- **与阶段 4 的关系**:阶段 4 的 `join_before_delivery` 深度 QA 在该场景下正确
  输出 `中文主草稿转场缺失 / 英文草稿转场缺失` 两条 error 级 issue —— 深度 QA
  的可见性价值在既有失败场景得到了实证(旧实现此问题静默)。

---

## 4. 剪映实机端到端验收清单(操作员执行;每步完成后勾选)

前置:启动服务(管理员 pwsh7:`start_production.ps1`),PG 可达,剪映 5.9.0 已安装。

- [ ] 1. 素材:确认 `outputs/e2e-assets/clip_a.mp4`、`clip_b.mp4` 存在
      (缺失则重跑 `.\.venv\Scripts\python.exe scripts\make_e2e_test_assets.py`)。
- [ ] 2. 单视频场景:创建台上传 `clip_a.mp4`,填写提示词,创建任务,记录 Thread ID:
      `____________________`。
- [ ] 3. 等待 `generate_draft` 完成:创建台"交付状态"卡显示 **草稿生成:成功**,
      **草稿发布:成功**(发布失败会显示"降级",此时排障 `draft_publish_error`)。
- [ ] 4. 剪映首页确认出现 `AutoVideo_<thread_id>` 草稿,双击打开无"草稿损坏"
      提示,时间轴有视频轨与片段。
- [ ] 5. 关卡①:在剪映中调整分镜顺序 → 保存 → 关闭剪映 → 创建台"确认恢复"
      (关卡① 自动选中,放行值 `true`)。
- [ ] 6. 多视频场景(可另开任务):同时上传 `clip_a.mp4` + `clip_b.mp4`,重复 3-5;
      用两个片段的明显画面差异核对调序结果。
- [ ] 7. 关卡③(条件触发):构造英文字幕布局异常(长英文文本素材),确认③出现、
      显示 issues 明细与预览/证据帧;人工修正后恢复,检查 `subtitle_en.srt`
      保留人工修改。
- [ ] 8. 关卡②:在剪映 VIP 音乐库选 BGM 拖入音轨 → 保存关闭 → 恢复;确认交付
      状态卡"交付 QA"最终显示成功,且无 `BGM镜像` 类 issue(英文分支音轨镜像完整)。
- [ ] 9. 确认 `node_13_adjust_volume`、`join_before_delivery` 完成后运行到
      `export_final_video`(默认关闭 → "MP4 导出:跳过")。
- [ ] 10. 双草稿首页复检:中文 `AutoVideo_<thread_id>` 与英文分支草稿都能从剪映
      首页打开、无损坏提示;字幕 / 音轨内容正确。
- [ ] 11. 监控页事件与状态正确;断开网络后 SSE 重连按游标补发,节点不重复执行
      (事件表无重复 `node.started`)。
- [ ] 12. 导出链实机(可选):设 `AUTO_VIDEO_EDITOR_FINAL_EXPORT_ENABLED=1` 重启
      服务后重跑一轮,确认"MP4 导出:成功"且 `outputs/<thread_id>/export/final.mp4`
      可播放;交付状态卡 fidelity 提示(转场硬切 / 贴纸特效未渲染)与实际画面一致。
- [ ] 13. 记录:Thread ID、剪映版本(5.9.0.11632)、每步结果与本清单勾选状态,
      回填本文件 §5。

## 5. 实机验收回填(操作员填写)

- 验收日期:__________ 操作员:__________
- 单视频场景 Thread ID:__________ 结果:__________
- 多视频场景 Thread ID:__________ 结果:__________
- 关卡①/③/② 触发与恢复:__________
- 双草稿剪映首页显示:__________
- 导出链(若启用):__________
- 遗留问题:__________

---

## 6. 导入失败修复记录(2026-11-07 晚;§4 清单第 4 步首跑即证伪后的一轮修复)

### 6.1 实机证伪结论

按 §4 步骤 1-4 首跑:首页能列出发布草稿,但双击必报"草稿有异常,导入失败";
剪映启动另弹"草稿列表异常,检测到部分草稿丢失"。证据链:

- 发布产物的 `draft_content.json` 是仓库内部 storyline 4 键格式
  (canvas_config/materials/storyline_meta/tracks),缺原生必填的 28 顶层键 /
  45 素材桶 / 34 键 segment —— 剪映首页列表只读 `root_meta_info.json` +
  `draft_meta_info.json`(所以"可列"),加载时间轴必失败(所以"导入失败");
- "部分草稿丢失"是另一回事:`draft_acion_watch.json` 显示剪映启动对账时清掉
  了索引外的游离草稿(一次会话外测试残留)与旧 id 条目;
- 首页磁贴实为阶段 1 冒烟夹具 `AutoVideo_SMOKE-阶段1勿编辑-99c742e55c7d`
  (真实 thread 的草稿因发布功能晚于其草稿生成 14h 合入,从未发布)。

### 6.2 修复内容(字段级依据:真实样本 + 参考实现)

- **真实样本**:操作员在剪映 5.9.0.11632 新建空白草稿("11月7日"),其
  draft_content.json(28 顶层键/45 桶/46 键视频素材/34 键 segment)、
  draft_meta_info.json(34 键)、root_meta_info.json 条目(22 键)逐字段采集;
  关键口径:tm_draft_create/modified 为**微秒**、draft_root_path 反斜杠、
  meta 侧 `draft_timeline_materials_size_` 带尾下划线而根索引不带。
- **参考实现**:isYangs/jianying-editor-skill(MIT,内嵌 pyJianYingDraft 裁剪
  fork,产出 5.9 可打开草稿)提供音频/文本素材字段模板;并证明最小 platform
  头、tm_*/封面缺失不阻碍打开。

代码改动(P1 原生转换核心):

- 新增 `storyline/native_draft.py`:storyline→原生转换器(uuid5 确定性 id、
  四伴随条目、BGM/配音占位与私有语义进 sidecar `autovideo_storyline_source.json`);
- `node_05`:三路构建统一过转换器,staging 出生即原生;
- `jianying_registry`:meta/索引条目升级为原生 34/22 键(µs 时间戳等);
- `draft_publisher`:旧格式 staging 发布时转换+幂等直通;ffmpeg 抽帧生成
  draft_cover.jpg(best-effort)。

代码改动(P2 读回/写入链兼容):

- node_12/13/join 音频匹配改 `segment.material_id` 优先(兼容旧 id 约定)——
  人工在剪映加的 BGM 不再被镜像/QA 静默丢弃;
- node_07:段已引用 speed material 时同步更新速度值(原生伴随结构下变速生效);
- 文本链原生升级:node_08/08b 把字幕铺上**原生 text 轨道**(此前只躺
  materials.texts 桶,剪映时间轴不可见,关卡③ 无从下手);material 带原生
  必填键(content=styles JSON)+ 私有键双形态;node_16 SRT 重建、
  draft_exporter、draft_integrity 双形态兼容(人工编辑保存后照样读得到)。

### 6.3 修复后回归

| 项目 | 结果 |
|---|---|
| 单元测试(tests/unit 全量) | **全部通过**(exit 0;新增 test_native_draft_converter + test_subtitle_texts_native) |
| 集成测试 | 仅剩 §3 既有失败 `test_tail_chain_applies_to_both_drafts`;`test_english_draft_gets_subtitles_and_text_fx` 断言按双形态契约更新后通过 |
| 前端 node mjs | 36/36 通过 |
| 剪映实机 | 待操作员按 §4 清单用**新建任务**重跑(旧线程草稿为旧格式,恢复不补发布) |

## 7. 切镜抽帧超时→空草稿修复记录(2026-11-07;§6.3 实机复验前的最后一轮)

### 7.1 实机现象与根因(五层)

新建验证任务 `console-da0cf811f347`(69s 4K HEVC Main10/HLG iPhone 源):剪映首页正常列出草稿、双击正常打开(**§6 修复在本任务上验证生效**),但草稿为 0 片段/00:00 空时间轴,关卡①的"调整镜头顺序"无从下手。根因链:

1. **触发**:`split_shots` 抽帧用全分辨率 PNG(单帧 ~9.2MB)+ `timeout=60`;该源软解 HEVC 10bit + 落盘吞吐 328 帧/60s,完成需 ~63.3s → 超预算 3.3s 被杀(帧目录 mtime 13:55:47→13:56:47 精确 60s);
2. **放大**:`except (FileNotFoundError, TimeoutExpired): return []` 把两类可恢复性完全不同的失败同等"静默放弃",已落盘的 328 张有效帧被丢弃;`len(frames)<2` 早退无 error 标记;
3. **贯通**:understand/filter/group(`no_input`/`empty_input`)、plan(`method: empty`)、qa_gate(空 plan 显式放行,plan §4.2 设计)、CanonicalTimeline(允许空 clips)、node_05(单空 storyline 照发草稿)、node_06(无条件 interrupt)—— 全链无一处把"空"当失败;
4. **哲学**:"环境级失败降级"纪律未覆盖数据缺陷 —— 缺"时间轴至少一个镜头"这条语义不变量;
5. **观测**:全链无异常 → 监控"完成 23 · 失败 0";线索只埋在产物 method 字段与 stderr 警告里(其中 `探测分辨率失败('1080x')` 还是另一个被降级纪律吞掉的 bug,见 7.2)。

### 7.2 修复内容

前 4 项已随 `b8548dd` 入库并推送:

- `storyline_capabilities/split_shots.py`:抽帧降分辨率 480 宽(`scale=480:-2`,帧差无需原分辨率)+ 超时 300s + **超时不弃帧**(已落盘帧继续参与切分)+ 真空结果带 `error=no_frames`;min_shot 合并后补 **max_shot 后置强制切**(连续运动素材帧帧超阈值,相邻段全部被合并成 1 个 69s 巨镜头,逐对强切分支永远轮不到 —— 违反 docstring "超过 max_shot 强制切"契约,现兜底切为 3×30s);
- `nodes/node_05_generate_draft.py`:canonical `clips=0` → raise ContractInvalid fail-closed —— 不发布空草稿、不挂死局关卡①(qa_gate 空 plan 放行是 plan §4.2 既定设计,不翻转,拦截点上移到 node_05);
- `nodes/storyline/node_split_shots.py`:0 镜头 → `SPLIT_SHOTS_EMPTY` 写 error_log(监控事件流直接可见切镜层原因);
- `video_edit_capabilities/render.py`:`probe_video_size` 兼容本机 ffprobe csv 输出尾部 `x` 分隔符(实机输出 `1920x1080x`,旧解析 `int("1080x")` 崩,归一化被降级跳过)。

本轮追加:

- `storyline_capabilities/load_media.py`:ffprobe `-show_entries` 补 `codec_type,sample_rate` —— 此前分类循环永不命中,宽高/fps/has_audio/sample_rate 全 0;`has_audio=False` 连带 `graph.py` ASR 分支路由永不触发(功能缺陷,非仅显示问题);
- 测试密封:`test_node_05` 两用例注入 `proc_checker=lambda: False`(原子双写/快照/verify 仍真实执行,剪映开着不再环境性失败;互斥行为仍由 test_safe_write_guard / test_atomic_writer* 覆盖);
- 测试:新增 `test_split_shots_extraction_robustness.py`(4 用例:超时保帧/scale+300s 断言/ffprobe 缺失 no_frames 标记/max_shot 强切无缝)+ `test_empty_storyline_fail_closed.py`(4 用例:node_05 空守卫 raise/1-clip 正例不误伤/node_split_shots 空诊断/probe 尾分隔符)+ load_media ffprobe 分类回归用例(随 b8548dd / 本轮分别入库)。

### 7.3 修复后回归

| 项目 | 结果 |
|---|---|
| 单元测试(tests/unit 全量) | **全部通过**(1512 用例,exit 0;含新增 9 用例) |
| 真机复核(split_shots) | 0001.mov(69s 4K HEVC 10bit):76s 抽帧 346/346,切出 3 镜头(30s/30s/9.1s,无缝),无 error |
| 真机复核(load_media) | 同源 metadata:1920x1080 / fps 30.0 / has_audio True / 44100Hz 全字段正确填充 |
| 剪映实机 | 待操作员**关闭剪映后新建任务**重跑(工作流为每任务独立子进程,新任务自动带上修复;挂起任务 console-da0cf811f347 的 state 已是空 storyline 不回补,其空草稿卡片在剪映内手动删除) |
