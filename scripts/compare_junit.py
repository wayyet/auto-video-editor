#!/usr/bin/env python3
"""对照两份 pytest 的 junit XML:逐个用例比较结果,找出"新增失败"。

用法:  python compare_junit.py <基线.xml> <当前.xml>
生成 XML:  python -m pytest <目录> -q --junitxml=<文件.xml>

- 只用标准库。
- 用例的身份 = classname + name;结果分 通过 / 失败 / 错误 / 跳过。
- "失败"和"错误"都算坏结果。基线里就坏的用例不算新增。
退出码:0 = 没有新增失败、没有消失的用例;1 = 有。
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections import Counter

RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 2}


def load(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        status = "passed"
        if case.find("failure") is not None:
            status = "failed"
        elif case.find("error") is not None:
            status = "error"
        elif case.find("skipped") is not None:
            status = "skipped"
        if key not in result or RANK[status] > RANK[result[key]]:
            result[key] = status
    return result


def summary(cases: dict[str, str]) -> str:
    c = Counter(cases.values())
    return f"共 {len(cases)} 个：通过 {c['passed']}，失败 {c['failed']}，错误 {c['error']}，跳过 {c['skipped']}"


def bad(status: str | None) -> bool:
    return status in ("failed", "error")


def show(title: str, items: list[str]) -> None:
    print(f"{title}：{len(items)} 个")
    for name in items[:30]:
        print(f"  - {name}")
    if len(items) > 30:
        print(f"  …还有 {len(items) - 30} 个")


def main() -> int:
    base, now = load(sys.argv[1]), load(sys.argv[2])
    print("基线  ", summary(base))
    print("当前  ", summary(now))
    new_bad = sorted(k for k, s in now.items() if bad(s) and not bad(base.get(k)))
    gone = sorted(set(base) - set(now))
    fixed = sorted(k for k, s in base.items() if bad(s) and k in now and not bad(now[k]))
    added = sorted(set(now) - set(base))
    show("新增失败或错误（必须为 0）", new_bad)
    show("消失的用例（必须为 0）", gone)
    show("新增用例（数量应等于预期）", added)
    show("原来失败、现在不失败", fixed)
    return 1 if new_bad or gone else 0


if __name__ == "__main__":
    sys.exit(main())
