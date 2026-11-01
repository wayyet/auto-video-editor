"""GLM API(BigModel)endpoint 的**真实验证**(设计 §10.4 C-T1 / C-T2)。

⚠️ 这些用例会**真的出网**调用 BigModel endpoint。默认跳过:
只有设置了 ``OPENSTORYLINE_GLM_API_KEY`` 才运行。跑法::

    OPENSTORYLINE_GLM_API_KEY=sk-xxx ./.venv/.../python.exe -m pytest \\
        openstoryline/tests/test_glm_live.py -q

覆盖(设计 §10.4 C-T1/C-T2/C-T5):
- C-T1:文本对话(OpenAI 兼容 chat completions)
- C-T2:结构化输出(with_structured_output)与图像输入(VLM,需
  ``OPENSTORYLINE_GLM_VLM_MODEL``;没设就跳过图像用例 —— 对应
  「不满足则只做 LLM 预设」)
- C-T5:超时配置生效(把 timeout 拉到极小,断言请求超时抛错而不是挂死)
"""
from __future__ import annotations

import base64
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENSTORYLINE_GLM_API_KEY"),
    reason="未设置 OPENSTORYLINE_GLM_API_KEY:GLM live 验证默认跳过(C-T1/C-T2 待验证项)",
)

MODEL = os.getenv("OPENSTORYLINE_GLM_MODEL", "glm-4.6")
VLM_MODEL = os.getenv("OPENSTORYLINE_GLM_VLM_MODEL", "")
BASE_URL = os.getenv("OPENSTORYLINE_GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
API_KEY = os.getenv("OPENSTORYLINE_GLM_API_KEY", "")


def _make(model: str, timeout: float = 60.0):
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        base_url=BASE_URL,
        api_key=API_KEY,
        timeout=timeout,
        max_retries=0,
        streaming=False,
    )


def test_glm_text_chat_completion():
    """C-T1:文本对话基本可用。"""
    llm = _make(MODEL)
    resp = llm.invoke("只回答两个字:你好")
    assert resp.content and str(resp.content).strip()


def test_glm_structured_output():
    """C-T2:结构化输出(JSON schema 约束能被模型遵守)。"""
    from pydantic import BaseModel

    class Scene(BaseModel):
        subject: str
        mood: str

    llm = _make(MODEL).with_structured_output(Scene)
    out = llm.invoke("描述一个画面:夕阳下的海滩,一个孩子在奔跑。subject 一句话,mood 一个词。")
    assert isinstance(out, Scene)
    assert out.subject.strip() and out.mood.strip()


@pytest.mark.skipif(not VLM_MODEL, reason="未设置 OPENSTORYLINE_GLM_VLM_MODEL:VLM 图像验证跳过")
def test_glm_vlm_image_input():
    """C-T2:VLM 侧图像输入可用(1x1 红点 PNG)。"""
    # 1x1 红色像素 PNG
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    b64 = base64.b64encode(png).decode()
    llm = _make(VLM_MODEL)
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图主要是什么颜色?只回答颜色名。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ],
    }
    resp = llm.invoke([msg])
    assert str(resp.content).strip()


def test_glm_timeout_enforced():
    """C-T5:timeout 极小时请求快速失败(超时报错,而不是无限挂起)。"""
    import time

    llm = _make(MODEL, timeout=0.001)
    t0 = time.monotonic()
    with pytest.raises(Exception):
        llm.invoke("你好")
    assert time.monotonic() - t0 < 30.0, "timeout=0.001s 却耗时 30s+:超时配置没生效"
