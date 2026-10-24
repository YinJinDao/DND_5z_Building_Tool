#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""条件策略求值（D-018）。

策略不是固定脚本，而是**按决策时点分支**的规则集：战技的结算时点分层为
前置 / 掷骰后宣判前 / 命中后 / 失手后 / 重击时。限次资源的价值取决于它花在
什么结果上，因此一轮的期望伤害要在**命中/重击结果树**上求值，而不是假设
攻击相互独立。

实现方式：对 k 次攻击枚举 3^k 个结果叶子（未命中/命中/重击），
在每个叶子按规则顺序结算，最后按概率加权。k=8 时 6561 个叶子，可精确求解。
"""

from dataclasses import dataclass, field, replace
from fractions import Fraction

from evaluator import Attack, DamageComponent, apply_target_pipeline, dice_mean

MISS, HIT, CRIT = "miss", "hit", "crit"


@dataclass
class Rule:
    """一条条件规则。

    trigger: "miss" | "hit" | "crit" | "hit_or_crit"
    on_attack: 0-based 攻击序号；None 表示每次攻击都适用
    """
    trigger: str
    on_attack: int | None = None
    extra_dice: list = field(default_factory=list)   # [DamageComponent]
    next_adv: int = 0                                 # 影响后续攻击的优势层数
    spends: dict = field(default_factory=dict)        # 资源消耗（仅用于可行性校验）
    extra_attack: dict | None = None                  # 命中后追加一次攻击
    extra_attack_all_hit: dict | None = None          # 本轮所有攻击都命中才追加
    once_per_turn: bool = False                       # 整轮只结算一次（偷袭）
    #: 连续命中计数（暴雨梨花）：第 X 次连续命中额外造成 per_streak×X。
    #: 计数在结果树上按分支维护——失手归零，命中累加。不能预先算成常数，
    #: 因为"连续第几次"本身取决于这条分支上前几击是否命中。
    streak_damage: dict | None = None
    #: 整场一次（毒击的毒素能量池、每场战斗一次的爆发）。池子是有限总量，
    #: 花掉就没了。跨轮传递的是"还有多少概率没用掉"——因为伤害对池子线性，
    #: 按概率缩放是精确的，和连击数用的是同一个道理。
    once_per_fight: bool = False
    name: str = ""

    def matches(self, outcome, index):
        if self.on_attack is not None and self.on_attack != index:
            return False
        if self.trigger == "hit_or_crit":
            return outcome in (HIT, CRIT)
        return self.trigger == outcome


def _damage_for_outcome(attack, outcome, target, extra):
    """给定结果时的伤害（含目标端结算）。结果已确定，骰子取期望。"""
    if outcome == MISS:
        return Fraction(0)
    by_type = {}
    for comp in list(attack.components) + list(extra):
        key = (comp.dtype, comp.irreducible)
        d = by_type.setdefault(key, {"mean": Fraction(0), "dbl": Fraction(0)})
        d["mean"] += comp.mean
        if comp.crit_doubles:
            d["dbl"] += dice_mean(comp.dice) * comp.w
    total = Fraction(0)
    for (dtype, irreducible), d in by_type.items():
        amount = d["mean"] + (d["dbl"] if outcome == CRIT else Fraction(0))
        if irreducible:
            total += amount
            continue
        dmg, _ = apply_target_pipeline(amount, dtype, target)
        total += dmg
    return total


def evaluate_turn(attacks, rules=None, target=None, state=None, carry=None):
    """求一轮的期望伤害。

    `carry` 是**跨轮状态**：不传就是一轮打完清零。目前只放连击计数——
    「暴雨梨花」的 X 指的是连续第几次命中，规则书说它只在失手或骰先攻时归零，
    也就是**跨轮保留**。每轮独立求值会把这条链切断，第 2 轮又从头算 X=1。

    返回 (期望伤害, 叶子数)。carry 就地更新，调用方无需接收。
    """
    rules = rules or []
    if target is None:
        raise ValueError("需要 target")
    acc = Fraction(0)
    leaves = 0
    start_streak = int((carry or {}).get("streak", 0))
    # 轮末连击数的**期望**。不能只取某一条分支的结果——连击数本身是随机的。
    # 好在伤害对连击数是线性的（额外 2×X），所以带期望值进下一轮是精确的：
    # E[2×(S+k)] = 2·E[S] + 2·k。
    acc_streak = Fraction(0)

    def dfs(i, ctx, prob, seq):
        nonlocal acc, leaves
        if prob == 0:
            return
        if i >= len(seq):
            leaves += 1
            acc_streak_now[0] += prob * ctx.get("streak", start_streak)
            for name, avail in ctx.get("pool_avail", {}).items():
                pf_next[name] = pf_next.get(name, Fraction(0)) + prob * avail
            return
        atk = seq[i]
        adv = atk.adv_tiers + ctx.get("adv_bonus", 0)
        a = replace(atk, adv_tiers=adv)
        p_hit, p_crit = a.probs(target.ac)

        outcomes = ((MISS, Fraction(1) - p_hit),
                    (HIT, p_hit - p_crit),
                    (CRIT, p_crit))
        for outcome, p in outcomes:
            if p <= 0:
                continue
            extra = []
            # 浅拷贝会把 pool_avail 的**同一个 dict** 带进每个分支，
            # 于是"这条分支花掉了池子"会污染兄弟分支——多一次攻击反而少算伤害
            next_ctx = dict(ctx)
            next_ctx["pool_avail"] = dict(ctx.get("pool_avail") or {})
            next_seq = seq
            # 跨攻击条件：本轮到目前为止是否全部命中（「刺剑大师」的精准连刺）
            if outcome == MISS:
                next_ctx["all_hit"] = False
            else:
                next_ctx.setdefault("all_hit", True)
            # 连续命中计数：失手归零，命中累加。同一轮内所有 streak 规则共享
            # 一个计数（规则书说的是"连续第 X 次攻击命中相同目标"）
            if outcome == MISS:
                next_ctx["streak"] = 0
            else:
                next_ctx["streak"] = next_ctx.get("streak", 0) + 1

            for rule in rules:
                if not rule.matches(outcome, i):
                    continue
                if rule.once_per_fight:
                    # 池子还没花掉的那部分概率。花掉的分支把可用性清零，
                    # 没花掉的分支原样带到下一轮——伤害对池子线性，缩放即精确
                    avail = next_ctx["pool_avail"].get(rule.name, Fraction(1))
                    if avail == 0:
                        continue
                    scaled = [replace(c, weight=(c.w * avail))
                              for c in rule.extra_dice]
                    extra.extend(scaled)
                    next_ctx["pool_avail"][rule.name] = Fraction(0)
                    continue
                if rule.streak_damage:
                    spec = rule.streak_damage
                    n = int(spec.get("per_streak", 1)) * next_ctx["streak"]
                    if n:
                        extra.append(DamageComponent(
                            "0", flat=n,
                            dtype=spec.get("damage_type", "same_as_weapon"),
                            slot=rule.name or "streak"))
                # 每回合一次（偷袭）：本分支已用掉就不再给。
                # 用 ctx 记录而不是跳过固定序号——第一击失手时，第二击仍应吃到偷袭。
                once_key = f"once:{rule.name}"
                if rule.once_per_turn:
                    if next_ctx.get(once_key):
                        continue
                    next_ctx[once_key] = True
                extra.extend(rule.extra_dice)
                if rule.next_adv:
                    next_ctx["adv_bonus"] = next_ctx.get("adv_bonus", 0) + rule.next_adv
                # 命中后追加攻击（如「疯狂连击」）：在结果树上动态延长攻击序列。
                # 只在该分支生效，故用局部 seq 副本；no_chain 防止无限递归。
                if rule.extra_attack and not next_ctx.get(f"fired:{rule.name}"):
                    spec = rule.extra_attack
                    bonus = int(spec.get("attack_bonus_mod", 0))
                    for _ in range(int(spec.get("count", 1))):
                        next_seq = next_seq + [replace(a, attack_bonus=a.attack_bonus + bonus,
                                                       components=list(a.components))]
                    next_ctx[f"fired:{rule.name}"] = True
            # 序列走完且全程命中 → 追加一次攻击（该攻击本身不再触发本条件）
            last = (i + 1 >= len(next_seq))
            if last and next_ctx.get("all_hit", True):
                for rule in rules:
                    if not rule.extra_attack_all_hit:
                        continue
                    key = f"fired_all:{rule.name}"
                    if next_ctx.get(key):
                        continue
                    spec = rule.extra_attack_all_hit
                    bonus = int(spec.get("attack_bonus_mod", 0))
                    for _ in range(int(spec.get("count", 1))):
                        next_seq = next_seq + [replace(a, attack_bonus=a.attack_bonus + bonus,
                                                       components=list(a.components))]
                    next_ctx[key] = True

            dmg = _damage_for_outcome(a, outcome, target, extra)
            acc += prob * p * dmg
            dfs(i + 1, next_ctx, prob * p, next_seq)

    acc_streak_now = [Fraction(0)]
    pf_start = dict((carry or {}).get("pool_avail") or {})
    pf_next = {}

    dfs(0, {"streak": start_streak, "pool_avail": dict(pf_start)},
        Fraction(1), list(attacks))
    if carry is not None:
        carry["streak"] = acc_streak_now[0]
        carry["pool_avail"] = pf_next
    return acc, leaves


def check_feasible(rules, resources):
    """可行性校验：策略声明的资源消耗不得超过持有量。"""
    need = {}
    for rule in rules:
        for pool, amount in (rule.spends or {}).items():
            need[pool] = need.get(pool, 0) + amount
    problems = []
    for pool, amount in need.items():
        if resources.get(pool, 0) < amount:
            problems.append(f"{pool} 需要 {amount}，持有 {resources.get(pool, 0)}")
    return problems
