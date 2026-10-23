#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接口一致性检查：算子、样例、数据三者之间的契约是否对得上。

用法：
    python check_interfaces.py

检查项：
  A. 算子文件
       A1 id 与文件名一致
       A2 必填字段齐全（id / kind / source / status）
       A3 source 含 version 与 file（溯源强制，见决策 D-015）
       A4 法术算子的字段位置一致（scaling / upcast 必须在顶层）
       A5 攻击型法术必须有 attack.damage；豁免型必须有 save + damage
  B. 样例输入
       B1 策略引用的算子 id 都存在
       B2 策略消耗的资源池都在构筑的 resources 中声明
       B3 构筑的 casting_ability 与所用法术匹配
  C. 数据文件
       C1 JSONL 每行合法
       C2 每条都有 source（溯源强制）

任何一条失败即返回非零退出码——接口漂移必须在提交前被拦住，
而不是等到计算器给出一个看似合理的错数。
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OPS_DIR = os.path.join(REPO, "形式化", "ops")
DATA_DIR = os.path.join(REPO, "形式化", "data")
EX_DIR = os.path.join(REPO, "examples")

REQUIRED_OP_FIELDS = ("id", "kind", "source", "status")
VALID_KINDS = {"feat", "spell", "maneuver", "class_feature", "action"}

errors = []
warnings = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warnings.append(msg)


def check_ops():
    if not os.path.isdir(OPS_DIR):
        err(f"算子目录不存在: {OPS_DIR}")
        return {}
    ops = {}
    for fname in sorted(os.listdir(OPS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(OPS_DIR, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                op = json.load(fh)
        except json.JSONDecodeError as exc:
            err(f"[A] {fname}: JSON 解析失败 — {exc}")
            continue

        stem = fname[:-5]
        if op.get("id") != stem:
            err(f"[A1] {fname}: id={op.get('id')!r} 与文件名不一致")

        for field in REQUIRED_OP_FIELDS:
            if field not in op:
                err(f"[A2] {fname}: 缺必填字段 {field}")

        if op.get("kind") and op["kind"] not in VALID_KINDS:
            err(f"[A2] {fname}: 未知 kind={op['kind']!r}")

        src = op.get("source") or {}
        if not src.get("version") or not src.get("file"):
            err(f"[A3] {fname}: source 缺 version 或 file（溯源强制，D-015）")

        # A6：职业特性算子必须能与构筑的 class id 对上
        acq = op.get("acquisition") or {}
        if acq.get("type") == "class_level":
            if not acq.get("class_id"):
                err(f"[A6] {fname}: class_level 算子缺 acquisition.class_id"
                    f"（构筑用 class.barbarian，特性数据用 狂战士，必须有显式映射）")
            if not isinstance(acq.get("level"), int):
                err(f"[A6] {fname}: acquisition.level 必须是整数")

        # A7：攻击次数的类型与取值
        proj = op.get("projection") or {}
        if "attack_count" in proj:
            if not isinstance(proj["attack_count"], int) or proj["attack_count"] < 1:
                err(f"[A7] {fname}: attack_count 必须是 ≥1 的整数")
            if proj.get("attack_count_mode") not in ("max", None):
                err(f"[A7] {fname}: attack_count_mode 目前只支持 max")

        if op.get("kind") == "spell":
            # A8：多实例法术的字段形态
            atk = op.get("attack") or {}
            if "count_by_slot" in atk:
                bad_keys = [k for k in atk["count_by_slot"] if not str(k).isdigit()]
                if bad_keys:
                    err(f"[A8] {fname}: count_by_slot 的键必须是环阶数字，发现 {bad_keys}")
            if atk.get("auto_hit") and atk.get("crucial") is None:
                pass   # auto_hit 无额外要求，但不得与需要攻击检定的字段混用
            # A9：upcast 必须说明加什么，否则升环无效果
            up = op.get("upcast") or {}
            if up and not any(k in up for k in ("add_dice", "add_count_per_slot")):
                err(f"[A9] {fname}: upcast 缺 add_dice 或 add_count_per_slot，升环不会产生任何效果")

            # A4：scaling / upcast 必须写在顶层。写在 attack 内部会让等级成长静默失效。
            if "scaling" in (op.get("attack") or {}):
                err(f"[A4] {fname}: scaling 写在了 attack 内，必须移到顶层")
            if "upcast" in (op.get("attack") or {}):
                err(f"[A4] {fname}: upcast 写在了 attack 内，必须移到顶层")
            # A5：两条伤害路径各自的必填项
            if "attack" in op:
                if not (op["attack"].get("damage") or {}).get("dice"):
                    err(f"[A5] {fname}: 攻击型法术缺 attack.damage.dice")
                if "damage" in op:
                    err(f"[A5] {fname}: 攻击型与豁免型字段混用（同时有 attack 与 damage）")
            elif "save" in op:
                if not (op.get("damage") or {}).get("dice"):
                    err(f"[A5] {fname}: 豁免型法术缺 damage.dice")
                if "ability" not in (op.get("save") or {}):
                    err(f"[A5] {fname}: save 缺 ability")
            else:
                err(f"[A5] {fname}: 法术必须有 attack 或 save")

        ops[op.get("id", stem)] = op
    return ops


def check_examples(ops):
    if not os.path.isdir(EX_DIR):
        warn(f"样例目录不存在，跳过 B: {EX_DIR}")
        return
    files = sorted(f for f in os.listdir(EX_DIR) if f.endswith(".json"))
    builds = {}
    for fname in files:
        path = os.path.join(EX_DIR, fname)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except json.JSONDecodeError as exc:
            err(f"[B] {fname}: JSON 解析失败 — {exc}")
            continue
        if "combat" in doc:
            builds[fname] = doc

    for fname in files:
        if not fname.endswith(".strategy.json"):
            continue
        with open(os.path.join(EX_DIR, fname), encoding="utf-8") as fh:
            strat = json.load(fh)
        for rnd in strat.get("rounds", []):
            pa = rnd.get("per_attack") or {}
            for oid in pa.get("ops", []):
                if oid not in ops:
                    err(f"[B1] {fname}: 引用不存在的算子 {oid}")
            sid = pa.get("spell")
            if sid and sid not in ops:
                err(f"[B1] {fname}: 引用不存在的法术算子 {sid}")
            for eff in rnd.get("save_effects", []):
                sid = eff.get("spell")
                if sid and sid not in ops:
                    err(f"[B1] {fname}: 引用不存在的法术算子 {sid}")


def check_build_strategy_pairs(ops):
    """B2/B3：按策略中声明的 requires_build 精确配对检查。

    早期实现是"每条策略对每个构筑都查一遍"，会产生大量假阳性——
    这说明样例之间缺少显式配对声明，本身就是接口缺陷，故改用显式字段。
    """
    if not os.path.isdir(EX_DIR):
        return
    builds = {}
    for fname in os.listdir(EX_DIR):
        if fname.endswith(".build.json"):
            with open(os.path.join(EX_DIR, fname), encoding="utf-8") as fh:
                builds[fname] = json.load(fh)

    for sname in sorted(os.listdir(EX_DIR)):
        if not sname.endswith(".strategy.json"):
            continue
        with open(os.path.join(EX_DIR, sname), encoding="utf-8") as fh:
            strat = json.load(fh)

        build_name = (strat.get("meta") or {}).get("requires_build")
        if not build_name:
            err(f"[B4] {sname}: 缺 meta.requires_build，无法确定与哪个构筑配对")
            continue
        if build_name not in builds:
            err(f"[B4] {sname}: requires_build={build_name} 不存在")
            continue

        build = builds[build_name]
        has_casting = bool(build.get("combat", {}).get("casting_ability"))
        resources = set((build.get("resources") or {}).keys())

        rounds = strat.get("rounds", [])
        uses_spell = any((r.get("per_attack") or {}).get("spell") or
                         any("spell" in e for e in r.get("save_effects", []))
                         for r in rounds)
        if uses_spell and not has_casting:
            err(f"[B3] {sname} 用法术，但 {build_name} 未声明 casting_ability")

        for rnd in rounds:
            for pid in (rnd.get("spends") or {}):
                if pid not in resources:
                    err(f"[B2] {sname} 消耗 {pid}，但 {build_name} 未声明该资源池")


KIND_TO_DATA = {
    "feat": "feats.jsonl",
    "spell": "spells.jsonl",
    "maneuver": "maneuvers.jsonl",
    "class_feature": "class_features.jsonl",
}


def check_op_data_alignment(ops):
    """A10：每个算子 id 必须能在对应的数据表里找到条目。

    这条检查的由来：曾把「神性之怒」编译成 `cf.神罚者.神性之怒`，
    而数据里的 id 是 `cf.狂战士.神性之怒`（class 是主职，subclass 单独存）。
    算子在、数据在，但两者永远匹配不上，覆盖率统计显示为 0 却不报错。
    """
    cache = {}
    for oid, op in ops.items():
        kind = op.get("kind")
        fname = KIND_TO_DATA.get(kind)
        if not fname:
            continue                      # action 类算子源自「基础」章节，不在 A 档数据里
        if fname not in cache:
            path = os.path.join(DATA_DIR, fname)
            ids = set()
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                ids.add(json.loads(line).get("id"))
                            except json.JSONDecodeError:
                                pass
            cache[fname] = ids
        if oid not in cache[fname]:
            err(f"[A10] {oid}: 在 {fname} 中找不到同名条目"
                f"（算子与数据对不上，永远无法被引用）")


# 引擎实际消费的 projection 字段（封闭词汇表）。
# 多 agent 协作时这是最关键的一道闸：没有它，每个 agent 都会发明自己的字段，
# 而引擎对未知字段是**静默忽略**的——算子写了、看着对、实际不生效。
ALLOWED_PROJECTION = {
    "note",                          # 纯注释，引擎不消费（允许）
    "adv_tiers", "attack_bonus", "attack_count", "crit_range",
    "damage_slots", "extra_dice", "ability_bonus",
    "weapon_dice_override", "weapon_dice_min", "weapon_dice_min_applies_to",
    "weapon_dice_step_up",
    "bonus_action_attack", "bonus_action_attack_dice", "bonus_action_attack_type",
    "bonus_action_attack_spec",    # 附赠攻击自带的劣势 / 去属性调整值
    "bonus_action_ability_mod",    # 附赠攻击享受攻击关键属性调整值（双武器战斗）
    "damage_type_override",        # 改写武器伤害类型（龙焰铸就、野兽之形）
    "weapon_enhancement_bonus",    # 武器增强加值（强化天生武器 +1/+2/+3）
    "advantage_to_attacks",        # 放弃每重优势换额外攻击（飞燕斩）
    "natural_weapon",              # 天生武器形态：{dice, damage_type, properties}
    "opportunity_attack_budget_add",  # 每轮额外借机攻击次数
    "opportunity_attack_adv",        # 借机攻击的优势层数
    "scales_with_adv",               # 按最终优势层数结算：per_tier / steps
    "choice_key",                    # 多选一：构筑 choices 里读取的键
    "options",                       # 多选一：{选项名: {projection, gate, ...}}
    "chosen_option",                 # 引擎回填：实际选中的选项
    "sneak_attack",                  # 偷袭骰（按职业等级分段），每回合一次
    "sneak_attack_variant",          # 偷袭方式的扩充（duel）
    "reach_bonus_ft",                # 触及距离加值（F12 判据）
    "speed_bonus_ft",                # 移动速度加值
    "weapon_selector",               # 限定武器：{kinds, properties}，满足其一即可
    "weapon_selector_exclude",       # 排除武器（武艺不适用于重型武器）
    "requires_unarmored",            # 未着装盔甲与盾牌时才生效
    "opportunity_trigger_source",    # 能引发借机的环境事件：approach/cast_spell/...
    "opportunity_trigger_requires_weapon",  # 该触发只在持用指定武器时成立
    "opportunity_attack_per_trigger",  # 单次触发打几击（连续借机 = 2）
    "opportunity_trigger_drawback",  # 该触发附带的代价（DPR 不计量，仅告警）
    "on_hit_extra_attack", "on_all_hit_extra_attack", "on_hit_target_vuln",
    "damage_replaces_weapon", "damage_table", "formula_damage", "attack_irreducible",
    "save_dc_modifier", "on_fail_extra", "requires_contest",
    "grants_attack_count", "uses", "rage", "area",
    "size_increase",               # 已声明但引擎未实现，见审计文档
    "conditional_dice",            # 旧写法，已被通用 conditions 取代
    "conditions",                  # 通用条件结构
}


def check_projection_vocabulary(ops):
    """A12：算子只能使用引擎认识（或已登记待实现）的 projection 字段。"""
    for oid, op in ops.items():
        for key in (op.get("projection") or {}):
            if key not in ALLOWED_PROJECTION:
                err(f"[A12] {oid}: 未知 projection 字段 {key!r} —— "
                    f"引擎会静默忽略它。如需新增请先扩展引擎并登记到白名单")


CONDITION_KEYS = {"attacker_state", "prior_action", "target_hp", "target_state",
                  "target_size", "weapon_property", "once_per_turn"}
CONDITION_EFFECTS = {"extra_dice", "damage_slots", "attack_bonus", "damage_mult",
                     "crit_range", "note"}


def check_conditions(ops):
    """A13：conditions 的 when/then 只能用封闭词汇表里的键。

    与 A12 同理——引擎对未知条件键是**抛错**的，但对未知效果键是静默忽略的，
    后者更危险，必须在提交前拦住。
    """
    for oid, op in ops.items():
        for i, rule in enumerate((op.get("projection") or {}).get("conditions", [])):
            for key in (rule.get("when") or {}):
                if key not in CONDITION_KEYS:
                    err(f"[A13] {oid} conditions[{i}]: 未知条件 {key!r}")
            for key in (rule.get("then") or {}):
                if key not in CONDITION_EFFECTS:
                    err(f"[A13] {oid} conditions[{i}]: 未知效果 {key!r}")


def check_table_refs(ops):
    """A11：算子引用的伤害表必须存在，且轴键能被解析。

    与 A10 同类问题：表名写错时算子不会立刻出错，而是在求值那一刻才炸，
    甚至可能因为走了默认分支而静默取到错误的档位。
    """
    path = os.path.join(DATA_DIR, "damage_tables.jsonl")
    tables = {}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    t = json.loads(line)
                    tables[t["id"]] = t
    for oid, op in ops.items():
        spec = (op.get("projection") or {}).get("damage_table")
        if not spec:
            continue
        tid = spec.get("table")
        if tid not in tables:
            err(f"[A11] {oid}: 引用的伤害表 {tid} 不存在于 damage_tables.jsonl")
            continue
        t = tables[tid]
        if "bands" in t:
            if "row" not in t.get("axes", {}):
                err(f"[A11] {oid}: 分档表 {tid} 缺 axes.row")
        else:
            for axis in ("row", "column"):
                if axis not in t.get("axes", {}):
                    err(f"[A11] {oid}: 交叉表 {tid} 缺 axes.{axis}")


def check_data():
    if not os.path.isdir(DATA_DIR):
        err(f"数据目录不存在: {DATA_DIR}")
        return
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        path = os.path.join(DATA_DIR, fname)
        bad = 0
        nosrc = 0
        total = 0
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    err(f"[C1] {fname}:{lineno} JSON 非法 — {exc}")
                    bad += 1
                    continue
                if fname not in ("relevance.jsonl", "feat_triage.jsonl") and not rec.get("source"):
                    nosrc += 1
        if nosrc:
            err(f"[C2] {fname}: {nosrc}/{total} 条缺 source（溯源强制，D-015）")


def main():
    print("=" * 60)
    print("接口一致性检查")
    print("=" * 60)

    ops = check_ops()
    print(f"算子: {len(ops)} 个")
    check_projection_vocabulary(ops)
    check_conditions(ops)
    check_op_data_alignment(ops)
    check_table_refs(ops)
    check_examples(ops)
    check_build_strategy_pairs(ops)
    check_data()

    print("-" * 60)
    for w in warnings:
        print("  警告 " + w)
    for e in errors:
        print("  错误 " + e)
    print("-" * 60)
    if errors:
        print(f"失败：{len(errors)} 项错误")
        return 1
    print(f"通过：无错误（{len(warnings)} 项警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
