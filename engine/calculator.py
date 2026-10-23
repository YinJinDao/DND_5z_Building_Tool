#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""伤害计算器：构筑 + 输出策略 + 输出环境 → 期望伤害。

这是**一期（伤害计算器）**的完整内部构造。三份输入均为固定格式，
格式定义见 `形式化/输入格式.md`。

内部流程：

    Build ──derive──► Derived（属性/熟练/AC/HP/豁免/攻击加值）
      │                    │
      │                    └── + Environment.target ──► 命中率/重击率
      └── combat.weapon ──► 基础攻击 ──apply ops──► Attack
                                          │
    Strategy ──每轮 buffs/攻击次数/每击修饰──►  [Attack, ...]
                                          │
                                          └──evaluate_turn──► 每轮期望伤害
                                                                  │
                                                          rounds 求和

算子从 `形式化/ops/*.json` 读取，不写死在代码里——规则变更只改数据。
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from character import Bonus, Build, Contribution, derive
from combat import (Budget, CombatState, RangeProfile, ResourcePool, Step,
                    expected_save_damage, opposed_win_probability,
                    save_fail_probability, validate_turn)
from evaluator import (Attack, DamageComponent, TargetProfile,
                       apply_target_pipeline, dice_mean, expected_damage)
from strategy import Rule, evaluate_turn

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPS_DIR = os.path.join(REPO, "形式化", "ops")


# ---------------------------------------------------------------- 输入装载

def load_ops(ops_dir=OPS_DIR):
    """读取算子目录。id 与文件名去扩展名后一致。"""
    ops = {}
    if not os.path.isdir(ops_dir):
        return ops
    for fname in sorted(os.listdir(ops_dir)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(ops_dir, fname), encoding="utf-8") as fh:
            op = json.load(fh)
        ops[op["id"]] = op
    return ops


def _bonus_dict(src):
    """规范化「属性 → 加值」字典。"""
    return {k: int(v) for k, v in (src or {}).items()}


def load_rage_table(class_name="狂战士", path=None):
    """从 class_tables.jsonl 读取等级 → 狂暴伤害加值。不写死在代码里。"""
    path = path or os.path.join(REPO, "形式化", "data", "class_tables.jsonl")
    table = {}
    if not os.path.isfile(path):
        return table
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("class") != class_name:
                continue
            raw = row.get("columns", {}).get("狂暴伤害加值")
            if raw:
                table[str(row["level"])] = int(str(raw).replace("+", "").strip())
    return table


# ---------------------------------------------------------------- 构筑 → 派生

@dataclass
class ResolvedBuild:
    raw: dict
    build: Build
    derived: object
    weapon: dict
    rage_bonus: int
    trace: list = field(default_factory=list)
    size: str = "medium"


def resolve_build_full(build_doc, ops=None, rage_table=None, trace=None):
    """完整解析入口：先结算构筑期效果，再推导派生值。

    外部（测试、后续的阶段）应统一走这里，而不是直接调 resolve_build——
    否则会绕过构筑期效果（如凶蛮打手的属性 +1），得到与计算器不一致的数值。
    """
    ops = ops if ops is not None else load_ops()
    if rage_table is None:
        rage_table = load_rage_table()
    doc = apply_build_time_effects(build_doc, ops, trace)
    return resolve_build(doc, rage_table)


def resolve_build(build_doc, rage_table=None):
    """把构筑文档解析成可计算的派生值。"""
    meta = build_doc.get("meta", {})
    combat = build_doc.get("combat", {})
    scores = build_doc.get("scores", {})

    level = int(meta.get("level") or sum(c.get("level", 0)
                                         for c in build_doc.get("classes", [])) or 1)
    b = Build(
        level=level,
        base_scores=_bonus_dict(scores.get("base")),
        racial=_bonus_dict(scores.get("racial")),
        level_ups=_bonus_dict(scores.get("level_ups")),
        feat_bonus=_bonus_dict(scores.get("feat")),
        item_bonus=_bonus_dict(scores.get("item")),
        hit_die_faces=int(combat.get("hit_die_faces", 8)),
        armor=combat.get("armor", "none"),
        shield=bool(combat.get("shield", False)),
        proficient_saves=tuple(combat.get("proficient_saves", ())),
        weapon_attack_ability=combat.get("weapon", {}).get("ability", "str"),
        weapon_enhancement=int(combat.get("weapon", {}).get("enhancement", 0)),
        casting_ability=combat.get("casting_ability"),
    )
    d = derive(b)

    weapon = {
        "dice": combat.get("weapon", {}).get("dice", "1d4"),
        "damage_type": combat.get("weapon", {}).get("damage_type", "bludgeoning"),
        "enhancement": int(combat.get("weapon", {}).get("enhancement", 0)),
        "kind": combat.get("weapon", {}).get("kind"),      # 巨斧/巨剑/巨锤 等，供算子判断
        # 武器属性（双手/两用/轻型/触及…）：武器门控与 weapon_property 条件都靠它
        "properties": list(combat.get("weapon", {}).get("properties") or []),
    }
    size = combat.get("size", "medium")
    # 狂暴伤害加值取自职业表；是否生效由策略的 buffs 决定
    rage_bonus = int((rage_table or {}).get(str(level), 0))

    trace = [f"等级 {level}", f"熟练加值 +{d.prof}",
             f"AC {d.ac}", f"HP {d.hp}",
             f"攻击加值：{d.attack.explain()}"]
    if d.spell_attack is not None:
        trace.append(f"法术攻击加值：{d.spell_attack.explain()}｜"
                     f"法术豁免 DC {d.spell_dc}")
    return ResolvedBuild(raw=build_doc, build=b, derived=d, weapon=weapon,
                         rage_bonus=rage_bonus, trace=trace, size=size)


# ---------------------------------------------------------------- 算子应用

def _max_die(weapon_dice):
    """'1d12' → 12。威力攻击的加值取基础伤害骰最大值。"""
    try:
        return int(str(weapon_dice).lower().split("d")[1])
    except (IndexError, ValueError):
        return 0


def _eval_expr(expr, resolved):
    """算子表达式求值。当前只支持少量具名表达。"""
    if isinstance(expr, (int, float)):
        return int(expr)
    e = str(expr).strip()
    if e == "max_die(base_damage)":
        return _max_die(resolved.weapon["dice"])
    if e.isdigit():
        return int(e)
    raise ValueError(f"不支持的算子表达式: {expr!r}（可在 calculator._eval_expr 扩展）")


def weapon_dice_for(resolved, passive_ops, build_doc=None):
    """武器伤害骰。被动算子可覆盖或抬高下限。

    例：「巨大武器师」按体型把巨斧提升到 2d8/2d10/2d12；
        「斗殴高手」把临时武器/徒手的伤害骰抬到至少 1d6；
        「武艺」把徒手/武僧武器的骰抬到武艺骰（d4→d10）。
    """
    dice = resolved.weapon["dice"]
    for op in passive_ops:
        proj = op.get("projection") or {}
        # 武器种类排除（武艺不适用于重型武器）
        excl = proj.get("weapon_selector_exclude")
        if excl and weapon_matches(resolved.weapon, excl):
            continue
        over = proj.get("weapon_dice_override")
        if over:
            kinds = over.get("applies_to")
            if not kinds or resolved.weapon.get("kind") in kinds:
                # `all` 表示与体型无关的固定覆盖（缠蛇掠血把长鞭改为 1d8）；
                # 否则按体型取键（巨大武器师）。
                if "all" in over:
                    dice = scaled_value(over["all"], op, build_doc or {})
                elif resolved.size in over:
                    dice = over[resolved.size]
        # 「伤害骰变为 1d6，若原本已是 1d6 或更高则升一阶」（斗殴高手）
        minimum = proj.get("weapon_dice_min")
        if minimum:
            kinds = proj.get("weapon_dice_min_applies_to")
            if not kinds or resolved.weapon.get("kind") in kinds:
                minimum = scaled_value(minimum, op, build_doc or {})
                cur = dice_mean(dice)
                floor_val = dice_mean(minimum)
                if cur < floor_val:
                    dice = minimum
                elif proj.get("weapon_dice_step_up"):
                    dice = _step_up_dice(dice)
    return dice


def _step_up_dice(dice):
    """伤害骰升一阶：d6→d8，d8→d10，d10→d12。"""
    steps = {"d6": "d8", "d8": "d10", "d10": "d12"}
    n, _, faces = str(dice).lower().partition("d")
    return f"{n or 1}d{steps.get('d' + faces, faces)}"


def superiority_faces(level):
    """卓越骰的骰面：初始 d4，5 级 d6，11 级 d8，17 级 d10。"""
    if level >= 17:
        return 10
    if level >= 11:
        return 8
    if level >= 5:
        return 6
    return 4


def resolve_superiority_dice(expr, level):
    """把 '1dS' 解析成具体骰式。

    战技里大量写成「N 枚卓越骰的骰值」——骰面随等级变化，
    因此不能把 d4/d6/d8/d10 写死在算子里，必须按角色等级解析。
    """
    e = str(expr)
    if "dS" not in e:
        return e
    return e.replace("dS", f"d{superiority_faces(level)}")


FORMULA_CTX_KEYS = ("speed", "level", "temporary_hp", "max_die", "prof")


def eval_formula(expr, ctx):
    """公式型伤害求值。

    战技里存在"伤害 = 移动速度/2.5（上限 50）"这类写法，不是骰式。
    用受限命名空间求值——表达式来自本仓库自行编写的算子数据，不接受外部输入。
    """
    env = {k: ctx.get(k, 0) for k in FORMULA_CTX_KEYS}
    env.update({"min": min, "max": max, "int": int, "floor": lambda x: int(x // 1)})
    try:
        return int(eval(str(expr), {"__builtins__": {}}, env))  # noqa: S307
    except Exception as exc:
        raise ValueError(f"公式求值失败: {expr!r} — {exc}") from exc


def build_formula_context(resolved, extra=None):
    ctx = {
        "speed": resolved.raw.get("combat", {}).get("speed_ft", 30),
        "level": resolved.derived.level,
        "prof": resolved.derived.prof,
        "max_die": _max_die(resolved.weapon["dice"]),
    }
    if extra:
        ctx.update(extra)
    return ctx


def eval_level_expr(expr, level):
    """极小的等级表达式求值。目前支持 `level//N` 与 `level`。"""
    e = str(expr).replace(" ", "")
    if e == "level":
        return level
    if e.startswith("level//"):
        return level // int(e.split("//")[1])
    raise ValueError(f"不支持的等级表达式: {expr!r}")


# ---------------------------------------------------------------- 条件系统

# 条件词汇表（封闭）。`when` 里出现未列出的键一律报错——
# 静默放行会让算子"看起来生效、实际永不触发"，比报错危险得多。
CONDITION_KEYS = {
    "attacker_state",   # raging | mounted | hidden | has_adv
    "prior_action",     # moved | dashed | crit_this_turn | killed_this_turn
    "target_hp",        # below_max | below_half | zero_or_below
    "target_state",     # prone | restrained | frightened | unaware
    "target_size",      # smaller_than_you | at_most_your_size
    "weapon_property",  # 双手 | 两用 | 轻型 | 触及 | 投掷 | 弹药（取值是列表）
    "once_per_turn",    # 本回合是否尚未用过同类效果
}
CONDITION_VALUES = {
    "attacker_state": {"raging", "mounted", "hidden", "has_adv"},
    "prior_action": {"moved", "dashed", "crit_this_turn", "killed_this_turn"},
    "target_hp": {"below_max", "below_half", "zero_or_below"},
    "target_state": {"prone", "restrained", "frightened", "unaware"},
    "target_size": {"smaller_than_you", "at_most_your_size"},
    # 取值用中文：数据层与武器条目都是中文，混用两套词汇迟早对不上
    "weapon_property": {"双手", "两用", "轻型", "触及", "投掷", "弹药"},
}


def eval_condition(when, ctx):
    """判断一组条件是否同时满足。未识别的键抛错，不静默放行。"""
    for key, want in when.items():
        if key not in CONDITION_KEYS:
            raise ValueError(f"未知条件字段 {key!r}（见 CONDITION_KEYS 白名单）")
        if key == "once_per_turn":
            continue                      # 由调用方保证，此处不重复判断
        allowed = CONDITION_VALUES.get(key)
        if allowed and want not in allowed:
            raise ValueError(f"条件 {key} 的取值 {want!r} 不在 {sorted(allowed)} 中")
        got = ctx.get(key)
        # 集合型条件（武器属性）判包含：武器可能同时是「两用」和「轻型」
        if isinstance(got, (list, tuple, set)):
            if want not in got:
                return False
        elif got != want:
            return False
    return True


def apply_condition_effects(attack, effects, resolved, op_id):
    """把条件满足时的效果施加到攻击上（只允许伤害相关字段）。"""
    # damage_mult 尚未实现，故意不在白名单里——用了要报错而不是静默忽略
    ALLOWED = {"extra_dice", "damage_slots", "attack_bonus", "crit_range"}
    for key in effects:
        if key not in ALLOWED and key != "note":
            raise ValueError(f"条件效果字段 {key!r} 不在 {sorted(ALLOWED)} 中")
    if "attack_bonus" in effects:
        attack.attack_bonus += int(effects["attack_bonus"])
    if "crit_range" in effects:
        attack.crit_range += int(effects["crit_range"])
    for slot in effects.get("damage_slots", []):
        value = _eval_expr(slot["expr"], resolved)
        dtype = slot.get("type") or resolved.weapon["damage_type"]
        if dtype == "same_as_weapon":
            dtype = resolved.weapon["damage_type"]
        attack.components.append(
            DamageComponent("0", flat=value, dtype=dtype, slot=op_id,
                            irreducible=bool(slot.get("irreducible", False))))
    for extra in effects.get("extra_dice", []):
        dice = resolve_superiority_dice(extra["dice"], resolved.derived.level)
        dtype = extra.get("damage_type") or resolved.weapon["damage_type"]
        if dtype == "same_as_weapon":
            dtype = resolved.weapon["damage_type"]
        flat = int(extra.get("flat", 0))
        if extra.get("flat_by_size"):
            flat += int(extra["flat_by_size"].get(resolved.size, 0))
        attack.components.append(
            DamageComponent(dice, flat=flat, dtype=dtype, slot=op_id,
                            crit_doubles=bool(extra.get("crit_doubles", True))))
    return attack


def build_condition_context(resolved, gate_ctx, extras=None):
    """把构筑与回合状态整理成条件求值用的扁平上下文。"""
    ctx = dict(gate_ctx or {})
    ctx["attacker_state"] = ("raging" if ctx.get("rage_active") else None)
    if int(ctx.get("net_adv_tiers", 0)) > 0:
        ctx["attacker_state"] = "has_adv"     # 有净优势时覆盖为 has_adv
    ctx.setdefault("target_hp", "below_max")      # 打桩目标默认满血
    if extras:
        ctx.update(extras)
    return ctx


# ---------------------------------------------------------------- 触及与速度

def scaled_value(spec, op, build_doc):
    """字段值支持按**本职业等级**分段：`{"value": 10, "scale": {"6": 15}}`。

    一个特性只能有一个算子（检查 A10），所以等级成长不能靠多写几个算子，
    只能把分段表放在字段里。**保持原类型**——骰式（`1d6 → 3d8`）同样走这条路。
    """
    if not isinstance(spec, dict):
        return spec
    out = spec.get("value", 0)
    scale = spec.get("scale") or {}
    if not scale:
        return out
    acq = op.get("acquisition") or {}
    cid = acq.get("class_id")
    level = None
    for c in build_doc.get("classes", []):
        if c.get("id") == cid:
            level = int(c.get("level", 0))
    if level is None:
        return out
    for k, v in scale.items():
        if level >= int(k):
            out = v
    return out


def build_sneak_rules(resolved, build_doc, ops, attacks, distance_ft, env_doc):
    """偷袭：每回合一次，满足条件时加骰。

    条件有两条互斥路径（规则书「偷袭方式」），环境负责声明后者：

      优势        —— 该次攻击的净优势层数 ≥ 1
      友方夹击    —— `ally_within_5ft_of_target` 为真，且该次攻击没有劣势
      「决斗」    —— 目标 5 尺内没有别的生物，且没有劣势（需 2 级盗贼）

    **一次都没命中的回合不该白拿偷袭**，所以挂在命中结果树上而不是直接加到
    伤害里：第一击失手时，第二击仍然吃得到——这正是 `once_per_turn` 按
    分支记账、而不是钉死在某一击上的原因。
    """
    class_ops = {op["id"]: op for op in granted_class_ops(build_doc, ops)}
    op = class_ops.get("cf.盗贼.偷袭")
    if not op:
        return []
    spec = (op.get("projection") or {}).get("sneak_attack") or {}
    dice = scaled_value(spec.get("extra_dice", "0"), op, build_doc)
    if not dice or dice == "0":
        return []
    ally = bool(env_doc.get("ally_within_5ft_of_target", False))
    duel = "cf.盗贼.决斗" in class_ops and distance_ft <= 5
    rules = []
    for i, a in enumerate(attacks):
        ok = (a.adv_tiers >= 1
              or (ally and a.adv_tiers >= 0)
              or (duel and not ally and a.adv_tiers >= 0))
        if not ok:
            continue
        rules.append(Rule(
            trigger="hit_or_crit", on_attack=i, once_per_turn=True, name="盗贼.偷袭",
            extra_dice=[DamageComponent(dice, dtype=resolved.weapon["damage_type"],
                                        slot="sneak_attack")]))
    return rules


def _scale_dice(dice, n):
    """把 `1d10` 按层数放大成 `2d10`；纯加值写法原样返回。"""
    if n <= 0:
        return "0"
    head, sep, tail = str(dice).partition("d")
    if not sep or not head.isdigit():
        return dice
    return f"{int(head) * n}d{tail}"


def apply_adv_scaling(attack, active_ops, resolved):
    """按**最终**优势层数结算「N 重优势时……」类效果。

    必须在所有算子都作用完之后调用：优势层数会被狂暴、鲁莽攻击、条件效果叠加，
    早一步读到的层数是半成品。「死亡收割」在 2/3/4 重优势下分别 +2/+6/+19
    重击范围，读错层数就不是"差一点"，而是整个重击概率都错。

    两种写法：
      `per_tier`  每层线性（阵地狙击：每重优势 +1d10）
      `steps`     按档跳跃（死亡收割：{2: 2, 3: 6, 4: 19}），取不超过当前层数的最大档
    """
    tiers = max(0, attack.adv_tiers)
    seen = set()
    for op in active_ops:
        if op["id"] in seen:
            continue
        seen.add(op["id"])
        proj = op.get("projection") or {}
        spec = proj.get("scales_with_adv")
        if not spec:
            continue
        sel = proj.get("weapon_selector")
        if sel and not weapon_matches(resolved.weapon, sel):
            continue
        per = spec.get("per_tier") or {}
        steps = spec.get("steps") or {}
        if per:
            effect, times = per, tiers
        elif steps:
            applicable = [int(k) for k in steps if tiers >= int(k)]
            if not applicable:
                continue
            effect, times = steps[str(max(applicable))], 1
        else:
            raise ValueError(f"{op['name']} 的 scales_with_adv 既无 per_tier 也无 steps")
        for key in effect:
            if key not in ("crit_range", "extra_dice", "flat", "damage_type", "note"):
                raise ValueError(
                    f"{op['name']} 的 scales_with_adv 不支持字段 {key!r}"
                    f"（可用：crit_range / extra_dice / flat / damage_type）")
        if "crit_range" in effect:
            attack.crit_range += int(effect["crit_range"]) * times
        if "extra_dice" in effect or "flat" in effect:
            dtype = effect.get("damage_type") or resolved.weapon["damage_type"]
            attack.components.append(DamageComponent(
                _scale_dice(effect.get("extra_dice", "0"), times),
                flat=int(effect.get("flat", 0)) * times,
                dtype=dtype, slot=op["id"]))
    return attack


def collect_movement(active_ops, weapon, build_doc=None, armored=False):
    """把一批生效算子的触及/速度修正加起来。

    返回 `{"reach", "speed", "reach_notes", "speed_notes", "skipped"}`。
    `skipped` 是被武器门控挡下的条目——**必须报出来**，否则用户会以为
    「长度优势」给了 5 尺，其实他拿的是巨剑。

    武器门控（`weapon_selector`）必须在这里生效：「长度优势」只在长棍或
    触及武器上给 +5，拿巨剑时给它就会凭空多出 5 尺触及。
    """
    out = {"reach": 0, "speed": 0, "reach_notes": [], "speed_notes": [],
           "skipped": []}
    for op in active_ops:
        if (op.get("cost") or {}).get("passive_choice"):
            continue
        proj = op.get("projection") or {}
        sel = proj.get("weapon_selector")
        if sel and not weapon_matches(weapon, sel):
            if proj.get("reach_bonus_ft") or proj.get("speed_bonus_ft"):
                out["skipped"].append(
                    f"「{op['name']}」的触及/速度加成要求特定武器，"
                    f"当前武器是 {weapon.get('kind')}")
            continue
        if proj.get("requires_unarmored") and armored:
            out["skipped"].append(f"「{op['name']}」要求未着装盔甲与盾牌")
            continue
        rb = scaled_value(proj.get("reach_bonus_ft", 0), op, build_doc or {})
        if rb:
            out["reach"] += rb
            out["reach_notes"].append(f"{op['name']} +{rb}")
        sb = scaled_value(proj.get("speed_bonus_ft", 0), op, build_doc or {})
        if sb:
            out["speed"] += sb
            out["speed_notes"].append(f"{op['name']} +{sb}")
    return out


def weapon_matches(weapon, selector):
    """武器是否落在算子限定的范围内。

    `selector` 形如 `{"kinds": ["长棍"], "properties": ["触及"]}`，
    **满足其一即可**——规则写成「长棍或触及武器」时，两个列表并列就是或。
    只给一个列表时行为相同。空选择器视为不限。
    """
    kinds = selector.get("kinds") or []
    props = selector.get("properties") or []
    if not kinds and not props:
        return True
    if kinds and weapon.get("kind") in kinds:
        return True
    if props and set(weapon.get("properties") or []) & set(props):
        return True
    return False


# ---------------------------------------------------------------- 借机攻击

#: 借机攻击的触发源。**基础规则只认「离开触及」**——敌人走进你的触及不引发借机，
#: 施法、激活程序、攻击你、使用物件也都不引发，必须由专长逐项解锁。
OA_SOURCE_LABELS = {
    "retreat": "离开你的触及",
    "approach": "进入你的触及",
    "cast_spell": "施放法术",
    "activate_program": "激活程序",
    "attack_you": "攻击你",
    "use_object": "使用物件",
}


def opportunity_trigger_sources(resolved, ops):
    """构筑能够反应的触发源，以及每次触发最多能打出几击。

    返回 `(sources, per_trigger, drawbacks, skipped)`：`sources` 是 源 → 解锁者
    （默认只有 `retreat`），`per_trigger` 是单次触发允许的武器攻击次数
    （「连续借机」可打到 2），`drawbacks` 是 源 → 该触发附带的代价描述，
    `skipped` 是因武器不符而没接上的触发（必须报出来，不能静默丢弃）。
    """
    sources = {"retreat": "基础规则"}
    per_trigger = 1
    drawbacks, skipped = {}, []
    weapon_kind = resolved.weapon.get("kind")
    for f in resolved.raw.get("feats", []):
        op = ops.get(f.get("id"))
        if not op:
            continue
        proj = op.get("projection") or {}
        need = proj.get("opportunity_trigger_requires_weapon")
        for src in proj.get("opportunity_trigger_source") or []:
            if src not in OA_SOURCE_LABELS:
                raise KeyError(f"{op['name']} 的借机触发源 {src} 不认识"
                               f"（可用：{'/'.join(OA_SOURCE_LABELS)}）")
            if need and weapon_kind not in need:
                skipped.append(f"「{op['name']}」的{OA_SOURCE_LABELS[src]}借机"
                               f"需要 {'/'.join(need)}，当前武器是 {weapon_kind}")
                continue
            sources.setdefault(src, op["name"])
            if proj.get("opportunity_trigger_drawback"):
                drawbacks[src] = proj["opportunity_trigger_drawback"]
        per_trigger = max(per_trigger,
                          int(proj.get("opportunity_attack_per_trigger", 1)))
    return sources, per_trigger, drawbacks, skipped


def opportunity_attack_triggers(env_doc, sources):
    """环境声明的触发事件中，这个构筑实际能借机的部分。

    环境说的是**敌人做了什么**（施法几次、攻击你几次），至于能不能反应，
    取决于构筑有没有对应专长——没解锁的触发要单独报出来，
    否则会得到"环境说了 3 次、伤害没变"这种看不出原因的静默结果。
    """
    declared = dict(env_doc.get("enemy_triggers") or {})
    legacy = int(env_doc.get("enemy_triggers_per_round", 0))
    if legacy:
        declared["retreat"] = int(declared.get("retreat", 0)) + legacy
    total, used, blocked, used_src = 0, [], [], []
    for src, n in declared.items():
        if src not in OA_SOURCE_LABELS:
            raise KeyError(f"未知借机触发源 {src}"
                           f"（可用：{'/'.join(OA_SOURCE_LABELS)}）")
        n = int(n)
        if n <= 0:
            continue
        if src in sources:
            total += n
            used_src.append(src)
            used.append(f"{OA_SOURCE_LABELS[src]} {n} 次（{sources[src]}）")
        else:
            blocked.append(f"{OA_SOURCE_LABELS[src]} {n} 次（缺对应专长）")
    return total, used, blocked, used_src


def opportunity_attack_budget(resolved, ops):
    """每轮可发动的借机攻击次数。

    基础是 1 次反应；「层层设障」「坚守阵地」「连续借机」这类专长按属性调整值追加。
    **被动专长不需要策略引用**——选进构筑就生效。
    """
    total = 1
    sources = ["基础反应 1 次"]
    passive_choice = set()
    for f in resolved.raw.get("feats", []):
        op = ops.get(f.get("id"))
        if not op:
            continue
        if (op.get("cost") or {}).get("passive_choice"):
            passive_choice.add(op["id"])
            continue
        spec = (op.get("projection") or {}).get("opportunity_attack_budget_add")
        if not spec:
            continue
        if "ability" in spec:
            n = max(0, resolved.derived.mods.get(spec["ability"], 0))
        else:
            n = int(spec.get("value", 0))
        if n:
            total += n
            sources.append(f"{op['name']} +{n}")
    return total, sources


def build_base_attack(resolved, adv_tiers=0, rage_active=False, weapon_dice=None):
    """武器基础攻击：武器骰 + 属性调整值 + 武器增强 + 狂暴加值。"""
    d = resolved.derived
    flat = d.mods["str"] + resolved.weapon["enhancement"]
    components = [DamageComponent(weapon_dice or resolved.weapon["dice"], flat=flat,
                                  dtype=resolved.weapon["damage_type"],
                                  slot="weapon+str+enhancement")]
    if rage_active and resolved.rage_bonus:
        components.append(DamageComponent("0", flat=resolved.rage_bonus,
                                          dtype=resolved.weapon["damage_type"],
                                          slot="class.rage"))
    return Attack(attack_bonus=d.attack.total, adv_tiers=adv_tiers,
                  components=components)


def load_damage_tables(path=None):
    """读取结构化伤害表。"""
    path = path or os.path.join(REPO, "形式化", "data", "damage_tables.jsonl")
    tables = {}
    if not os.path.isfile(path):
        return tables
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                t = json.loads(line)
                tables[t["id"]] = t
    return tables


# 构筑侧统一用英文尺寸键（与「巨大武器师」的覆盖表一致），
# 伤害表的人读键是中文，查表时做一次映射。
SIZE_CN = {"tiny": "微型", "small": "小型", "medium": "中型",
           "large": "大型", "huge": "巨型", "gargantuan": "超巨型"}


def resolve_damage_table(table, resolved, ctx=None):
    """按轴取值从表中查出骰式。支持两类表：

    - 交叉表：axes.row + axes.column，如「体型 × 材质」
    - 分档表：bands，按数值区间取，如「按临时生命值」
    """
    ctx = ctx or {}
    if "bands" in table:
        key = table["axes"]["row"]
        value = int(ctx.get(key, 0))
        for band in table["bands"]:
            hi = band["max"]
            if value >= band["min"] and (hi is None or value <= hi):
                return band["dice"]
        raise ValueError(f"{table['id']}: 值 {value} 落在所有分档之外")

    row_key = table["axes"]["row"]
    col_key = table["axes"]["column"]
    row_val = ctx.get(row_key) or resolved.size
    if row_key == "size":
        row_val = SIZE_CN.get(row_val, row_val)
    col_val = ctx.get(col_key) or resolved.raw.get("combat", {}).get("material", "血肉")
    row = table["cells"].get(row_val)
    if row is None:
        raise ValueError(f"{table['id']}: 未知的 {row_key}={row_val!r}")
    dice = row.get(col_val)
    if dice is None:
        raise ValueError(f"{table['id']}: 未知的 {col_key}={col_val!r}")
    return dice


def apply_op(op, attack, resolved, attack_index=0, condition_ctx=None):
    if not _TABLES_CACHE:
        _TABLES_CACHE.update(load_damage_tables())
    condition_ctx = condition_ctx or {}
    """把一个算子作用到攻击上。返回 (attack, 额外的规则列表)。"""
    proj = op.get("projection", {})
    rules = []

    if "attack_bonus" in proj:
        attack.attack_bonus += int(proj["attack_bonus"])

    if "adv_tiers" in proj:
        attack.adv_tiers += int(proj["adv_tiers"])

    if "crit_range" in proj:
        attack.crit_range += int(proj["crit_range"])

    # 整次攻击不可减伤（「力劈华山」：伤害无法被任何方式减少、免疫或转移）
    # 对抗检定：战技要求"胜出"才生效（如泰山压顶的推撞），按胜率加权
    contest = proj.get("requires_contest")
    if contest:
        spec = contest.get("attacker") or {}
        ability = spec.get("ability", "str")
        prof = resolved.derived.prof if spec.get("skill") else 0
        atk_bonus = resolved.derived.mods.get(ability, 0) + prof
        dice = None
        if spec.get("add_superiority"):
            faces = superiority_faces(resolved.derived.level)
            dice = (spec.get("superiority_count", 1), faces)
        defenders = contest.get("defender_bonus")
        if defenders is None:
            defenders = 0
        win = opposed_win_probability(atk_bonus, defenders, dice, adv_tiers=0)
        _CONTEST_FACTOR = win
        # 对抗检定型战技（如推撞）**没有攻击检定**，成败只由对抗决定。
        # 若不关闭命中掷骰，会额外乘一次命中率——双重门控，结果系统性偏低。
        attack.auto_hit = True
    else:
        _CONTEST_FACTOR = None

    if proj.get("attack_irreducible"):
        for comp in attack.components:
            comp.irreducible = True

    for slot in proj.get("damage_slots", []):
        if slot.get("applies_on", "on_hit") not in ("on_hit", "always"):
            continue
        value = _eval_expr(slot["expr"], resolved)
        cap = slot.get("cap")
        for step in slot.get("cap_scaling", []):
            if resolved.derived.level >= step["level"]:
                cap = step["cap"]
        if cap is not None:
            value = min(value, int(cap))
        dtype = slot.get("type") or resolved.weapon["damage_type"]
        if dtype == "same_as_weapon":
            dtype = resolved.weapon["damage_type"]
        attack.components.append(
            DamageComponent("0", flat=value, dtype=dtype, slot=op["id"],
                            irreducible=bool(slot.get("irreducible", False))))

    # 伤害整体被替换：查表或公式（如泰山压顶按体型材质、苍鹰击于殿按速度）
    if proj.get("damage_replaces_weapon"):
        spec = proj.get("damage_table") or proj.get("formula_damage") or {}
        dtype = spec.get("type") or resolved.weapon["damage_type"]
        dice = "0"
        flat = 0
        if "table" in spec:
            tbl = _TABLES_CACHE.get(spec["table"])
            if tbl is None:
                raise KeyError(f"未知伤害表: {spec['table']}")
            dice = resolve_damage_table(tbl, resolved,
                                        {"temporary_hp": spec.get("temporary_hp", 0)})
        elif "expr" in spec:
            flat = eval_formula(spec["expr"], build_formula_context(resolved))
        cap = spec.get("cap")
        if cap is not None and flat:
            flat = min(flat, int(cap))
        # 表里可能返回 dS 形式（如「长城」按临时生命值给若干枚卓越骰）
        dice = resolve_superiority_dice(dice, resolved.derived.level)
        attack.components = [DamageComponent(dice, flat=flat, dtype=dtype,
                                             slot=op["id"],
                                             weight=_CONTEST_FACTOR)]

    # 通用条件：when 满足时施加 then（终结之舞按目标生命值加骰、巨人技艺模仿等）
    for rule in proj.get("conditions", []):
        if eval_condition(rule.get("when") or {}, condition_ctx):
            apply_condition_effects(attack, rule.get("then") or {}, resolved, op["id"])

    # 本轮所有攻击均命中后追加一次（刺剑大师的精准连刺）
    all_hit = proj.get("on_all_hit_extra_attack")
    if all_hit:
        rules.append(Rule(trigger="hit_or_crit",
                          extra_attack_all_hit={"count": int(all_hit.get("count", 1)),
                                                "attack_bonus_mod": int(all_hit.get("attack_bonus_mod", 0))},
                          name=op["id"]))

    # 命中后追加一次攻击（疯狂连击、巨武器大师的旋风顺劈等）
    follow = proj.get("on_hit_extra_attack")
    if follow:
        rules.append(Rule(trigger="hit_or_crit",
                          on_attack=follow.get("after_attack"),
                          extra_attack={"count": int(follow.get("count", 1)),
                                        "attack_bonus_mod": int(follow.get("attack_bonus_mod", 0))},
                          name=op["id"]))

    for extra in proj.get("extra_dice", []):
        dice = extra["dice"]
        for step in extra.get("dice_scaling", []):
            if resolved.derived.level >= step["level"]:
                dice = step["dice"]
        # 类型键有两种写法：`type` 与 `damage_type`。早期只读 `type`，
        # 导致「穿刺者」「巨人打击」写的 damage_type 被静默忽略、
        # 回落到武器类型——穿刺者的 +2 实际按挥砍结算。
        dtype = extra.get("type") or extra.get("damage_type") or resolved.weapon["damage_type"]
        if dtype == "same_as_weapon":
            dtype = resolved.weapon["damage_type"]
        flat = int(extra.get("flat", 0))
        if extra.get("flat_by_size"):        # 按自身体型给固定加值（泰坦弓手）
            flat += int(extra["flat_by_size"].get(resolved.size, 0))
        if extra.get("flat_expr"):          # 如「狂战士等级的一半」
            flat = eval_level_expr(extra["flat_expr"], resolved.derived.level)
        dice = resolve_superiority_dice(dice, resolved.derived.level)
        comp = DamageComponent(dice, flat=flat, dtype=dtype,
                               slot=extra.get("slot") or op["id"],
                               crit_doubles=bool(extra.get("crit_doubles", True)),
                               irreducible=bool(extra.get("irreducible", False)))
        if extra.get("limit") == "每回合一次":
            # 每回合一次：只有本回合第一次攻击享有，其余攻击靠结果树分支处理
            rules.append(Rule(trigger="hit_or_crit", on_attack=attack_index,
                              extra_dice=[comp], name=op["id"]))
        else:
            attack.components.append(comp)
    return attack, rules


# ---------------------------------------------------------------- 主计算

# ---------------------------------------------------------------- 法术

def _split_dice(expr):
    """'8d6' → (8, 6)；'3' → (0, 0, 3)。"""
    s = str(expr).strip().lower()
    if "d" in s:
        n, _, faces = s.partition("d")
        return int(n or 1), int(faces)
    return 0, 0


def _add_dice(base, extra, times=1):
    """把 extra 叠加 times 次到 base 上。'8d6' + '2d6'×2 → '12d6'。"""
    n1, f1 = _split_dice(base)
    n2, f2 = _split_dice(extra)
    if f1 and f2 and f1 != f2:
        raise ValueError(f"骰面不一致，无法合并: {base} + {extra}")
    faces = f1 or f2
    return f"{n1 + n2 * times}d{faces}"


def spell_dice(op, char_level, slot_level=None):
    """按角色等级应用 scaling，按法术位应用 upcast，返回最终骰式。

    scaling 一律写在算子**顶层**。早期把攻击型法术的 scaling 写进了
    `attack` 内，导致等级成长静默失效（5 级仍算 1d8），现统一到顶层，
    并在此对嵌套位置做一次兼容读取，避免旧数据静默出错。
    """
    scaling = op.get("scaling") or (op.get("attack") or {}).get("scaling") or []
    if "attack" in op:
        base = op["attack"]["damage"]["dice"]
    else:
        base = op["damage"]["dice"]

    for step in scaling:
        if char_level >= step["level"]:
            base = step["dice"]

    # 升环有两种形态：加骰（火球术）与加实例（灼热射线加射线、魔法飞弹加枚数）。
    # 后者不改骰式，因此这里必须判断 add_dice 是否存在。
    upcast = op.get("upcast")
    if (upcast and slot_level and "add_dice" in upcast
            and slot_level > upcast["per_slot_above"]):
        base = _add_dice(base, upcast["add_dice"],
                         slot_level - upcast["per_slot_above"])
    return base


def spell_attack_instances(op, slot_level=None):
    """攻击实例数，含升环增加的射线（灼热射线每高一环 +1 道）。

    魔法飞弹的枚数按法术位查表（2 环 5 枚、3 环 7 枚……），不是线性增长，
    因此额外支持 count_by_slot。
    """
    attack = op.get("attack") or {}
    count = int(attack.get("count", 1))
    by_slot = attack.get("count_by_slot")
    if slot_level and by_slot:
        return int(by_slot.get(str(slot_level), count))
    up = op.get("upcast") or {}
    if slot_level and up.get("add_count_per_slot"):
        count += (slot_level - up["per_slot_above"]) * up["add_count_per_slot"]
    return count


def pick_damage_type(choices, target):
    """在可选伤害类型中挑对目标最有利的一种。

    「繁彩球」「能量射线」这类法术由施法者自选类型，打桩下应对着目标的
    抗性/免疫来选——取第一个出现的类型会系统性低估。
    """
    def score(t):
        if t in target.absorb:
            return 3          # 反而给目标回血，最优
        if t in target.immune:
            return 0
        if t in target.vuln:
            return 3
        if t in target.resist:
            return 1
        return 2
    return max(choices, key=score) if choices else None


def build_spell_attack(resolved, op, adv_tiers=0, slot_level=None, target=None):
    """攻击型法术（如冻寒之触）：法术攻击加值 + 法术伤害骰。"""
    if resolved.derived.spell_attack is None:
        raise ValueError(f"构筑未声明 casting_ability，无法施放 {op['id']}")
    spec = op["attack"]
    dice = spell_dice(op, resolved.derived.level, slot_level)
    dtype = spec["damage"]["damage_type"]
    if spec["damage"].get("type_choice"):
        if target is not None:
            dtype = pick_damage_type(spec["damage"]["type_choice"], target)
    comp = DamageComponent(dice, flat=0, dtype=dtype, slot=op["id"])
    return Attack(attack_bonus=resolved.derived.spell_attack.total,
                  adv_tiers=adv_tiers, components=[comp],
                  auto_hit=bool(spec.get("auto_hit", False)))


def spell_attack_count(op):
    """法术产生的攻击实例数（灼热射线为 3 道、升环再加）。"""
    return int((op.get("attack") or {}).get("count", 1))


def save_effect_from_op(resolved, op, target, slot_level=None,
                        dc_modifier=0, on_fail_extra=None):
    """豁免型法术的期望伤害。DC 取自角色，豁免加值取自目标。

    dc_modifier / on_fail_extra 供「威力豁免」这类修正专长使用：
    承受 -5 DC，换取豁免失败时额外伤害。
    """
    if resolved.derived.spell_dc is None:
        raise ValueError(f"构筑未声明 casting_ability，无法施放 {op['id']}")
    spec = op.get("save") or {}
    ability = spec.get("ability", "dex")
    dice = spell_dice(op, resolved.derived.level, slot_level)
    amount = dice_mean(dice)
    half = bool(op.get("damage", {}).get("half_on_save", False))
    save_bonus = int(getattr(target, "saves", {}).get(ability, 0))
    dc = resolved.derived.spell_dc + int(dc_modifier)

    exp = expected_save_damage(dc, save_bonus, amount, half_on_save=half)

    if on_fail_extra:
        p_fail = save_fail_probability(dc, save_bonus)
        extra = _eval_expr(on_fail_extra["expr"],
                           _MaxDieProxy(dice))
        cap = on_fail_extra.get("cap")
        for step in on_fail_extra.get("cap_scaling", []):
            if resolved.derived.level >= step["level"]:
                cap = step["cap"]
        if cap is not None:
            extra = min(extra, int(cap))
        exp += p_fail * extra

    dtype = op["damage"]["damage_type"]
    if op["damage"].get("type_choice"):
        dtype = pick_damage_type(op["damage"]["type_choice"], target)
    dmg, _ = apply_target_pipeline(exp, dtype, target)
    return dmg, dice


class _MaxDieProxy:
    """让 _eval_expr 能取到"该法术基础伤害骰的最大值"。"""

    def __init__(self, dice_expr):
        self.weapon = {"dice": dice_expr}


@dataclass
class Result:
    per_round: list
    total: Fraction
    leaves: int
    trace: list
    spend_report: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def derive_feat_resources(build_doc, ops, attack_count):
    """从专长的 `uses` 声明推导资源池。

    例：「刺剑大师」的精准连刺每长休 1 次，拥有「额外攻击」后 3 次，
    拥有「精通额外攻击」后无限。这类"用几次"的限制和卓越骰一样是资源，
    必须进预算，否则策略可以无限制地使用。

    条件的表达用 `has_attack_count_gte`——因为"额外攻击/精通额外攻击"本身
    就是攻击次数的门槛，用它做判据比再引一层特性名匹配更直接。
    """
    pools = {}
    for f in build_doc.get("feats", []):
        op = ops.get(f.get("id"))
        uses = (op.get("projection") or {}).get("uses") if op else None
        if not uses:
            continue
        cap = uses.get("capacity", 1)
        unlimited = False
        for rule in uses.get("capacity_if", []):
            if attack_count >= int(rule.get("has_attack_count_gte", 99)):
                cap = rule.get("capacity")
                unlimited = cap is None
                break
        pid = uses.get("pool") or f"uses:{op['id']}"
        pools[pid] = ResourcePool(
            id=pid, name=op["name"],
            capacity=10 ** 6 if unlimited else int(cap),
            recharge=uses.get("recharge", "long_rest"),
            unlimited_from_level=uses.get("unlimited_from_level"))
    return pools


def load_budget(build_doc, env_doc, extra_pools=None):
    """从构筑的 resources 声明 + 环境的短休次数构造资源预算。"""
    pools = dict(extra_pools or {})
    for pid, spec in (build_doc.get("resources") or {}).items():
        pools[pid] = ResourcePool(
            id=pid,
            name=spec.get("name", pid),
            capacity=int(spec.get("capacity", 0)),
            recharge=spec.get("recharge", "long_rest"),
            interval=int(spec.get("interval", 1)),
            die_faces=spec.get("die_faces"),
            unlimited_from_level=spec.get("unlimited_from_level"),
            derived_from=tuple(spec["derived_from"]) if spec.get("derived_from") else None,
            per_circle=spec.get("per_circle"),
        )
    level = int(build_doc.get("meta", {}).get("level", 1))
    return Budget(pools=pools, short_rests=int(env_doc.get("short_rests", 0)),
                  long_rests=int(env_doc.get("long_rests", 1)), level=level)


def _has_feat(build_doc, op_id):
    return any(f.get("id") == op_id for f in build_doc.get("feats", []))


def apply_build_time_effects(build_doc, ops, trace=None):
    """把"构筑期"生效的算子投影落到构筑上。

    有些专长的效果是改属性（如「凶蛮打手」力量或体质 +1），它发生在**算派生值之前**，
    不属于战斗回合。早先引擎只处理战斗期投影，导致这类专长的属性加值完全不生效——
    数值上缺失但不会报错。这里在 derive 之前统一结算。
    """
    doc = json.loads(json.dumps(build_doc))       # 深拷贝，不改调用方的对象
    scores = doc.setdefault("scores", {})
    feat_scores = dict(scores.get("feat") or {})
    choices = doc.get("choices") or {}

    missing_warnings = []
    for f in doc.get("feats", []):
        op = ops.get(f.get("id"))
        bonus = (op.get("projection") or {}).get("ability_bonus") if op else None
        if not bonus:
            continue
        # 属性加值有两种写法：
        #   str_or_con: 1                     —— 固定的一组候选（历史写法）
        #   choose_one_of: [str, dex], amount —— 通用写法
        amount = None
        candidates = None
        if "str_or_con" in bonus:
            candidates, amount = ("str", "con"), int(bonus["str_or_con"])
        elif "choose_one_of" in bonus:
            candidates, amount = tuple(bonus["choose_one_of"]), int(bonus.get("amount", 1))
        if candidates:
            # choices.ability_bonus 可以是字符串（对所有加值专长生效），
            # 也可以是 {算子 id: 属性} 的映射——同一构筑可能同时有
            # 「挥砍者(str/dex)」与「敏锐心灵(int/wis)」，单一字段表达不了。
            raw = choices.get("ability_bonus")
            if isinstance(raw, dict):
                target = raw.get(op["id"])
            else:
                target = raw
            if target is None:
                target = candidates[0]
            if target not in candidates:
                # 抛错会让"构筑里有一条参数不合的专长"整体不可算，
                # 改为跳过并告警：其余效果照常结算
                missing_warnings.append(
                    f"{op['name']} 需要的属性加值候选为 "
                    f"{'/'.join(candidates)}，构筑选择的是 {target!r}，该加值跳过")
                continue
            feat_scores[target] = feat_scores.get(target, 0) + amount
            if trace is not None:
                trace.append(f"构筑期：{op['name']} {target.upper()} +{amount}")

    scores["feat"] = feat_scores
    if trace is not None:
        trace.extend(missing_warnings)
    return doc


# 策略里可用的短名 → 正式算子 id。保留短名是为了让策略可读，
# 但解析后一律落到算子 id，避免"魔法字符串"散落在各处。
BUFF_ALIASES = {
    "rage": "cf.狂战士.狂暴",
    "reckless": "cf.狂战士.鲁莽攻击",
}


def _build_level(build_doc):
    meta = build_doc.get("meta", {})
    if meta.get("level"):
        return int(meta["level"])
    return sum(int(c.get("level", 0)) for c in build_doc.get("classes", [])) or 1


def granted_class_ops(build_doc, ops):
    """构筑在给定等级下**自动获得**的职业特性算子。

    这是动作经济与算子数据之间的接缝：构筑只写"职业 + 等级"，
    能发动几次攻击由职业特性算子决定，不由策略里写死的数字决定。
    """
    # 必须用**该职业自己的等级**判断，而不是总等级。
    # 早期用总等级会让 6 级狂战士拿到 11 级的「精通额外攻击」——
    # 单人职业时看不出来，兼职构筑会全错。
    class_levels = {c.get("id"): int(c.get("level", 0))
                    for c in build_doc.get("classes", [])}
    granted = []
    for op in ops.values():
        acq = op.get("acquisition") or {}
        if acq.get("type") != "class_level":
            continue
        cid = acq.get("class_id")
        if cid not in class_levels:
            continue
        if int(acq.get("level", 99)) > class_levels[cid]:
            continue
        granted.append(op)
    return granted


def granted_feat_ops(build_doc, ops):
    """构筑里已选专长对应的算子。

    被动专长（改武器骰、给附赠动作攻击、加优势）应当因为"在构筑里"就自动生效，
    不需要策略再显式引用——早期的做法会让玩家漏写而少算伤害。
    需要主动宣告的专长（如威力攻击）仍由策略的 per_attack.ops 显式引用。
    """
    out = []
    for f in build_doc.get("feats", []):
        op = ops.get(f.get("id"))
        if not op:
            continue
        # `passive_choice` 的专长（威力攻击、疯狂连击）需要**每次攻击自主决定**
        # 是否使用，不能因为选进了构筑就无条件生效——那会给每一击都挂上 -5。
        if (op.get("cost") or {}).get("passive_choice"):
            continue
        out.append(op)
    return out


def multiclass_level_sum(build_doc, classes):
    """指定职业的等级总和（用于「融汇百家」这类按多职业等级判定的规则）。"""
    total = 0
    for c in build_doc.get("classes", []):
        if c.get("id") in classes:
            total += int(c.get("level", 0))
    return total


def derive_attack_count(build_doc, ops):
    """攻击动作内可发动的攻击次数：取所有已获得特性中的最大值。

    除了职业特性直接给出的次数，还处理「元规则」——像「融汇百家」那样
    按多职业等级总和**授予**其他职业的特性。这类规则改的不是数值而是
    "你能不能获得某个特性"，因此必须在获取阶段结算，不能当作伤害投影。
    """
    counts = [(1, "基础")]
    for op in granted_class_ops(build_doc, ops):
        proj = op.get("projection") or {}
        if "attack_count" in proj:
            counts.append((int(proj["attack_count"]), op["name"]))

    for f in build_doc.get("feats", []):
        op = ops.get(f.get("id"))
        grants = (op.get("projection") or {}).get("grants_attack_count") if op else None
        if not grants:
            continue
        level_sum = multiclass_level_sum(build_doc, grants.get("classes", []))
        for rule in grants.get("tiers", []):
            if level_sum >= int(rule["min_level_sum"]):
                counts.append((int(rule["count"]), f"{op['name']}（职业等级和 {level_sum}）"))

    value, src = max(counts, key=lambda x: x[0])
    return value, src


def unmet_gates(op, ctx):
    """检查算子声明的 gate 是否满足。

    早先引擎只把 buffs 当作增益开关，从不校验算子自己的 gate——于是可以在
    没开狂暴时使用要求狂暴的算子而不报错。
    """
    gate = op.get("gate") or {}
    missing = []
    if gate.get("requires_rage") and not ctx.get("rage_active"):
        missing.append("狂暴")
    if gate.get("mounted") and not ctx.get("mounted"):
        missing.append("骑乘")
    return missing


def _with_vulnerability(target, types):
    """返回一个"额外具有若干易伤类型"的目标副本，不修改原对象。"""
    import copy
    t = copy.copy(target)
    t.vuln = tuple(set(target.vuln) | set(types))
    return t


RANGE_RE = re.compile(r"(\d+)\s*尺")
_TABLES_CACHE = {}


def _range_ft(text):
    """施法距离。'120尺' → 120；'自身' → None（无需距离判定）。

    注意"自身（半径10尺）"这类写法里的 10 尺是**区域半径**，不是施法距离。
    早期实现直接取第一个"数字+尺"，把区域法术误判成 10 尺射程，
    30 尺外就一律判为超出距离而记 0 伤害。
    """
    s = str(text or "")
    if "自身" in s:
        return None
    m = RANGE_RE.search(s)
    return int(m.group(1)) if m else None


def _save_effect_damage(effect, target, resolved):
    """豁免型效果的单次期望伤害。"""
    dc = int(effect["dc"])
    save_bonus = int(getattr(target, "saves", {}).get(effect.get("save", "dex"), 0))
    amount = dice_mean(effect["dice"])
    half = bool(effect.get("half_on_save", False))
    exp = expected_save_damage(dc, save_bonus, amount, half_on_save=half)
    dtype = effect.get("damage_type", "force")
    dmg, _ = apply_target_pipeline(exp, dtype, target)
    return dmg


def evaluate(build_doc, env_doc, strategy_doc, ops=None, rage_table=None):
    """三份输入 → 期望伤害。"""
    ops = ops or load_ops()
    if rage_table is None:
        rage_table = load_rage_table()

    build_trace = []
    build_doc = apply_build_time_effects(build_doc, ops, build_trace)
    resolved = resolve_build(build_doc, rage_table)

    target_doc = env_doc.get("target", {})
    target = TargetProfile(
        ac=int(target_doc.get("ac", 10)),
        resist=tuple(target_doc.get("resist", ())),
        vuln=tuple(target_doc.get("vuln", ())),
        immune=tuple(target_doc.get("immune", ())),
        absorb=tuple(target_doc.get("absorb", ())),
        pre_reduction=target_doc.get("pre_reduction", {}),
        post_reduction=target_doc.get("post_reduction", {}),
    )
    # 豁免加值挂在目标上，供豁免型效果使用
    target.saves = dict(target_doc.get("saves", {}))

    combat = build_doc.get("combat", {})
    weapon_reach = int(combat.get("weapon", {}).get("reach_ft", 5))
    weapon_range = combat.get("weapon", {}).get("range_ft")

    # 触及/速度：构筑里的专长与职业特性可以改这两项（方案 §4.2 的 F12 判据）。
    # 两者都不直接产生伤害，但决定**这一轮够不够得着**——够不着就没有攻击，
    # 速度富余则能退得更远、下一轮再冲回来。
    weapon_doc = combat.get("weapon", {})
    # 「无甲防御」类取值（barbarian_unarmored / monk_unarmored）不是着甲，
    # 早先按"只要不是 none 就算着甲"处理，会让武僧/狂战士的无甲移动永远不生效
    armor_doc = str(combat.get("armor") or "none").lower()
    armored = not (armor_doc in ("", "none", "unarmored") or "unarmored" in armor_doc) \
        or bool(combat.get("shield"))
    granted_ops = granted_feat_ops(build_doc, ops) + granted_class_ops(build_doc, ops)
    reach_move = collect_movement(granted_ops, weapon_doc, build_doc, armored)
    weapon_reach += reach_move["reach"]
    reach_notes = reach_move["reach_notes"]
    range_profile = RangeProfile(reach_ft=weapon_reach, range_ft=weapon_range)

    # 骑乘时移动力来自坐骑（方案 §2.3）。坐骑速度取自其资料表：
    # 矮马 50 尺、挽马 60 尺、乘用马 75 尺。
    mount = combat.get("mount") or {}
    speed_base = int(mount.get("speed_ft") or combat.get("speed_ft", 30))
    speed_bonus = reach_move["speed"]
    speed_notes = reach_move["speed_notes"]
    speed_ft = speed_base + speed_bonus
    state = CombatState(distance_ft=int(env_doc.get("initial_distance_ft", 30)),
                        speed_ft=speed_ft)
    if mount:
        trace_speed = f"{speed_ft} 尺（坐骑：{mount.get('name', '未命名')}）"
    else:
        trace_speed = f"{speed_ft} 尺"
    if speed_notes:
        trace_speed += f"（含 {'、'.join(speed_notes)}）"
    derived_attacks, attack_src = derive_attack_count(build_doc, ops)

    feat_pools = derive_feat_resources(build_doc, ops, derived_attacks)
    budget = load_budget(build_doc, env_doc, feat_pools)

    trace = build_trace + list(resolved.trace) + [
        f"目标 AC {target.ac}",
        f"初始距离 {state.distance_ft} 尺｜速度 {trace_speed}"
        f"｜触及 {weapon_reach} 尺"
        + (f"（含 {'、'.join(reach_notes)}）" if reach_notes else "")
        + (f"｜射程 {weapon_range} 尺" if weapon_range else ""),
    ]
    trace.append(f"攻击次数由职业特性决定：{derived_attacks}（{attack_src}）")

    warnings = []
    per_round = []
    spends = []
    concentration_active = None      # 当前维持专注的法术名
    sustained_op = None              # 维持中的专注算子（用于逐轮结算）

    for rnd in strategy_doc.get("rounds", []):
        rnum = rnd.get("round", len(per_round) + 1)
        buffs = {BUFF_ALIASES.get(b, b) for b in rnd.get("buffs", [])}

        # 增益类职业特性算子：按 buffs 启用，其投影直接作用于本轮攻击
        # 增益算子的投影一律通用处理，不按具体算子 id 分支——
        # 早先写死了 "cf.狂战士.狂暴"，换个职业就得改引擎。
        buff_adv = 0
        rage_active = False
        for bid in buffs:
            bop = ops.get(bid)
            if not bop:
                warnings.append(f"第 {rnum} 轮未知增益 {bid}")
                continue
            proj = bop.get("projection") or {}
            if "adv_tiers" in proj:
                buff_adv += int(proj["adv_tiers"])
                trace.append(f"第 {rnum} 轮 {bop['name']}：+{proj['adv_tiers']} 重优势")
            if "rage" in proj:
                rage_active = True

        # 被动算子 = 构筑已选专长 + 本轮增益；被动专长应自动生效，无需策略引用
        passive_ops = granted_feat_ops(build_doc, ops) + [
            ops[b] for b in buffs if b in ops]

        # 资源消耗
        for pid, amount in (rnd.get("spends") or {}).items():
            spends.append((rnum, pid, int(amount)))

        # 维持中的专注效果：在施放之后的回合继续结算
        sustained_dmg = Fraction(0)
        if sustained_op is not None and not rnd.get("end_concentration"):
            eff = sustained_op.get("concentration_effect") or {}
            if eff.get("damage"):
                spec = dict(sustained_op)
                spec["damage"] = eff["damage"]
                if "save" in eff:
                    spec["save"] = eff["save"]
                sd, dice = save_effect_from_op(resolved, spec, target,
                                               eff.get("slot_level"))
                sustained_dmg += sd
                trace.append(f"第 {rnum} 轮 维持 {sustained_op['name']}（{dice}）→ {float(sd):.3f}")

        if rnd.get("end_concentration"):
            if concentration_active:
                trace.append(f"第 {rnum} 轮 结束专注：{concentration_active}")
            concentration_active, sustained_op = None, None

        # 每轮重置移动力与槽位。速度**逐轮重算**——狂暴、点燃这类增益只在
        # 生效期间给移速，写在构筑层会让没开 buff 的回合也快跑。
        move_ops = granted_ops + [ops[b] for b in buffs if b in ops]
        round_move = collect_movement(move_ops, weapon_doc, build_doc, armored)
        round_speed = speed_base + round_move["speed"]
        round_speed_notes = round_move["speed_notes"]
        for note in round_move["skipped"]:
            if note not in warnings:
                warnings.append(note)
        if per_round:
            state.next_round(round_speed)
        else:
            state.start_turn(round_speed)
        if round_speed_notes:
            trace.append(f"第 {rnum} 轮速度 {round_speed} 尺"
                         f"（含 {'、'.join(round_speed_notes)}）")

        mv = rnd.get("move")
        if mv:
            moved = state.move(int(mv.get("ft", 0)), toward=bool(mv.get("toward", True)))
            trace.append(f"第 {rnum} 轮移动 {moved} 尺 → 距离 {state.distance_ft} 尺")

        per_attack = rnd.get("per_attack", {})
        op_ids = per_attack.get("ops", [])
        adv = int(per_attack.get("adv_tiers", 0)) + buff_adv

        kind = per_attack.get("kind", "weapon")
        spell_id = per_attack.get("spell")
        slot_level = per_attack.get("slot_level")

        # 距离判定要用**本动作自己的**射程：武器用触及/射程，法术用该法术的施法距离
        if kind == "spell_attack" and spell_id in ops:
            spell_range = _range_ft(ops[spell_id].get("range"))
            profile = RangeProfile(reach_ft=10 ** 6, range_ft=spell_range)
        else:
            profile = range_profile
        in_range = profile.can_reach(state.distance_ft)

        # 攻击次数：未声明时由职业特性推导；声明值超过推导值则告警
        if rnd.get("attacks") is None:
            attacks_n = derived_attacks
        else:
            attacks_n = int(rnd["attacks"])
            if attacks_n > derived_attacks:
                warnings.append(
                    f"第 {rnum} 轮策略声明 {attacks_n} 次攻击，"
                    f"但职业特性只允许 {derived_attacks} 次（{attack_src}），按声明值计算")
        if attacks_n and not in_range:
            warnings.append(f"第 {rnum} 轮距离 {state.distance_ft} 尺超出触及/射程，本回合无攻击")
            attacks_n = 0

        # 复合动作（如骑乘冲刺）：追加前置条件、覆盖攻击次数、附加优势
        round_target = target
        action_id = rnd.get("action")
        if action_id:
            if action_id not in ops:
                raise KeyError(f"未知动作算子: {action_id}")
            comp = ops[action_id].get("composite") or {}
            move_spec = comp.get("move") or {}
            min_travel = int(move_spec.get("min_travel_ft", 0))
            if min_travel and state.travel_this_turn < min_travel:
                warnings.append(
                    f"第 {rnum} 轮 {ops[action_id]['name']} 要求本回合直线移动 ≥{min_travel} 尺，"
                    f"实际 {state.travel_this_turn} 尺，未生效")
                attacks_n = 0
            # 冲刺需要足够的空间：跑不出 30 尺就冲不起来
            start_distance = state.distance_ft + state.travel_this_turn
            if min_travel and attacks_n and start_distance < min_travel:
                warnings.append(
                    f"第 {rnum} 轮起点距目标仅 {start_distance} 尺，"
                    f"不足以完成 ≥{min_travel} 尺的直线冲刺，未生效")
                attacks_n = 0
            atk_spec = comp.get("attack") or {}
            if attacks_n:
                cap_n = int(atk_spec.get("count", attacks_n))
                if attacks_n > cap_n:
                    trace.append(f"第 {rnum} 轮 {ops[action_id]['name']} 覆盖额外攻击："
                                 f"{attacks_n} 次 → {cap_n} 次")
                    attacks_n = cap_n
                adv += int(atk_spec.get("adv_tiers", 0))
                trace.append(f"第 {rnum} 轮 {ops[action_id]['name']}：+{atk_spec.get('adv_tiers', 0)} 重优势")

            # 命中时给目标附加易伤：由算子声明 trigger，引擎按 trigger 生效，
            # 不写死具体算子 id（原先写死 "feat.骑乘战斗"，加同类专长就得改引擎）。
            for fid in (f.get("id") for f in build_doc.get("feats", [])):
                fop = ops.get(fid)
                if not fop:
                    continue
                vuln_spec = (fop.get("projection") or {}).get("on_hit_target_vuln")
                if not vuln_spec or vuln_spec.get("trigger") != action_id:
                    continue
                round_target = _with_vulnerability(target, vuln_spec["types"])
                trace.append(f"第 {rnum} 轮 {fop['name']}：目标对 "
                             f"{'/'.join(vuln_spec['types'])} 易伤")

        # 条件策略：按命中/重击结果分支（触发时才算消耗）
        all_rules = []
        for cond in rnd.get("contingencies", []):
            extras = [DamageComponent(e["dice"], flat=int(e.get("flat", 0)),
                                      dtype=e.get("damage_type", "slashing"),
                                      slot=cond.get("name", "contingency"))
                      for e in cond.get("extra_dice", [])]
            all_rules.append(Rule(trigger=cond.get("trigger", "hit_or_crit"),
                                  on_attack=cond.get("attack"),
                                  extra_dice=extras,
                                  next_adv=int(cond.get("next_adv", 0)),
                                  spends=cond.get("spend") or {},
                                  name=cond.get("name", "contingency")))
            for pid, amount in (cond.get("spend") or {}).items():
                spends.append((rnum, pid, int(amount)))
            trace.append(f"第 {rnum} 轮条件规则：{cond.get('trigger')} 时"
                         f"{cond.get('name', '')}"
                         f"{'（消耗 ' + str(cond.get('spend')) + '）' if cond.get('spend') else ''}")

        # 动作经济校验（策略显式声明 steps 时）
        if rnd.get("steps"):
            steps = [Step(s.get("name", ""), s.get("cost", {}), s.get("group"))
                     for s in rnd["steps"]]
            state.attack_instances = max(1, attacks_n)
            for msg in validate_turn(steps, state):
                warnings.append(f"第 {rnum} 轮动作经济：{msg}")

        # 附赠动作攻击：来自增益算子（如「狂怒」）或职业特性（「武艺」的徒手追打），
        # 不占攻击动作的次数上限
        bonus_attacks = 0
        for pop in move_ops:
            bonus_attacks += int((pop.get("projection") or {})
                                 .get("bonus_action_attack", 0))
        if bonus_attacks and attacks_n:
            trace.append(f"第 {rnum} 轮附赠动作攻击 +{bonus_attacks} 次")

        gate_ctx = {"rage_active": rage_active, "mounted": bool(mount),
                    "weapon_property": list(weapon_doc.get("properties") or [])}
        # 条件上下文：构筑/回合状态 → 扁平键值，供 conditions 求值
        cond_ctx = build_condition_context(
            resolved, gate_ctx,
            {"size": resolved.size, "net_adv_tiers": adv,
             "prior_action": "moved" if state.travel_this_turn > 0 else None})
        wdice = weapon_dice_for(resolved, move_ops, build_doc)
        if wdice != resolved.weapon["dice"]:
            trace.append(f"第 {rnum} 轮武器伤害骰被替换："
                         f"{resolved.weapon['dice']} → {wdice}")

        attacks, all_rules_built = [], []
        if kind == "spell_attack" and attacks_n:
            if spell_id not in ops:
                raise KeyError(f"未知法术算子: {spell_id}")
            sop = ops[spell_id]
            instances = spell_attack_instances(sop, slot_level)
            if instances > 1:
                trace.append(f"第 {rnum} 轮 {sop['name']} 产生 {instances} 次攻击")
            for i in range(instances):
                a = build_spell_attack(resolved, sop, adv_tiers=adv,
                                       slot_level=slot_level, target=round_target)
                # 被动专长（构筑里已选）自动贡献 crit_range / extra_dice，
                # 但若策略已显式列出同一个算子则不重复应用。
                # 本轮增益（buffs）已由 buff_adv / rage_active 单独结算，
                # 这里再走一遍会把 adv_tiers 之类算两次。
                explicit = set(op_ids) | buffs
                for pop in passive_ops:
                    if pop["id"] in explicit:
                        continue
                    # 无条件调用 apply_op：条件、命中加值、命中后追加都靠它生效。
                    # 早期只在含 extra_dice 时调用，其余写法全部静默失效。
                    a, rs = apply_op(pop, a, resolved, attack_index=i,
                                     condition_ctx=cond_ctx)
                    all_rules_built.extend(rs)
                for oid in op_ids:
                    if oid not in ops:
                        raise KeyError(f"未知算子: {oid}")
                    miss = unmet_gates(ops[oid], gate_ctx)
                    if miss:
                        warnings.append(f"第 {rnum} 轮 {ops[oid]['name']} 前置条件不满足"
                                        f"（缺 {'/'.join(miss)}），未生效")
                        continue
                    # 限次专长：每被使用一次就记一次消耗，由预算校验是否超额
                    uses = (ops[oid].get("projection") or {}).get("uses")
                    if uses:
                        pool_id = uses.get("pool") or f"uses:{oid}"
                        # 构筑里没选该专长时不会有对应资源池——此时用量不受限，
                        # 说明构筑与策略不一致，必须告警而不是静默放行
                        if pool_id not in budget.pools:
                            warnings.append(
                                f"第 {rnum} 轮使用了 {ops[oid]['name']}，"
                                f"但构筑未包含该专长，使用次数不受限制")
                        else:
                            spends.append((rnum, pool_id, 1))
                    a, rs = apply_op(ops[oid], a, resolved, attack_index=i,
                                     condition_ctx=cond_ctx)
                    all_rules_built.extend(rs)
                apply_adv_scaling(a, passive_ops + [ops[o] for o in op_ids
                                                    if o in ops], resolved)
                attacks.append(a)
        else:
            bonus_dice = None
            bonus_type = None
            bonus_spec = None
            for pop in passive_ops:
                pproj = pop.get("projection") or {}
                if pproj.get("bonus_action_attack"):
                    bonus_spec = pproj.get("bonus_action_attack_spec") or {}
                if pproj.get("bonus_action_attack_dice"):
                    bonus_dice = pproj["bonus_action_attack_dice"]
                    bonus_type = pproj.get("bonus_action_attack_type")
            for i in range(attacks_n + bonus_attacks):
                use_dice = wdice
                # 附赠攻击的判定只取决于位置，不取决于是否另设了骰式
                is_bonus = i >= attacks_n and bonus_attacks > 0
                if is_bonus:
                    use_dice = bonus_dice          # 附赠动作攻击用指定骰式
                a = build_base_attack(resolved, adv_tiers=adv,
                                      rage_active=rage_active, weapon_dice=use_dice)
                if is_bonus and bonus_type:
                    # 附赠动作攻击可指定伤害类型（长柄武器末端为钝击）
                    for comp in a.components:
                        comp.dtype = bonus_type
                if is_bonus and bonus_spec:
                    # 附赠攻击可自带劣势、且/或不享属性调整值
                    # （双武器低级战士：副手攻击具有劣势，且无双武器战斗风格时不吃属性加值）
                    if "adv_tiers" in bonus_spec:
                        a.adv_tiers += int(bonus_spec["adv_tiers"])
                    if bonus_spec.get("no_ability_mod"):
                        ability = resolved.derived.mods["str"]
                        for comp in a.components:
                            comp.flat = max(0, comp.flat - ability)
                # 被动专长（构筑里已选）自动贡献 crit_range / extra_dice，
                # 但若策略已显式列出同一个算子则不重复应用。
                # 本轮增益（buffs）已由 buff_adv / rage_active 单独结算，
                # 这里再走一遍会把 adv_tiers 之类算两次。
                explicit = set(op_ids) | buffs
                for pop in passive_ops:
                    if pop["id"] in explicit:
                        continue
                    # 无条件调用 apply_op：条件、命中加值、命中后追加都靠它生效。
                    # 早期只在含 extra_dice 时调用，其余写法全部静默失效。
                    a, rs = apply_op(pop, a, resolved, attack_index=i,
                                     condition_ctx=cond_ctx)
                    all_rules_built.extend(rs)
                for oid in op_ids:
                    if oid not in ops:
                        raise KeyError(f"未知算子: {oid}（检查 形式化/ops/）")
                    miss = unmet_gates(ops[oid], gate_ctx)
                    if miss:
                        warnings.append(f"第 {rnum} 轮 {ops[oid]['name']} 前置条件不满足"
                                        f"（缺 {'/'.join(miss)}），未生效")
                        continue
                    # 限次专长：每被使用一次就记一次消耗，由预算校验是否超额
                    uses = (ops[oid].get("projection") or {}).get("uses")
                    if uses:
                        pool_id = uses.get("pool") or f"uses:{oid}"
                        # 构筑里没选该专长时不会有对应资源池——此时用量不受限，
                        # 说明构筑与策略不一致，必须告警而不是静默放行
                        if pool_id not in budget.pools:
                            warnings.append(
                                f"第 {rnum} 轮使用了 {ops[oid]['name']}，"
                                f"但构筑未包含该专长，使用次数不受限制")
                        else:
                            spends.append((rnum, pool_id, 1))
                    a, rs = apply_op(ops[oid], a, resolved, attack_index=i,
                                     condition_ctx=cond_ctx)
                    all_rules_built.extend(rs)
                apply_adv_scaling(a, passive_ops + [ops[o] for o in op_ids
                                                    if o in ops], resolved)
                attacks.append(a)
            if attacks:
                all_rules_built.extend(build_sneak_rules(
                    resolved, build_doc, ops, attacks, state.distance_ft, env_doc))
        all_rules.extend(all_rules_built)

        # 借机攻击：打桩默认目标不动不还手，**触发次数由环境显式声明**（默认 0）。
        # 不声明就不产生收益——否则等于白送一次攻击。
        # 环境说"敌人做了什么"，构筑决定"哪些能做反应"：施法/进入触及/攻击你
        # 都只有对应专长才能借机，没解锁的触发在 trace 里单独报出来。
        (oa_sources, oa_per_trigger, oa_drawbacks,
         oa_skipped) = opportunity_trigger_sources(resolved, ops)
        oa_triggers, oa_used, oa_blocked, oa_used_src = opportunity_attack_triggers(
            env_doc, oa_sources)
        for note in oa_blocked:
            trace.append(f"第 {rnum} 轮借机触发：{note}，未计入")
        for note in oa_skipped:
            warnings.append(f"第 {rnum} 轮借机触发：{note}，未计入")
        # 用「敌人攻击你」换借机的专长（罗比拉战术）是有代价的：你承受更多伤害。
        # 本工具只算输出，代价不进数字——必须明说，否则这个专长会被系统性高估。
        for src in oa_used_src:
            if src in oa_drawbacks:
                warnings.append(oa_drawbacks[src])
        if oa_triggers > 0 and attacks:
            budget, budget_sources = opportunity_attack_budget(resolved, ops)
            # 单次触发可以打出多击（连续借机），但总攻击数仍受反应预算封顶
            count = min(budget, oa_triggers * oa_per_trigger)
            oa_adv = 0
            for op in granted_feat_ops(build_doc, ops):
                oa_adv += int((op.get("projection") or {}).get("opportunity_attack_adv", 0))
            for _ in range(count):
                a = build_base_attack(resolved, adv_tiers=adv + oa_adv,
                                      rage_active=rage_active, weapon_dice=wdice)
                apply_adv_scaling(a, granted_ops, resolved)
                attacks.append(a)
            trace.append(f"第 {rnum} 轮借机攻击 {count} 次"
                         f"（触发 {'、'.join(oa_used)}，预算 {budget}："
                         f"{'、'.join(budget_sources)}）")

        dmg, leaves = (evaluate_turn(attacks, all_rules, round_target) if attacks
                       else (Fraction(0), 0))

        # 作用于本轮所有豁免效果的修正专长（如「威力豁免」）
        dc_modifier = 0
        on_fail_extra = None
        for soid in rnd.get("save_ops", []):
            sop = ops.get(soid)
            if not sop:
                raise KeyError(f"未知豁免修正算子: {soid}")
            proj = sop.get("projection") or {}
            dc_modifier += int(proj.get("save_dc_modifier", 0))
            if proj.get("on_fail_extra"):
                on_fail_extra = proj["on_fail_extra"]
            trace.append(f"第 {rnum} 轮 {sop['name']}：豁免 DC {dc_modifier:+d}")

        # 豁免型效果：可按法术算子 id 引用，也可内联写明
        for eff in rnd.get("save_effects", []):
            if "spell" in eff:
                sid = eff["spell"]
                if sid not in ops:
                    raise KeyError(f"未知法术算子: {sid}")
                srng = _range_ft(ops[sid].get("range"))
                if srng is not None and state.distance_ft > srng:
                    warnings.append(
                        f"第 {rnum} 轮 {ops[sid]['name']} 距离 {state.distance_ft} 尺"
                        f"超出施法距离 {srng} 尺，未计入")
                    continue
                sd, dice = save_effect_from_op(resolved, ops[sid], target,
                                               eff.get("slot_level"),
                                               dc_modifier=dc_modifier,
                                               on_fail_extra=on_fail_extra)
                trace.append(f"第 {rnum} 轮 {ops[sid]['name']}（{dice}，豁免）"
                             f" → {float(sd):.3f}")
            else:
                sd = _save_effect_damage(eff, target, resolved)
                trace.append(f"第 {rnum} 轮豁免效果 {eff.get('dice')} → {float(sd):.3f}")
            dmg += sd

        # 步骤 5：攻击之后才能用剩余移动力（骑乘冲刺流程的第 5 步）。
        # 「反复拉冲」的可持续性取决于这里——退开的距离决定了下一轮能否再冲。
        post = rnd.get("post_move")
        if post:
            moved = state.move(int(post.get("ft", 0)),
                               toward=bool(post.get("toward", False)))
            trace.append(f"第 {rnum} 轮攻击后移动 {moved} 尺 → 距离 {state.distance_ft} 尺")

        # 专注：同时只能维持一个。打桩下没有持续效果模型，因此这里只做冲突检测，
        # 待专注类法术编译后，未中断的专注效果应跨轮继续生效。
        for eff in rnd.get("save_effects", []):
            sid = eff.get("spell")
            if sid and ops.get(sid, {}).get("concentration"):
                if concentration_active:
                    warnings.append(f"第 {rnum} 轮施放 {ops[sid]['name']}（专注），"
                                    f"但 {concentration_active} 的专注尚未结束")
                concentration_active = ops[sid]["name"]
                if ops[sid].get("concentration_effect"):
                    sustained_op = ops[sid]
        pa_spell = (rnd.get("per_attack") or {}).get("spell")
        if pa_spell and ops.get(pa_spell, {}).get("concentration"):
            if concentration_active:
                warnings.append(f"第 {rnum} 轮施放 {ops[pa_spell]['name']}（专注），"
                                f"但 {concentration_active} 的专注尚未结束")
            concentration_active = ops[pa_spell]["name"]
        if rnd.get("end_concentration"):
            concentration_active = None

        dmg += sustained_dmg
        per_round.append((rnum, dmg))
        trace.append(f"第 {rnum} 轮：{attacks_n} 次攻击"
                     f"{'（狂暴）' if 'rage' in buffs else ''}"
                     f"{'，' + '、'.join(op_ids) if op_ids else ''} → {float(dmg):.3f}")

    total = sum((d for _, d in per_round), Fraction(0))
    budget_errors = budget.check_schedule(spends) if spends else []
    return Result(per_round=per_round, total=total, leaves=leaves, trace=trace,
                  spend_report=budget.spend_report(spends) if spends else [],
                  warnings=warnings + budget_errors)


# ---------------------------------------------------------------- CLI

def main(argv):
    if len(argv) < 4:
        print("用法: python -I engine/calculator.py <build.json> <env.json> <strategy.json>")
        return 2
    docs = []
    for path in argv[1:4]:
        with open(path, encoding="utf-8") as fh:
            docs.append(json.load(fh))
    res = evaluate(*docs)
    print("=" * 60)
    print("输入追溯")
    print("=" * 60)
    for line in res.trace:
        print("  " + line)
    print("=" * 60)
    print(f"期望伤害合计: {float(res.total):.3f}")
    if res.per_round:
        print(f"平均每轮:     {float(res.total / len(res.per_round)):.3f}")
    print(f"结果树叶子:   {res.leaves}")
    if res.spend_report:
        print("资源消耗:")
        for line in res.spend_report:
            print("  " + line)
    if res.warnings:
        print("警告:")
        for w in res.warnings:
            print("  ! " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
