"""``tests/integration/`` 的共享 fixture。

重点:隔离测试库
----------------
PostgreSQL 集成测试**不能**直接打生产库 ``video_workflow``。

原因:这些用例会真的建会话 / 改标题 / 软删除 / 写 checkpoint。
打在生产库上 = 往用户的库里灌垃圾数据(标题形如「分页xxx」「独特前缀ABC」),
而且测试之间的数据会互相干扰(比如"标题前缀过滤"会翻到别的用例留下的行)。

约定:
- 每个测试文件/会话用**带唯一后缀**的独立库 ⇒ 用例之间彻底隔离;
- 库名带每次运行的随机后缀 ⇒ 并发跑多个 pytest 会话不会互相 TRUNCATE;
- 会话结束时自动删库 ⇒ 生产库一个字节都不动。

库连不上时 **fail**(不静默 skip)—— 避免"假绿"(方案 §7.2)。
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
import uuid
from typing import Callable
from urllib.parse import urlparse, urlunparse

import pytest


def default_postgres_uri() -> str:
    """默认 URI = ``config.POSTGRES_URI``(库名 ``video_workflow``)。"""
    try:
        from config import POSTGRES_URI

        return POSTGRES_URI
    except Exception:  # pragma: no cover - config 不可导入时的兜底
        return "postgresql://postgres:devpass@localhost:5432/video_workflow"


def base_postgres_uri() -> str:
    """"用户显式指定的目标库" —— 只用来取 host/port/口令,不用它的库名。"""
    return (
        os.environ.get("TEST_POSTGRES_URI")
        or os.environ.get("POSTGRES_URI")
        or default_postgres_uri()
    )


def uri_is_explicit() -> bool:
    """用户是否显式给了 URI(显式 ⇒ 连不上必须 fail,不许 skip)。"""
    return bool(os.environ.get("TEST_POSTGRES_URI") or os.environ.get("POSTGRES_URI"))


def postgres_reachable(uri: str, timeout: float = 1.5) -> bool:
    """TCP 探活(只解析 host:port,不建真连接)。"""
    try:
        parsed = urlparse(uri)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def dbname_from_uri(uri: str) -> str:
    """取 URI 里的库名。"""
    parsed = urlparse(uri)
    return (parsed.path or "").lstrip("/")


def uri_with_dbname(uri: str, dbname: str) -> str:
    """换掉 URI 里的库名(其余部分原样保留)。"""
    parsed = urlparse(uri)
    return urlunparse(parsed._replace(path=f"/{dbname}"))


def admin_uri_for(uri: str) -> str:
    """从目标 URI 推出管理 URI(把库名换成 ``postgres``)。"""
    return uri_with_dbname(uri, "postgres")


def create_test_db(uri: str) -> bool:
    """建库 + 跑应用层迁移(幂等)。成功返回 True。"""
    import psycopg

    admin = admin_uri_for(uri)
    name = dbname_from_uri(uri)
    if not name:
        return False
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{name}"')
    except Exception:
        return False
    try:
        from db.migrations import apply_migrations_to_db

        apply_migrations_to_db(uri=uri)
    except Exception:
        return False
    return True


def drop_test_db(uri: str) -> None:
    """删掉测试库(先踢掉残留连接)。失败不影响测试结论。"""
    import psycopg

    admin = admin_uri_for(uri)
    name = dbname_from_uri(uri)
    if not name:
        return
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    except Exception:
        pass


class PostgresTestDbFactory:
    """隔离测试库的工厂。

    - ``create()``  → 建一个带唯一后缀的库,返回 URI;
    - ``ensure(uri)`` → 确保某个已指定的库存在且表已建(不删);
    - ``drop(uri)``  → 删库。
    """

    def __init__(self) -> None:
        self.created: list[str] = []

    def create(self, prefix: str = "video_workflow_test") -> str:
        """建一个**唯一命名**的隔离库,返回 URI。"""
        base = base_postgres_uri()
        if not postgres_reachable(base):
            pytest.fail(
                f"Postgres 不可达: {base} —— PostgreSQL 集成测试不允许静默 skip"
                "(方案 §7.2);请先启动 Postgres 容器。"
            )
        name = f"{prefix}_{uuid.uuid4().hex[:8]}"
        uri = uri_with_dbname(base, name)
        if not create_test_db(uri):
            pytest.fail(f"无法创建隔离测试库 {name}(权限不足?)")
        self.created.append(uri)
        return uri

    def ensure(self, uri: str) -> bool:
        """确保指定库存在且应用层迁移已跑过。"""
        return create_test_db(uri)

    def drop(self, uri: str) -> None:
        """删库。"""
        drop_test_db(uri)

    def cleanup(self) -> None:
        """删掉本工厂建过的所有库。"""
        for uri in self.created:
            drop_test_db(uri)
        self.created.clear()


@pytest.fixture(scope="session")
def postgres_test_db() -> PostgresTestDbFactory:
    """隔离测试库工厂(会话级)。"""
    factory = PostgresTestDbFactory()
    yield factory
    factory.cleanup()


@pytest.fixture(scope="session")
def isolated_postgres_db(postgres_test_db: PostgresTestDbFactory) -> Callable[[], str]:
    """工厂:建一个**隔离**的测试库,返回它的 URI;会话结束时自动删掉。

    用法::

        def test_x(isolated_postgres_db):
            uri = isolated_postgres_db()
            store = WorkflowEventStore(uri=uri)  # 或任何需要独立库的被测对象

    连不上 Postgres ⇒ 直接 **fail**(不静默 skip,避免"假绿")。
    """
    return postgres_test_db.create


# ---------------------------------------------------------------------------
# 2026-11-17 P1(英文字幕真实翻译):真实翻译模式 fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def real_mode_translator():
    """把默认翻译客户端换成"真实模式"(LLMTranslateClient + 可编程假 LLM)。

    背景:P1 起 stub/Mock 翻译被 fail-closed 门禁拦截(node_08b 不写正式
    英文字幕、node_17 不送英文 TTS)。需要英文字幕/配音/封面全链产物的
    集成用例,用本 fixture 模拟"真实后端已装配"的运行;假 LLM 的 chat()
    按输入 index 逐段给出确定译文,满足输出契约校验。

    用例结束恢复 Mock 默认,防跨用例污染。
    """
    import json as _json

    from jy_common.translate_client import (
        LLMTranslateClient,
        MockTranslateClient,
        set_default_client,
    )

    class _GenericRealLLM:
        def chat(self, *, system_prompt, user_prompt, media=None,
                 json_schema=None, temperature=0.3, top_p=0.9, max_tokens=4096):
            payload = _json.loads(user_prompt)
            return _json.dumps(
                {"translations": [
                    {"index": s["index"], "text_en": f"EN-{s['index']}"}
                    for s in payload["segments"]
                ]},
                ensure_ascii=False,
            )

    set_default_client(
        LLMTranslateClient(_GenericRealLLM(), translator_id="integration-real")
    )
    yield
    set_default_client(MockTranslateClient())


@pytest.fixture(autouse=True)
def _stub_sdk_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 编译素材替身(2026-11-22 SDK 链转正;与 test_jianying_sdk_phase7 同款)。

    集成用例的草稿 media 是夹具假路径,真实 pyJianYingDraft VideoMaterial 会以
    pymediainfo 探测真实文件;替身跳过探测,让 node_11 编译链路确定性走通
    (MediaInfo 探测本身由 unit phase1-7 覆盖,不在此重复)。仅测进程内生效。
    """
    from jianying_sdk._sdk_loader import ensure_sdk_on_path

    ensure_sdk_on_path()

    from pyJianYingDraft.local_materials import CropSettings
    import pyJianYingDraft
    from pyJianYingDraft import AudioMaterial, VideoMaterial

    # 本地替身(必须继承真类:SDK add_segment 做 isinstance 校验;phase2 同款);
    # 时长放大到 1h,任意夹具 timerange 都能落进素材时长内(SDK 会校验
    # 截取范围 <= 素材时长;phase2 定长 30s 会被 35s 夹具拒绝)。
    class _StubVideoMaterial(VideoMaterial):
        def __init__(self, path: str, material_name: str | None = None, crop_settings=None):
            self.material_name = material_name or Path(path).name
            self.material_id = "stubvid-" + Path(path).name.replace(".", "-")
            self.path = path
            self.duration = 3_600_000_000
            self.width = 1080
            self.height = 1920
            self.crop_settings = crop_settings
            self.crop_ratio = "free"
            self.crop_scale = 1.0
            self.type = "local"

        def export_json(self) -> dict:
            crop = self.crop_settings.export_json() if self.crop_settings else CropSettings().export_json()
            return {
                "category_name": "local", "crop": crop, "crop_ratio": "free",
                "crop_scale": 1.0, "duration": self.duration, "height": self.height,
                "id": self.material_id, "local_material_id": "", "material_id": self.material_id,
                "material_name": self.material_name, "media_path": "", "path": self.path,
                "type": self.type, "width": self.width,
            }

    class _StubAudioMaterial(AudioMaterial):
        def __init__(self, path: str, material_name: str | None = None):
            self.material_name = material_name or Path(path).name
            self.material_id = "stubaud-" + Path(path).name.replace(".", "-")
            self.path = path
            self.duration = 3_600_000_000

        def export_json(self) -> dict:
            return {
                "category_id": "", "category_name": "local", "duration": self.duration,
                "id": self.material_id, "material_id": self.material_id,
                "material_name": self.material_name, "path": self.path, "type": "extract_music",
            }

    monkeypatch.setattr(
        pyJianYingDraft, "VideoMaterial",
        lambda path, material_name=None, crop_settings=None: _StubVideoMaterial(
            path, material_name, crop_settings
        ),
    )
    monkeypatch.setattr(
        pyJianYingDraft, "AudioMaterial",
        lambda path, material_name=None: _StubAudioMaterial(path, material_name),
    )
