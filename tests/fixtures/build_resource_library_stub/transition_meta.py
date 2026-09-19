"""stub:TransitionMeta 极简样本,覆盖 vip/非 vip/重复名 三个分支。"""

from .effect_meta import TransitionMeta


免费A = TransitionMeta("免费A", False, "1111111111111111111", "FX_T_001", "aaaaaa", 0.5, True)
VIP_A = TransitionMeta("VIP_A", True, "2222222222222222222", "FX_T_002", "bbbbbb", 1.0, False)
VIP_B = TransitionMeta("VIP_B", True, "3333333333333333333", "FX_T_003", "cccccc", 0.8, True)
# 同名重复(罕见但要支持):保留首条
VIP_A_DUP = TransitionMeta("VIP_A", True, "9999999999999999999", "FX_T_DUP", "dddddd", 0.5, True)
