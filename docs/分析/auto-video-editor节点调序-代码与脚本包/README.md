# auto-video-editor 节点调序 · 代码与脚本包

- 基线:`wayyet/auto-video-editor` 的 `main` @ `4c5b04289c45883718ddad7a6630d57be36db3c6`(2026-10-01 22:38 +0800)
- 基线变化:首次分析基于 `b00201b`(2026-09-30);`main` 随后前进了 1 个提交(拆分 node_04)。补丁、测试、实证、图都已按 `4c5b042` 重做并重新验证。
- 内容:把「英文线补 08b / 10b」和「09 / 11 / 12 / 13 推迟到 node_17 之后」落成可直接应用的代码,并附全部实证、分析、画图脚本。
- 配套文档:《auto-video-editor节点调序与全量工作流设计执行计划.md》(设计、全节点清单、4 张工作流图、执行计划)

## 已验证(沙箱:Linux · Python 3.13.16 · langgraph 1.2.9 · pytest 9.1.1 · ASR / 翻译 / TTS 走仓库默认 Mock)

| 项 | 结果 |
|---|---|
| 补丁应用到干净的 `4c5b042` 副本 | `git apply --check` 通过;工作区为 CRLF(`core.autocrlf=true`)时同样通过 |
| 新增 4 条守护测试 | 调序后 4 passed;同一组测试放到调序前仓库 4 failed |
| 全量回归 | 单元 789 passed / 3 failed / 25 skipped,与调序前完全一致;集成(排除 slow / admin)63 passed / 1 failed = 基线 59 passed / 1 failed + 新增 4。基线自带的 4 条失败调序前后是同一批,都是沙箱(Linux)环境所致:2 条断言写死 Windows 反斜杠路径、1 条缺 jieba、1 条因沙箱没有 `openstoryline\.venv\Scripts\python.exe`(原因与证据见设计文档 §7.1) |
| Mermaid 图 | 4 张图通过 mermaid 11.17.2 官方解析器;"画的边 = 真实边"双向校验通过;负对照确认校验器能报错 |
| 并行安全(静态) | 主链 26 个节点:17 个返回整份 state,9 个只返回变更字段。调序后唯一的"整份 + 变更字段"同超步组合是 node_10(中文)‖ fork,不冲突(设计文档 §3.4) |

**未验证**:Windows + 剪映 5.9.0 真机;真实 ASR / 翻译 / TTS 服务。

## 目录

| 目录 / 文件 | 作用 |
|---|---|
| `01_代码改动/` | 7 个文件的完整新版本(4 个修改 + 3 个新增),保持仓库相对路径,可直接覆盖 |
| `02_补丁/reorder-en-branch-and-tail.patch` | 同一份改动的标准 git 补丁(推荐用它应用) |
| `03_实证脚本/` | 4 个实证脚本 + 一键复现脚本 `run_all_proofs.py` + `运行输出/`(本次运行的原始输出) |
| `04_分析脚本/` | `ast_graph.py` 提取节点与边;`reach.py` 可达性分析;`ret_kinds.py` 判断每个节点返回整份 state 还是只返回变更字段;对应的 json 与输出(调序前 / 调序后) |
| `05_图表/` | `gen_mermaid.py` 生成并校验图;`validate_mermaid.mjs` 语法校验;`package.json`;4 张 `.mmd` |
| `06_文档生成/` | 设计文档的模板、清单导出脚本 `dump_inventory.py`、生成脚本 `build_doc.py`(数字与图都由真实输出填充,带断言) |
| `07_测试记录/` | 调序前 / 后的 pytest 完整输出、守护测试输出、全新克隆上按本 README 命令的合并运行输出、环境版本 |
| 根目录的 `.md` | 《auto-video-editor节点调序与全量工作流设计执行计划》:设计、全节点清单、4 张工作流图、执行计划 |

改动文件:

| 文件 | 类型 | 说明 |
|---|---|---|
| `graph.py` | 修改 | 注册 08b / 10b,重排边,汇合屏障前移到 node_09,更新拓扑文档字符串 |
| `nodes/node_08_add_subtitles.py` | 修改 | 循环体换成共用构造函数,输出逐字段不变 |
| `nodes/node_10_inject_text_fx.py` | 修改 | 拆出核心 `apply_text_fx`,保留中文 `inject_text_fx`,新增英文 `inject_text_fx_en` |
| `nodes/node_join_before_delivery.py` | 修改 | 仅更新位置说明(现为尾链末端 QA 闸门) |
| `nodes/_subtitle_texts.py` | 新增 | 一条字幕文本素材的共用构造函数 |
| `nodes/node_08b_add_subtitles_en.py` | 新增 | 英文字幕写入英文分支草稿 |
| `tests/integration/test_reorder_topology.py` | 新增 | 4 条守护测试(顺序、英文草稿内容、关卡③ 路径、②③ 不同时挂起) |

## 一、应用改动

先打开 PowerShell,**cd 到解压后的本目录**,再执行:

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$pkg  = (Get-Location).Path
$repo = 'E:\Documents\kuaishou\auto-video-editor'
Set-Location -LiteralPath $repo
git status --short
git switch main
git pull --ff-only
git switch -c feat/reorder-en-branch-and-tail
$patch = Join-Path $pkg '02_补丁\reorder-en-branch-and-tail.patch'
git apply --check $patch
git apply $patch
git status --short
```

- `git status --short` 第一次必须为空(工作区干净)。
- 应用后应看到 4 个 `M` 和 3 个 `??`。
- 如果 `main` 已经比 `4c5b042` 新:先 `git apply --check $patch`;不通过就把 `git apply $patch` 换成 `git apply --3way $patch`。

备选(仅当 `main` 仍是 `4c5b042`):直接覆盖。

```powershell
robocopy (Join-Path $pkg '01_代码改动') $repo /E /NFL /NDL /NJH /NJS /NP
```

`robocopy` 退出码 0 ~ 7 都表示成功。

## 二、验证

```powershell
Set-Location -LiteralPath $repo
.\.venv\Scripts\python.exe -m pytest tests\integration\test_reorder_topology.py
.\.venv\Scripts\python.exe -m pytest tests\unit tests\integration -m "not slow and not admin"
```

- 预期:新增 4 条全部通过;其余用例与改动前一致。
- 仓库的 `pyproject.toml` 已设 `addopts = "-q --strict-markers"`,命令里不要再写 `-q`。再写会变成 `-qq`,最后看不到 `N passed` 汇总行。
- 沙箱里按这两条命令(Linux 路径写法)在"全新克隆 + 补丁"上实跑:守护测试 `4 passed`;合并运行 `4 failed, 852 passed, 31 skipped, 5 deselected, 3 xfailed, 4 xpassed`。852 = 单元 789 + 集成 63;4 条失败就是基线自带的那 4 条(原始输出:`07_测试记录/after_all_fresh_clone.txt`)。你本机的数字会不同,以"比调序前多 4 条通过、失败名单不变"为准。

## 三、复现实证(可选)

```powershell
Set-Location -LiteralPath $repo
git worktree add --detach '..\auto-video-editor_before' main
$before = (Resolve-Path '..\auto-video-editor_before').Path
& "$repo\.venv\Scripts\python.exe" (Join-Path $pkg '03_实证脚本\run_all_proofs.py') $before $repo (Join-Path $pkg '03_实证脚本\运行输出_复现')
git worktree remove --force '..\auto-video-editor_before'
```

- 必须用仓库自己的 `.venv` 的 python(要 import langgraph 等)。
- 要在改动合并进 `main` 之前做。合并后 `main` 已是调序后的版本,上面 `git worktree add ... main` 取到的就不是"调序前"了。
- `运行输出\` 里出现的 `ave_before` / `ave_verify`,是沙箱里"调序前 / 调序后"两份副本的目录名。
- `main` 从 2026-10 起,`import_video` 节点默认等网页按钮最多 600 秒。实证脚本自己把等待压成 0(与 `tests/conftest.py` 同做法),所以不会卡住。
- 脚本只对节点 2 / 3 的进程拉起打桩;不访问外部服务。
- 会在两份仓库的 `outputs\` 下留下临时 QC 报告,可随时删除。

## 四、重新提取节点与边、重新画图(可选)

提取脚本只用 Python 标准库,系统 `python` 即可:

```powershell
python (Join-Path $pkg '04_分析脚本\ast_graph.py') $repo (Join-Path $env:TEMP 'graph_extract_check.json')
python (Join-Path $pkg '04_分析脚本\reach.py') (Join-Path $env:TEMP 'graph_extract_check.json')
python (Join-Path $pkg '04_分析脚本\ret_kinds.py') $repo (Join-Path $env:TEMP 'graph_extract_check.json') | Select-String -NotMatch '^(storyline_|assembly_)'
```

- 这三条要在**应用补丁之后**的仓库上跑。
- `ret_kinds.py` 的输出:主链共 26 行,17 行 `FULL`(返回整份 state)、9 行 `DELTA`(只返回变更字段)。`storyline_` / `assembly_` 开头的子图节点被 `Select-String` 过滤掉了。
- 为什么要看这个:同一超步里两个节点不能写同一个键;返回整份 state 的节点最容易撞车。

生成并校验图(需要 Node.js 18 及以上):

```powershell
Set-Location -LiteralPath (Join-Path $pkg '05_图表')
python gen_mermaid.py ..\04_分析脚本\graph_extract_old.json ..\04_分析脚本\graph_extract_new.json .
npm install
node validate_mermaid.mjs .
```

`gen_mermaid.py` 会比对"画出的边"与"真实边",漏画或多画都会直接报错。

## 五、重新生成设计文档(可选)

只用 Python 标准库:

```powershell
Set-Location -LiteralPath $pkg
python .\06_文档生成\build_doc.py $pkg .\06_文档生成\inventory_raw.json (Join-Path $env:TEMP '设计执行计划_复现.md')
```

文档里的数字(节点数、超步号、测试数)来自 `03_实证脚本\运行输出\`、`04_分析脚本\`、`07_测试记录\`,脚本会逐项断言;对不上就报错,不会静默出文档。
重新导出节点清单(`inventory_raw.json`)要用仓库的 `.venv` 的 python:

```powershell
& "$repo\.venv\Scripts\python.exe" (Join-Path $pkg '06_文档生成\dump_inventory.py') $repo (Join-Path $pkg '04_分析脚本\graph_extract_new.json') (Join-Path $env:TEMP 'inventory_raw_check.json')
```
