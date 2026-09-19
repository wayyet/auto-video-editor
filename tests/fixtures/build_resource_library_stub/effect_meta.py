"""stub metadata — 仅供单测 build_resource_library.py 使用。

不导入真实的 pyJianYingDraft,直接定义最小可用的构造器签名。
"""

from typing import List


class EffectParam:
    def __init__(self, name, default_value, min_value, max_value):
        self.name = name
        self.default_value = default_value
        self.min_value = min_value
        self.max_value = max_value


class EffectMeta:
    def __init__(self, name, is_vip, resource_id, effect_id, md5, params=None):
        self.name = name
        self.is_vip = is_vip
        self.resource_id = resource_id
        self.effect_id = effect_id
        self.md5 = md5
        self.params = params if params is not None else []


class AnimationMeta:
    def __init__(self, title, is_vip, duration, resource_id, effect_id, md5):
        self.title = title
        self.is_vip = is_vip
        self.duration = duration
        self.resource_id = resource_id
        self.effect_id = effect_id
        self.md5 = md5


class TransitionMeta:
    def __init__(self, name, is_vip, resource_id, effect_id, md5, default_duration, is_overlap):
        self.name = name
        self.is_vip = is_vip
        self.resource_id = resource_id
        self.effect_id = effect_id
        self.md5 = md5
        self.default_duration = default_duration
        self.is_overlap = is_overlap
