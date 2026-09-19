"""stub:EffectMeta 极简样本。"""

from .effect_meta import EffectMeta, EffectParam


免费VFX = EffectMeta("免费VFX", False, "4444444444444444444", "FX_V_001", "eeeeee")
VIP_VFX_A = EffectMeta(
    "VIP_VFX_A",
    True,
    "5555555555555555555",
    "FX_V_002",
    "ffffff",
    [EffectParam("p1", 0.5, 0.0, 1.0)],
)
VIP_VFX_B = EffectMeta("VIP_VFX_B", True, "6666666666666666666", "FX_V_003", "gggggg", [])
