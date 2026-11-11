"""文案模板库离线标注脚本(2026-11-12 noop 治理阶段1,设计文档 §1.1 任务2/3)。

参考 FireRed-OpenStoryline ``scripts/llm_script_template_label.py`` 的增量标注
思路(内容 hash 未变 → 复用旧元数据;变化 → 重新标注),按本项目约束重写:

- 只用**本项目已配置的 LLM 出口**(``agent_gate.factory.build_client`` →
  ``vlm_client.set_default_client``),不引入 FireRed 的 DeepSeek SDK / 第二套 Key;
- hash 用 SHA-256(FireRed 是 MD5);
- 标签纪律与 FireRed ``script_template_label`` prompt 一致:一句话描述(≤30 字)+
  1~3 个标签,枚举 [Life, Food, Beauty, Entertainment, Travel, Tech, Business,
  Vehicle, Health, Family, Pets, Knowledge]。

用法::

    # 预览(dry-run,不写任何文件;重算 hash、标注需求与差异)
    .venv/Scripts/python.exe scripts/label_script_templates.py

    # 写回 meta.json(默认后端是 stub;标注需要真实后端)
    .venv/Scripts/python.exe scripts/label_script_templates.py --apply --llm-backend mcode-cli

    # 强制重新标注(即使内容未变)
    .venv/Scripts/python.exe scripts/label_script_templates.py --apply --llm-backend mcode-cli --force

纪律:只收录原创或已获授权的模板文本;路径全部来自命令行/仓库相对结构,脚本内不嵌绝对路径。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storyline_capabilities.script_template_rec import TAG_ENUM  # noqa: E402
from storyline_capabilities.vlm_client import (  # noqa: E402
    StubLLMClient,
    chat_json_with_trace,
    get_default_client,
)

#: meta.json 当前 schema 版本
META_SCHEMA_VERSION = "1.0"

_LABEL_SYSTEM_PROMPT = (
    "你是文案模板标注助手。对给定的短视频文案模板,判断它的写作风格与典型使用场景,"
    "只输出一个 JSON 对象,不要输出任何解释:\n"
    '{"description": "<一句话概括写作风格与典型场景,不超过30个字>", '
    '"tags": ["<1~3个标签>"]}\n'
    "标签只能从以下枚举中选择:Life, Food, Beauty, Entertainment, Travel, Tech, "
    "Business, Vehicle, Health, Family, Pets, Knowledge。"
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="文案模板库离线标注:重算 SHA-256 + LLM 生成 description/tags,增量更新 meta.json"
    )
    p.add_argument(
        "--root",
        default=str(ROOT / "storyline_capabilities" / "resource" / "script_templates"),
        help="模板库根目录(默认仓库内 storyline_capabilities/resource/script_templates)",
    )
    p.add_argument("--apply", action="store_true", help="写回 meta.json(默认 dry-run 只预览)")
    p.add_argument(
        "--llm-backend",
        choices=("stub", "claude-cli", "mcode-cli"),
        default=None,
        help="标注所需 LLM 后端;不传 = 当前默认 client(通常是 stub,无法真实标注)",
    )
    p.add_argument(
        "--force", action="store_true", help="即使内容 hash 未变也重新标注(默认增量复用)"
    )
    return p.parse_args()


def _apply_llm_backend(backend: str | None) -> None:
    """照 scripts/run_workflow.py 的同款注入:build_client → set_default_client。"""
    if not backend:
        return
    from agent_gate.factory import build_client
    from storyline_capabilities.vlm_client import set_default_client

    set_default_client(build_client(backend))
    print(f"[llm] 已注入 LLM 后端: {backend}", file=sys.stderr)


def _language_from_stem(stem: str) -> str:
    if stem.endswith("_en"):
        return "en"
    if stem.endswith("_zh"):
        return "zh"
    return "zh"


def _derive_template_id(stem: str, version: int) -> str:
    return f"{stem}_v{version}"


def _llm_label(content: str) -> tuple[str, list[str]]:
    """调 LLM 生成 description/tags;严格校验,失败抛 RuntimeError。"""
    obj, trace = chat_json_with_trace(
        system_prompt=_LABEL_SYSTEM_PROMPT,
        user_prompt=f"模板正文:\n{content[:4000]}",
        schema={
            "type": "object",
            "properties": {"description": {"type": "string"}, "tags": {"type": "array"}},
        },
        client=get_default_client(),
    )
    description = str(obj.get("description") or "").strip()
    tags_raw = obj.get("tags")
    tags = [str(t).strip() for t in (tags_raw if isinstance(tags_raw, list) else []) if str(t).strip()]
    if not description or len(description) > 40:
        raise RuntimeError(f"description 不合规: {description!r}(raw_keys={sorted(obj.keys())})")
    if not tags or len(tags) > 3 or any(t not in TAG_ENUM for t in tags):
        raise RuntimeError(f"tags 不合规(须 1~3 个且在枚举内): {tags!r}(raw_keys={sorted(obj.keys())})")
    return description, tags


def main() -> int:
    args = _parse_args()
    root = Path(args.root).resolve()
    templates_dir = root / "templates"
    meta_path = root / "meta.json"
    if not templates_dir.is_dir():
        print(f"[错误] 模板目录不存在: {templates_dir}", file=sys.stderr)
        return 1

    existing: dict[str, dict] = {}
    if meta_path.is_file():
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            existing = {
                str(it.get("path") or ""): it
                for it in (raw.get("templates") or [])
                if isinstance(it, dict)
            }
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[警告] 现有 meta.json 解析失败({e!r}),将按全新库重建", file=sys.stderr)

    _apply_llm_backend(args.llm_backend)
    client_is_stub = isinstance(get_default_client(), StubLLMClient)

    txt_files = sorted(templates_dir.glob("*.txt"))
    if not txt_files:
        print(f"[错误] templates/ 下没有任何 .txt 模板: {templates_dir}", file=sys.stderr)
        return 1

    new_entries: list[dict] = []
    for txt in txt_files:
        rel_path = f"templates/{txt.name}"
        data = txt.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        content = data.decode("utf-8")
        stem = txt.stem
        prev = existing.get(rel_path)

        if prev and str(prev.get("content_sha256") or "").lower() == sha and not args.force:
            # 增量复用:内容未变,保留旧元数据(FireRed MD5 增量思路,hash 换 SHA-256)
            version = int(prev.get("version") or 1)
            new_entries.append(
                {
                    "template_id": str(prev.get("template_id") or _derive_template_id(stem, version)),
                    "path": rel_path,
                    "language": str(prev.get("language") or _language_from_stem(stem)),
                    "description": str(prev.get("description") or ""),
                    "tags": list(prev.get("tags") or []),
                    "content_sha256": sha,
                    "version": version,
                    "enabled": bool(prev.get("enabled", True)),
                }
            )
            print(f"[复用] {rel_path}: 内容未变,保留旧标注")
            continue

        # 内容变化(或新文件/强制):需要真实标注
        if client_is_stub:
            print(
                f"[错误] {rel_path} 需要(重新)标注,但当前 LLM 是 Stub。"
                "请加 --apply --llm-backend mcode-cli(或 claude-cli)后重跑。",
                file=sys.stderr,
            )
            return 1
        try:
            description, tags = _llm_label(content)
        except Exception as e:  # noqa: BLE001
            print(f"[错误] {rel_path} 标注失败: {e}", file=sys.stderr)
            return 1
        version = int((prev or {}).get("version") or 0) + 1
        new_entries.append(
            {
                "template_id": _derive_template_id(stem, version),
                "path": rel_path,
                "language": _language_from_stem(stem),
                "description": description,
                "tags": tags,
                "content_sha256": sha,
                "version": version,
                "enabled": bool((prev or {}).get("enabled", True)),
            }
        )
        print(f"[标注] {rel_path}: v{version} {description} {tags}")

    dropped = sorted(set(existing) - {e["path"] for e in new_entries})
    for rel in dropped:
        print(f"[移除] {rel}: 模板文件已不存在,条目将从 meta.json 移除")

    new_entries.sort(key=lambda e: e["template_id"])
    doc = {"schema_version": META_SCHEMA_VERSION, "templates": new_entries}
    preview = json.dumps(doc, ensure_ascii=False, indent=2)
    if not args.apply:
        print("--- dry-run 结果(meta.json 将变为如下内容;加 --apply 写回)---")
        print(preview)
        return 0

    meta_path.write_text(preview + "\n", encoding="utf-8")
    print(f"[完成] 已写回 {meta_path}({len(new_entries)} 条模板)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
