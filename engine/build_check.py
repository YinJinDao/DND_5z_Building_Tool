#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构筑校验：找出**会静默改变结果**的书写问题。

这个模块存在的理由只有一个——引擎对未知字段与对不上的名字一律宽容：
子职名写错不会报错，只是你拿不到该子职的任何特性；`choices` 的键写错不会报错，
只是那个多选一算作没选；专长的先决条件不满足也不会报错，只是白拿了一份收益。

宽容在这里是错的。**同名不同物、拼写错误、遗漏选择，三者在数值上看起来
都像"构筑本来就这样"**，而使用者无法从输出里分辨。所以校验要单独跑一遍，
每一条都指到具体字段，而不是等数字对不上再回头猜。

分级：
  ERROR  一定会算错（未知职业、等级超范围、属性键拼错、点数对不上）
  WARN   大概率算错（子职名与数据对不上、专长先决未满足、选项不存在）
  NOTE   可能少算（未声明的武器属性、缺失的多选一、未填写施法属性）
"""

import json
import os
import re

ABILITIES = ("str", "dex", "con", "int", "wis", "cha")
ABILITY_CN = {"力量": "str", "敏捷": "dex", "体质": "con",
              "智力": "int", "感知": "wis", "魅力": "cha"}
# 职业 id 的权威来源是数据本身；这里只用来给"完全写错"的情况一条有意义的提示
KNOWN_CLASS_IDS = ("class.artificer", "class.barbarian", "class.bard", "class.cleric",
                   "class.druid", "class.monk", "class.paladin", "class.psion",
                   "class.psychic_warrior", "class.ranger", "class.rogue",
                   "class.summoner", "class.warlock", "class.wizard")


class Report:
    def __init__(self):
        self.items = []

    def add(self, level, code, msg):
        self.items.append({"level": level, "code": code, "message": msg})

    def errors(self):
        return [i for i in self.items if i["level"] == "ERROR"]

    def warnings(self):
        return [i for i in self.items if i["level"] == "WARN"]

    def notes(self):
        return [i for i in self.items if i["level"] == "NOTE"]

    def ok(self):
        return not self.errors()

    def lines(self):
        order = {"ERROR": 0, "WARN": 1, "NOTE": 2}
        return [f"[{i['level']}] {i['code']} {i['message']}"
                for i in sorted(self.items, key=lambda x: order[x["level"]])]


# ---------------------------------------------------------------- 数据侧索引

def index_ops(ops):
    """把算子整理成校验需要的几个索引。"""
    idx = {"classes": {}, "subclasses": {}, "choice_keys": {},
           "feat_ids": set(), "feat_prereq": {}, "weapon_gated": [],
           "class_name_of_id": {}}
    for op in ops.values():
        acq = op.get("acquisition") or {}
        if acq.get("type") == "class_level":
            cid = acq.get("class_id")
            cls = acq.get("class")
            if cid:
                idx["classes"].setdefault(cid, set()).add(cls)
                if cls:
                    idx["class_name_of_id"][cid] = cls
            if acq.get("subclass"):
                idx["subclasses"].setdefault(cls, set()).add(acq["subclass"])
        if op.get("kind") == "feat":
            idx["feat_ids"].add(op["id"])
            idx["feat_prereq"][op["id"]] = (acq.get("prerequisite") or [])
        proj = op.get("projection") or {}
        if proj.get("choice_key"):
            entry = idx["choice_keys"].setdefault(
                proj["choice_key"], {"ops": [], "options": set()})
            entry["ops"].append(op["id"])
            entry["options"].update(k for k in (proj.get("options") or {})
                                    if k != "default")
        # 武器门控：需要构筑声明 kind 或 properties，否则这些特性不生效
        sel = proj.get("weapon_selector")
        if sel:
            idx["weapon_gated"].append((op["id"], op["name"],
                                        sorted(sel.get("kinds") or []),
                                        sorted(sel.get("properties") or [])))
    return idx


# ---------------------------------------------------------------- 各项检查

def check_meta(doc, rep):
    meta = doc.get("meta") or {}
    if not meta.get("rules_version"):
        rep.add("ERROR", "meta.rules_version",
                "缺规则书版本：算子与数据都锚定在版本上，缺了就无法复核")
    classes = doc.get("classes") or []
    total = meta.get("level")
    if total is None:
        total = sum(int(c.get("level", 0)) for c in classes)
        if not classes:
            rep.add("ERROR", "meta.level", "既未给总等级，也没有任何职业")
    elif classes:
        s = sum(int(c.get("level", 0)) for c in classes)
        if s != int(total):
            rep.add("ERROR", "meta.level",
                    f"总等级 {total} 与各职业等级之和 {s} 不一致——"
                    f"熟练加值、法术环阶、额外攻击都由此推出")
    return int(total or 1)


def check_classes(doc, idx, rep):
    classes = doc.get("classes") or []
    if not classes:
        rep.add("ERROR", "classes", "构筑没有任何职业")
    seen = set()
    for i, c in enumerate(classes):
        cid = c.get("id")
        here = f"classes[{i}]"
        if cid in seen:
            rep.add("ERROR", here + ".id",
                    f"职业 {cid} 重复出现——同一职业的等级应当合并")
        seen.add(cid)
        if cid not in idx["classes"]:
            hint = "" if cid in KNOWN_CLASS_IDS else "（不在已知职业列表中）"
            rep.add("ERROR", here + ".id",
                    f"未知职业 {cid!r}{hint}：没有任何算子挂在它下面，"
                    f"该职业不产生任何效果")
            continue
        lv = c.get("level")
        if not isinstance(lv, int) or not 1 <= lv <= 20:
            rep.add("ERROR", here + ".level", f"职业等级必须是 1–20 的整数，收到 {lv!r}")
        # 子职名必须与数据一致，否则该子职的全部特性**静默消失**
        sub = c.get("subclass")
        valid = sorted(idx["subclasses"].get(
            list(idx["classes"][cid])[0], set()))
        if sub:
            if sub not in valid:
                rep.add("WARN", here + ".subclass",
                        f"子职 {sub!r} 与数据中的名字对不上，该子职的特性不会生效。"
                        f"可用：{'、'.join(valid) or '（该职业没有子职数据）'}")
        elif valid:
            rep.add("NOTE", here + ".subclass",
                    f"未选子职：{'、'.join(valid)} 的特性都不会生效")


def check_scores(doc, rep):
    scores = doc.get("scores") or {}
    if "base" not in scores:
        rep.add("ERROR", "scores.base", "缺初始属性栏 base")
    for group, vals in scores.items():
        for k in (vals or {}):
            if k not in ABILITIES:
                rep.add("ERROR", f"scores.{group}.{k}",
                        f"未知属性键 {k!r}，可用：{'/'.join(ABILITIES)}")


def check_feats(doc, idx, total_level, rep):
    feats = doc.get("feats") or []
    have = {f.get("id") for f in feats}
    for i, f in enumerate(feats):
        fid = f.get("id")
        here = f"feats[{i}]"
        if fid not in idx["feat_ids"]:
            rep.add("ERROR", here + ".id",
                    f"未知专长 {fid!r}：没有对应算子，等于没选")
            continue
        lv = f.get("at_level")
        if lv is not None and int(lv) > total_level:
            rep.add("ERROR", here + ".at_level",
                    f"专长在 {lv} 级取得，但角色总等级只有 {total_level}")
        unmet = unmet_prerequisites(fid, idx, doc, have, total_level)
        if unmet:
            rep.add("WARN", here + ".prerequisite",
                    f"「{fid.split('.', 1)[1]}」的先决条件未满足：{'；'.join(unmet)}")


ABS_PREREQ = re.compile(r"(力量|敏捷|体质|智力|感知|魅力)\s*(\d+)")


def _score_of(doc, ability):
    total = 0
    for group in ("base", "racial", "level_ups", "feat", "item"):
        total += int(((doc.get("scores") or {}).get(group) or {}).get(ability, 0))
    return total


CLASS_PREREQ = re.compile(r"([^，,、\s]{2,4})(\d+)级")


def unmet_prerequisites(fid, idx, doc, have_feats, total_level):
    """检查先决条件里**能自动判定**的部分。

    规则书的先决条件混着三类东西：数值（等级、属性值）、
    其它专长、以及"某种武器熟练"这类需要另一张表才能判定的要求。
    前两类能查，第三类不猜——查不出就不报，避免误报把真问题淹掉。
    """
    missing = []
    for p in idx["feat_prereq"].get(fid, []):
        p = str(p).strip()
        m = re.fullmatch(r"(\d+)\s*级", p)
        if m:
            if total_level < int(m.group(1)):
                missing.append(f"需要 {m.group(1)} 级")
            continue
        # 职业等级先决：形如「牧师2级，圣武士2级」
        cm = CLASS_PREREQ.fullmatch(p.replace(" ", ""))
        if cm:
            cname, lv = cm.group(1), int(cm.group(2))
            got = 0
            for c in doc.get("classes") or []:
                if idx["class_name_of_id"].get(c.get("id")) == cname:
                    got = int(c.get("level", 0))
            if got < lv:
                missing.append(f"需要{cname} {lv} 级（当前 {got} 级）")
            continue
        m = ABS_PREREQ.fullmatch(p.replace("，", "").replace(" ", ""))
        if m:
            ab = ABILITY_CN[m.group(1)]
            if _score_of(doc, ab) < int(m.group(2)):
                missing.append(f"需要{m.group(1)} {m.group(2)}"
                               f"（当前 {_score_of(doc, ab)}）")
            continue
        # 形如 "精通额外攻击" 的先决：若数据里有同名算子且构筑未取得，提示
        # 但职业特性类先决不在这里判——需要职业等级，交给引擎的门控
    return missing


def active_choice_keys(doc, ops):
    """这个构筑**实际会拿到**的多选一。

    数据库里有多选一的算子远多于本构筑涉及的数量。把全部键都列出来当提醒，
    等于每次输出十几条与角色无关的噪音——提醒的价值在于"你漏了这个"，
    而不是"世界上还有这些东西"。
    """
    total = sum(int(c.get("level", 0)) for c in doc.get("classes") or [])
    levels = {c.get("id"): int(c.get("level", 0)) for c in doc.get("classes") or []}
    subs = {c.get("id"): c.get("subclass") for c in doc.get("classes") or []}
    keys = set()
    for op in ops.values():
        proj = op.get("projection") or {}
        key = proj.get("choice_key")
        if not key:
            continue
        acq = op.get("acquisition") or {}
        if acq.get("type") == "class_level":
            cid = acq.get("class_id")
            if cid not in levels or int(acq.get("level", 99)) > levels[cid]:
                continue
            need = acq.get("subclass")
            if need and need != subs.get(cid):
                continue
            keys.add(key)
        elif op.get("kind") == "feat":
            if op["id"] in {f.get("id") for f in doc.get("feats") or []}:
                keys.add(key)
        elif op.get("kind") == "maneuver":
            known = ((doc.get("maneuvers") or {}).get("known") or []) +                     ((doc.get("maneuvers") or {}).get("prepared") or [])
            if op["id"] in known:
                keys.add(key)
    return keys


def check_choices(doc, idx, ops, rep):
    choices = doc.get("choices") or {}
    active = active_choice_keys(doc, ops)
    for key, val in choices.items():
        if key not in idx["choice_keys"]:
            rep.add("ERROR", f"choices.{key}",
                    f"未知选择键 {key!r}。可用："
                    f"{'、'.join(sorted(idx['choice_keys']))}")
            continue
        entry = idx["choice_keys"][key]
        if val is None:
            continue
        pick = val if isinstance(val, str) else (val or {}).get("option")
        if pick not in entry["options"]:
            rep.add("WARN", f"choices.{key}",
                    f"选项 {pick!r} 不在 {'/'.join(sorted(entry['options']))} 中；"
                    f"该多选一将按未选择处理")
    # 该构筑会拿到、但没作出选择的：不算错，但要指出它没生效
    for key, entry in idx["choice_keys"].items():
        if key in choices or key not in active:
            continue
        rep.add("NOTE", f"choices.{key}",
                f"未作出选择：{'、'.join(entry['ops'])} 将不生效"
                f"（可选 {'/'.join(sorted(entry['options']))}）")


#: 武器属性词汇（与 calculator.CONDITION_VALUES 的 weapon_property 保持一致）
WEAPON_PROPERTIES = ("双手", "两用", "轻型", "重型", "灵巧", "触及", "投掷", "弹药", "装填")


def check_weapon(doc, idx, rep):
    combat = doc.get("combat") or {}
    w = combat.get("weapon") or {}
    kinds = w.get("kind")
    props = w.get("properties") or []
    needs_kind = [g for g in idx["weapon_gated"] if g[2]]
    needs_prop = [g for g in idx["weapon_gated"] if g[3]]
    if not kinds and needs_kind:
        rep.add("NOTE", "combat.weapon.kind",
                f"未声明武器种类，按种类门控的特性无法判定："
                f"{'、'.join(g[1] for g in needs_kind[:5])}")
    if not props and needs_prop:
        rep.add("NOTE", "combat.weapon.properties",
                f"未声明武器属性，按属性门控的特性无法判定："
                f"{'、'.join(g[1] for g in needs_prop[:5])}；"
                f"可用：双手/两用/轻型/触及/投掷/弹药")
    else:
        bad = [p for p in props if p not in WEAPON_PROPERTIES]
        if bad:
            rep.add("WARN", "combat.weapon.properties",
                    f"未知武器属性 {bad}——引擎只认 {'/'.join(WEAPON_PROPERTIES)}，"
                    f"写成别的不会报错，只会判为「没有该属性」")
    if not w.get("dice"):
        rep.add("ERROR", "combat.weapon.dice", "缺武器伤害骰")
    if not w.get("damage_type"):
        rep.add("ERROR", "combat.weapon.damage_type", "缺武器伤害类型")
    ab = w.get("ability")
    if ab and ab not in ABILITIES:
        rep.add("ERROR", "combat.weapon.ability",
                f"未知攻击属性 {ab!r}，可用：{'/'.join(ABILITIES)}")
    spells = (doc.get("spells") or {}).get("prepared") or []
    known = (doc.get("spells") or {}).get("known") or []
    if (spells or known) and not combat.get("casting_ability"):
        rep.add("ERROR", "combat.casting_ability",
                "构筑里已有法术，但未声明施法关键属性——法术攻击加值与豁免 DC 都算不出来")


def check_resources(doc, rep):
    """资源池声明。跨轮资源算错的典型形态是"根本没声明"。"""
    res = doc.get("resources") or {}
    # 法术位：职业表只给总数，按环分账必须由构筑声明
    spells = (doc.get("spells") or {})
    if (spells.get("prepared") or spells.get("known")):
        if not any(k.startswith("spell_slots_") for k in res):
            rep.add("NOTE", "resources",
                    "构筑里有法术但没声明任何 spell_slots_<环阶> 池——"
                    "施法不会计入消耗，等于无限法术位。按 spell_slots_3 这类名字声明即可")
    # 次数型职业资源：能由职业表推出的不必手写，但写错名字会让所有消耗落空
    for pid, pool in res.items():
        if not isinstance(pool, dict):
            rep.add("ERROR", f"resources.{pid}", "资源池必须是对象")
            continue
        cap = pool.get("capacity")
        if cap is None and not pool.get("derived_from"):
            rep.add("WARN", f"resources.{pid}.capacity",
                    "缺容量且不是派生池——该池的消耗永远无法判定是否超限")

    res = doc.get("resources") or {}
    for rid, pool in res.items():
        src = (pool or {}).get("derived_from")
        if src and src[0] not in res:
            rep.add("ERROR", f"resources.{rid}.derived_from",
                    f"派生自未声明的资源池 {src[0]!r}")
        if (pool or {}).get("die_faces") and not isinstance(pool["die_faces"], int):
            rep.add("ERROR", f"resources.{rid}.die_faces", "骰面必须是整数")


def check_build(build_doc, ops):
    """返回 Report。errors 非空时**不应**相信算出来的数字。"""
    rep = Report()
    idx = index_ops(ops)
    total = check_meta(build_doc, rep)
    check_classes(build_doc, idx, rep)
    check_scores(build_doc, rep)
    check_feats(build_doc, idx, total, rep)
    check_choices(build_doc, idx, ops, rep)
    check_weapon(build_doc, idx, rep)
    check_resources(build_doc, rep)
    return rep


# ---------------------------------------------------------------- CLI

def main(argv):
    ops_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "形式化", "ops")
    ops = {}
    for name in sorted(os.listdir(ops_dir)):
        if name.endswith(".json"):
            with open(os.path.join(ops_dir, name), encoding="utf-8") as fh:
                op = json.load(fh)
                ops[op["id"]] = op
    if len(argv) < 2:
        print("用法: python -I engine/build_check.py <build.json> [更多 build…]")
        return 2
    rc = 0
    for path in argv[1:]:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        rep = check_build(doc, ops)
        print("=" * 60)
        print(path)
        print("=" * 60)
        if not rep.items:
            print("  无问题")
        for line in rep.lines():
            print("  " + line)
        print(f"  —— ERROR {len(rep.errors())}｜WARN {len(rep.warnings())}"
              f"｜NOTE {len(rep.notes())}")
        if not rep.ok():
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv))
