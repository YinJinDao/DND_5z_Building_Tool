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


def evaluate_turn(attacks, rules=None, target=None, state=None):
    """求一轮的期望伤害。

    state 若给出（CombatState），会校验资源是否足以支撑策略中的消耗。
    返回 (期望伤害, 叶子数)。
    """
    rules = rules or []
    if target is None:
        raise ValueError("需要 target")
    acc = Fraction(0)
    leaves = 0

    def dfs(i, ctx, prob, seq):
        nonlocal acc, leaves
        if prob == 0:
            return
        if i >= len(seq):
            leaves += 1
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
            next_ctx = dict(ctx)
            next_seq = seq
            for rule in rules:
                if not rule.matches(outcome, i):
                    continue
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
            dmg = _damage_for_outcome(a, outcome, target, extra)
            acc += prob * p * dmg
            dfs(i + 1, next_ctx, prob * p, next_seq)

    dfs(0, {}, Fraction(1), list(attacks))
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
