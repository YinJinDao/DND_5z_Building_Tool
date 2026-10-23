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
        """移动。toward=True 靠近目标，False 远离。返回**实际**移动距离。

        靠近时实际位移受当前距离限制：距目标 20 尺时，走 30 尺的指令
        只会实际移动 20 尺。早期实现把距离钳制到 0 却按满额计入
        `travel_this_turn`，导致"退开不足 30 尺仍能满足冲锋条件"，
        使慢速坐骑的冲刺可持续性被高估。
        """
        usable = min(ft, self.movement_left)
        if toward:
            usable = min(usable, self.distance_ft)
        if usable <= 0:
            return 0
        self.movement_left -= usable
        self.travel_this_turn += usable
        self.distance_ft = (self.distance_ft - usable) if toward \
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


# ---------------------------------------------------------------- 资源时钟

@dataclass
class ResourcePool:
    """一个资源池。

    相对早期版本补了四项——它们是重检时发现的实质缺口，见
    `形式化/方案-打桩伤害链.md` §9.1：

      die_faces            卓越骰的骰面（消耗的价值，不只是枚数）
      unlimited_from_level 狂暴次数在 17 级起为"无"
      derived_from         超级战技次数 = floor(卓越骰 / 10)，是派生量
      per_circle           法术位按环分账，职业表只给总数
    """
    id: str
    name: str = ""
    capacity: int = 0
    recharge: str = "long_rest"     # short_rest | long_rest | per_n_long_rests | per_turn
    interval: int = 1               # recharge == per_n_long_rests 时的间隔
    die_faces: int | None = None    # 有骰面即表示"按枚消耗、按骰值计价"
    unlimited_from_level: int | None = None
    derived_from: tuple | None = None   # (源池 id, 除数)
    per_circle: dict | None = None      # {环阶: 数量}

    def __post_init__(self):
        if not self.name:
            self.name = self.id

    def die_mean(self):
        """一枚骰的期望值。d4→2.5，d6→3.5，d8→4.5，d10→5.5。"""
        return Fraction(self.die_faces + 1, 2) if self.die_faces else None

    def spend_value(self, count):
        """消耗 count 枚的期望骰值；无骰面的池返回枚数本身。"""
        return count * self.die_mean() if self.die_faces else Fraction(count)

    def effective_capacity(self, level=1, short_rests=0, long_rests=1, source_capacity=None):
        if self.unlimited_from_level is not None and level >= self.unlimited_from_level:
            return float("inf")
        if self.derived_from is not None:
            if source_capacity is None:
                raise ValueError(f"{self.id} 是派生池，需要先解析其源池")
            return int(source_capacity) // max(1, self.derived_from[1])
        if self.recharge == "short_rest":
            return self.capacity * (short_rests + 1)
        if self.recharge == "per_n_long_rests":
            return self.capacity * (long_rests // max(1, self.interval))
        return self.capacity

    def per_round_cap(self):
        """每回合门控；未声明则无单轮上限。"""
        return self.capacity if self.recharge == "per_turn" else None


def super_maneuver_uses(superiority_dice):
    """超级战技次数 = floor(可用卓越骰 / 10)。通过物品获得的卓越骰不计入。"""
    return superiority_dice // 10


@dataclass
class Budget:
    """跨轮资源预算：总量约束 + 每轮门控约束，两者都要满足。"""
    pools: dict = field(default_factory=dict)    # id -> ResourcePool
    short_rests: int = 0
    long_rests: int = 1
    level: int = 1

    def resolve(self):
        """解析全部池的有效容量，派生池依赖源池，最多两轮收敛。"""
        caps = {}
        pending = dict(self.pools)
        for _ in range(len(self.pools) + 1):
            progress = False
            for pid, pool in list(pending.items()):
                src = None
                if pool.derived_from is not None:
                    src = caps.get(pool.derived_from[0])
                    if src is None:
                        continue
                caps[pid] = pool.effective_capacity(
                    level=self.level, short_rests=self.short_rests,
                    long_rests=self.long_rests, source_capacity=src)
                del pending[pid]
                progress = True
            if not progress:
                break
        if pending:
            raise ValueError(f"资源池依赖无法解析: {list(pending)}")
        return caps

    def available(self, pid):
        return self.resolve()[pid]

    def check_schedule(self, per_round_spend):
        """per_round_spend: [(轮次, 池 id, 数量)]。返回违规列表。"""
        caps = self.resolve()
        errors = []
        totals, per_round = {}, {}
        for rnd, pid, amount in per_round_spend:
            if pid not in self.pools:
                errors.append(f"未声明资源池: {pid}")
                continue
            totals[pid] = totals.get(pid, 0) + amount
            per_round[(pid, rnd)] = per_round.get((pid, rnd), 0) + amount

        for pid, used in totals.items():
            cap = caps.get(pid, 0)
            if cap != float("inf") and used > cap:
                errors.append(f"{pid} 全程消耗 {used} 超过有效容量 {cap}")
        for (pid, rnd), used in per_round.items():
            cap = self.pools[pid].per_round_cap()
            if cap is not None and used > cap:
                errors.append(f"{pid} 第 {rnd} 轮消耗 {used} 超过每轮上限 {cap}")
        return errors

    def spend_report(self, per_round_spend):
        """报告每个池的消耗量与折算骰值——让"花了多少"可读。"""
        totals = {}
        for _, pid, amount in per_round_spend:
            totals[pid] = totals.get(pid, 0) + amount
        lines = []
        for pid, amount in sorted(totals.items()):
            pool = self.pools.get(pid)
            if pool is None:
                lines.append(f"{pid}: {amount}（未声明）")
                continue
            value = pool.spend_value(amount)
            extra = f" ≈ {float(value):.1f} 点骰值" if pool.die_faces else ""
            lines.append(f"{pool.name}: {amount}{extra}")
        return lines


# ---------------------------------------------------------------- 豁免型伤害

# ---------------------------------------------------------------- 对抗检定

def d20_tier_distribution(tiers):
    """优/劣势下 d20 结果的分布 {1..20: 概率}。

    n 重优势 = 掷 n+1 枚取最高；k 重劣势 = 掷 k+1 枚取最低。
    """
    tiers = int(tiers)
    n = tiers + 1 if tiers >= 0 else 1 - tiers
    dist = {}
    for k in range(1, 21):
        if tiers >= 0:
            hi = Fraction(k, 20) ** n
            lo = Fraction(k - 1, 20) ** n
        else:
            hi = Fraction(21 - k, 20) ** n
            lo = Fraction(20 - k, 20) ** n
        dist[k] = hi - lo
    return dist


def opposed_win_probability(attack_bonus, defender_bonus, attack_dice=None,
                            adv_tiers=0):
    """对抗检定的胜率 P(攻方 > 守方)。

    规则书用「若你胜出」——平手不算胜，因此是严格大于。

    - `attack_dice` 形如 `(枚数, 面数)`，对应「骰卓越骰加入检定」
    - `defender_bonus` 可给单个加值或一组；给一组时按守方**选择最有利者**处理，
      即每种加值分别算胜率后取加权平均（守方会挑最难赢的那个，故取最小值更保守，
      这里取平均并在文档中注明口径）
    """
    atk_tier = d20_tier_distribution(adv_tiers)
    plain = d20_tier_distribution(0)          # 守方不享有攻方的优/劣势

    def shift(dist, bonus):
        out = {}
        for face, p in dist.items():
            out[face + bonus] = out.get(face + bonus, Fraction(0)) + p
        return out

    def add_dice(dist, count, faces):
        cur = dist
        for _ in range(int(count)):
            nxt = {}
            for v, p in cur.items():
                for f in range(1, int(faces) + 1):
                    nxt[v + f] = nxt.get(v + f, Fraction(0)) + p * Fraction(1, faces)
            cur = nxt
        return cur

    a_dist = shift(atk_tier, attack_bonus)
    if attack_dice:
        a_dist = add_dice(a_dist, attack_dice[0], attack_dice[1])

    bonuses = (defender_bonus if isinstance(defender_bonus, (list, tuple))
               else [defender_bonus])
    per_defender = []
    for b in bonuses:
        d_dist = shift(plain, b)
        win = Fraction(0)
        for av, ap in a_dist.items():
            win += ap * sum((q for v, q in d_dist.items() if v < av), Fraction(0))
        per_defender.append(win)
    # 守方在多个加值中择一（如"运动或体操，由它选择"）→ 取对攻方最不利的那个
    return min(per_defender)


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
