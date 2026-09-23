"""阶段一单测:8 个 assembly_capabilities 工具函数的纯逻辑 / 错误路径覆盖。

策略:
- 真去调 ffprobe / ffmpeg 的场景:若本机有 ffprobe / ffmpeg 且 pytest tmp_path
  内有构造的视频样本,跑真链路(在 conftest 跳过开关控制);
- 默认情况:用 monkeypatch + tmp_path 测入参校验、错误路径、RunContext、
  helper 纯函数,证明搬过来的代码结构完整、可独立调用、不依赖 MCP / 云 ASR。
"""
