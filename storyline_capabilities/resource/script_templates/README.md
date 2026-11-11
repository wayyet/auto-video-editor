# script_templates(文案模板库)

`storyline_script_template_recommendation` 节点的能力资源:为文案生成提供"风格/结构参考"模板。

## 目录结构

```text
script_templates/
├── meta.json          # 模板元数据清单(schema 见下)
└── templates/*.txt    # 模板正文(utf-8)
```

## meta.json schema(1.0)

每项字段:

| 字段 | 含义 |
|---|---|
| `template_id` | 全局唯一 ID,惯例 `<名>_<lang>_v<版本>` |
| `path` | 相对**本目录**的正文路径(代码侧限制在根目录内,防路径穿越) |
| `language` | `zh` / `en`(检索按语言隔离) |
| `description` | 一句话概括写作风格与典型场景(LLM 标注产物) |
| `tags` | 1~3 个标签,枚举:Life, Food, Beauty, Entertainment, Travel, Tech, Business, Vehicle, Health, Family, Pets, Knowledge |
| `content_sha256` | 正文 SHA-256;不匹配的候选会被丢弃并记 warning |
| `version` | 正整数版本号 |
| `enabled` | `false` 的模板不参与检索 |

前四个字段与 FireRed-OpenStoryline(b45011d)的 `id/path/description/tags` 语义兼容;后四项是本项目扩展(缓存失效、版本审计、中英文隔离)。

## 维护方式

- 正文内容变化后运行离线标注脚本重算 hash 并按需重标 description/tags:

```bash
.venv/Scripts/python.exe scripts/label_script_templates.py            # dry-run 预览
.venv/Scripts/python.exe scripts/label_script_templates.py --apply    # 写回 meta.json
```

- **只收录原创或已获授权的文本**;禁止未经授权批量收集他人作品。
- 模板正文是"风格/结构参考"而非事实来源;`generate_script` 注入时 prompt 已写明:画面事实必须来自素材概述与分组信息,不得复制模板中的人名、地点、品牌、数字或事件。
