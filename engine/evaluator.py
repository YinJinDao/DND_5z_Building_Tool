#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5z 打桩伤害引擎：单次攻击 → 期望伤害。

实现 `形式化/方案-打桩伤害链.md` §6 的结算管线。原则：

1. **期望法**，不是抽样（D-019）。攻击的随机结果只有三态（未命中/命中/重击），
   伤害对骰子线性，因此可直接取期望。
2. **伤害按类型分账**，因为抗性/免疫/吸收都是按类型的。
3. **目标端顺序不可调**：抗性前减伤 → 抗性 → 易伤 → 抗性后减伤 → 吸收/免疫。

支持的骰式：NdM、NdM+K、纯常数。全部用期望值（NdM 的期望是 N*(M+1)/2）。
"""

from dataclasses import dataclass, field
from fractions import Fraction
import re

DICE_RE = re.compile(r"^\s*(?:(\d+)\s*)?d\s*(\d+)\s*(?:([+-])\s*(\d+))?\s*$")
FLAT_RE = re.compile(r"^\s*([+-]?\s*\d+)\s*$")


def dice_mean(expr):
    """'2d6' → 7.0；'1d8+2' → 6.5；'3' → 3.0。用 Fraction 保证精确。"""
    if isinstance(expr, (int, float)):
        return Fraction(expr)
    m = DICE_RE.match(str(expr))
    if m:
        n = int(m.group(1) or 1)
        faces = int(m.group(2))
        flat = int(m.group(4) or 0) * (-1 if m.group(3) == "-" else 1)
        return Fraction(n * (faces + 1), 2) + flat
    m = FLAT_RE.match(str(expr))
    if m:
        return Fraction(int(m.group(1).replace(" ", "")))
    raise ValueError(f"无法解析骰式: {expr!r}")


def _clamp01(x):
    return max(Fraction(0), min(Fraction(1), x))


@dataclass
class DamageComponent:
    """一个伤害构成项。槽位命名见方案 §5.4（武器骰/属性/熟练/专长/战技/骰池…）。"""
    dice: str = "0"
    flat: int = 0
    dtype: str = "physical"      # 伤害类型；physical 为占位，实际应给出具体类型
    slot: str = "other"
    crit_doubles: bool = True    # 重击时骰子是否翻倍（固定值一律不翻）

    @property
    def mean(self):
        return dice_mean(self.dice) + self.flat


@dataclass
class Attack:
    attack_bonus: int = 0
    adv_tiers: int = 0           # 优势层数，可为负；逐层抵消后的净值
    crit_range: int = 1          # d20 ≥ 21 - crit_range 即重击
    auto_hit: bool = False
    components: list = field(default_factory=list)

    def thresholds(self, ac):
        t_hit = ac - self.attack_bonus
        t_crit = 21 - self.crit_range
        return t_hit, max(t_hit, t_crit)

    def probs(self, ac):
        """返回 (P_hit, P_crit)。

        自动命中的攻击**没有攻击检定**，因此不会重击（魔法飞弹即为此类）。
        早期实现把它当成"必定重击"，会让自动命中类法术的期望值虚高。
        """
        if self.auto_hit:
            return Fraction(1), Fraction(0)
        t_hit, t_both = self.thresholds(ac)
        p_hit = _clamp01(Fraction(21 - t_hit, 20))
        p_both = _clamp01(Fraction(21 - t_both, 20))
        n = self.adv_tiers
        if n >= 0:
            k = n + 1
            return 1 - (1 - p_hit) ** k, 1 - (1 - p_both) ** k
        k = 1 - n
        return p_hit ** k, p_both ** k


@dataclass
class TargetProfile:
    """目标侧参数。对应方案 §5.2 的 TargetProfile。"""
    ac: int = 15
    resist: tuple = ()
    vuln: tuple = ()
    immune: tuple = ()
    absorb: tuple = ()
    pre_reduction: dict = field(default_factory=dict)   # 抗性前减伤 {类型|'all': 值}
    post_reduction: dict = field(default_factory=dict)  # 抗性后减伤
    saves: dict = field(default_factory=dict)           # 六项豁免加值，供豁免型效果使用


def apply_target_pipeline(amount, dtype, target: TargetProfile):
    """按类型结算。返回 (最终伤害, 治疗量)。

    顺序依据「基础/伤害与治疗」，每一步向下取整：
      抗性前减伤 → 抗性（floor 半）→ 易伤（×2）→ 抗性后减伤 → 吸收/免疫
    """
    if amount <= 0:
        return Fraction(0), Fraction(0)

    pre = target.pre_reduction.get(dtype, 0) + target.pre_reduction.get("all", 0)
    amount = max(Fraction(0), amount - pre)

    if dtype in target.resist:
        amount = Fraction(amount.numerator // (amount.denominator * 2)) if amount.denominator != 1 \
            else Fraction(int(amount) // 2)
    if dtype in target.vuln:
        amount = amount * 2

    post = target.post_reduction.get(dtype, 0) + target.post_reduction.get("all", 0)
    amount = max(Fraction(0), amount - post)

    if dtype in target.absorb:
        return Fraction(0), amount          # 吸收优先于免疫，转为治疗
    if dtype in target.immune:
        return Fraction(0), Fraction(0)
    return amount, Fraction(0)


def expected_damage(attack: Attack, target: TargetProfile):
    """单次攻击的期望伤害：按类型分账后求和。返回 (伤害, 治疗)。"""
    p_hit, p_crit = attack.probs(target.ac)

    total = Fraction(0)
    doubled = Fraction(0)

    # 按类型聚合：抗性/免疫/吸收都按类型作用
    by_type = {}
    for comp in attack.components:
        d = by_type.setdefault(comp.dtype, {"mean": Fraction(0), "double": Fraction(0)})
        d["mean"] += comp.mean
        if comp.crit_doubles:
            d["double"] += dice_mean(comp.dice)

    healed = Fraction(0)
    for dtype, d in by_type.items():
        # 重击只翻骰子，不翻固定加值
        exp_amount = p_hit * d["mean"] + p_crit * d["double"]
        dmg, heal = apply_target_pipeline(exp_amount, dtype, target)
        total += dmg
        healed += heal
    return total, healed
