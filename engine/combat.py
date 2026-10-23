#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""战斗状态、动作经济、距离与移动、豁免型伤害。

对应 `形式化/方案-打桩伤害链.md` §2.2（距离）、§7（动作经济）、§6（结算）。
打桩契约：目标不行动、场地无限大、初始距离固定、目标不移动。
"""

from dataclasses import dataclass, field
from fractions import Fraction

from evaluator import _clamp01


# ---------------------------------------------------------------- 距离与移动

@dataclass
class RangeProfile:
    """攻击可达范围。reach 用于近战，range 用于远程。"""
    reach_ft: int = 5
    range_ft: int | None = None

    def can_reach(self, distance_ft):
        if self.range_ft is not None:
            return distance_ft <= self.range_ft
        return distance_ft <= self.reach_ft


@dataclass
class CombatState:
    """一个角色的回合内状态。打桩下目标不移动，距离只由攻击者改变。"""
    round: int = 1
    distance_ft: int = 30
    speed_ft: int = 30
    movement_left: int = 0
    travel_this_turn: int = 0        # 本回合已直线移动的距离，冲锋类算子依赖

    action_used: bool = False
    bonus_used: bool = False
    reaction_used: bool = False
    interaction_used: bool = False

    attack_instances: int = 1        # 攻击动作内可用的攻击次数
    attacks_spent: int = 0

    resources: dict = field(default_factory=dict)

    def start_turn(self, speed_ft=None):
        if speed_ft is not None:
            self.speed_ft = speed_ft
        self.movement_left = self.speed_ft
        self.travel_this_turn = 0
        self.action_used = self.bonus_used = False
        self.reaction_used = self.interaction_used = False
        self.attacks_spent = 0

    def next_round(self, speed_ft=None):
        self.round += 1
        self.start_turn(speed_ft)

    def move(self, ft, toward=False):
        """移动。toward=True 靠近目标，False 远离。返回实际移动距离。"""
        usable = min(ft, self.movement_left)
        if usable <= 0:
            return 0
        self.movement_left -= usable
        self.travel_this_turn += usable
        self.distance_ft = max(0, self.distance_ft - usable) if toward \
            else self.distance_ft + usable
        return usable

    def spend_resource(self, pool, amount=1):
        have = self.resources.get(pool, 0)
        if have < amount:
            raise ValueError(f"资源不足：{pool} 需 {amount}，现有 {have}")
        self.resources[pool] = have - amount


# ---------------------------------------------------------------- 动作经济校验

SLOTS = ("action", "bonus_action", "reaction", "interaction")


@dataclass
class Step:
    """回合内一步。group 相同且相邻的步骤视为同一动作内的连续行动。"""
    name: str
    cost: dict = field(default_factory=dict)
    group: str | None = None


def validate_turn(steps, state: CombatState):
    """校验一条回合脚本，返回违规列表（空表示合法）。

    依据规则书「基础/回合与动作」：
      1. 每个槽位（动作/附赠动作/反应/交互）每回合各一次
      2. 不能在执行某个动作的过程中插入附赠动作
         （原文例：5 级狂战士不可「攻击→狂暴→额外攻击」）
      3. 「替换 X 次攻击」不得超过攻击动作内的剩余攻击次数
    """
    errors = []
    seen = {s: 0 for s in SLOTS}
    spent_attacks = 0

    for i, st in enumerate(steps):
        c = st.cost or {}
        for slot in SLOTS:
            if c.get(slot):
                seen[slot] += 1
                if seen[slot] > 1:
                    errors.append(f"第 {i+1} 步：{slot} 本回合已使用过")

        k = c.get("replace_attack", 0)
        if k:
            spent_attacks += k
            if spent_attacks > state.attack_instances:
                errors.append(
                    f"第 {i+1} 步：替换 {k} 次攻击，但本回合仅有 "
                    f"{state.attack_instances} 次可用，已用 {spent_attacks}")

    # 插入检查：同一个动作组的步骤必须连续。
    # 若某组被拆成两段，且两段之间夹了带附赠动作的步骤，即为违规。
    # （原文例：5 级狂战士不可「攻击→狂暴→额外攻击」）
    positions = {}
    for i, st in enumerate(steps):
        if st.group:
            positions.setdefault(st.group, []).append(i)
    for group, idxs in positions.items():
        lo, hi = min(idxs), max(idxs)
        for j in range(lo + 1, hi):
            if (steps[j].cost or {}).get("bonus_action"):
                errors.append(
                    f"第 {j+1} 步：不能在动作组「{group}」执行过程中插入附赠动作")
    return errors


# ---------------------------------------------------------------- 豁免型伤害

def save_fail_probability(dc, save_bonus):
    """豁免失败概率。d20 + 加值 ≥ DC 即成功。"""
    return _clamp01(Fraction(dc - save_bonus - 1, 20))


def expected_save_damage(dc, save_bonus, amount, half_on_save=False):
    """豁免型伤害的期望值。

    半伤按方案 §6.4 的取整口径：每一步向下取整。
    """
    p_fail = save_fail_probability(dc, save_bonus)
    p_success = 1 - p_fail
    if not half_on_save:
        return p_fail * amount
    half = Fraction(int(amount) // 2) if amount.denominator == 1 \
        else Fraction(amount.numerator // (amount.denominator * 2))
    return p_fail * amount + p_success * half
