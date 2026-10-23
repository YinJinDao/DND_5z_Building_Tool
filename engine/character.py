#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""角色基础数值：从「购点 + 种族/升级/专长/装备加值」推导出可计算的派生值。

对应角色卡模板的分栏（初始 / 种族+ / 升级+ / 专长+ / 装备+）。

**所有派生值都保留构成项**，因此命中加值可追溯到每一项来源——
`整体目标` §5 要求"能说明分数来自哪些假设与计算"，不可追溯的裸数字不满足这一条。

公式出处（规则书 v1.63）：
  熟练加值   「基础/创建角色与升级」：1 级 +2，此后每 4 级 +1
  属性调整值 (属性值 − 10) 向下取整除以 2（与 5z计算器.xlsx 的购点表一致）
  无甲 AC    「基础/创建角色与升级」：8 + 熟练加值 + 敏捷调整值
  各类护甲   「范例角色卡/5z角色卡空白模板」
  生命值     10 + 职业等级 ×（生命骰期望 + 体质调整值）
"""

from dataclasses import dataclass, field

ABILITIES = ("str", "dex", "con", "int", "wis", "cha")
ABILITY_CN = {"str": "力量", "dex": "敏捷", "con": "体质",
              "int": "智力", "wis": "感知", "cha": "魅力"}


def ability_mod(score):
    """(属性值 − 10) 向下取整除以 2。"""
    return (score - 10) // 2


def proficiency_bonus(level):
    """1 级 +2，5 级 +3，9 级 +4，13 级 +5，17 级 +6……"""
    return 2 + (level - 1) // 4


# ---------------------------------------------------------------- 可追溯的加值

@dataclass
class Contribution:
    source: str
    value: int

    def __str__(self):
        return f"{self.source}{self.value:+d}"


@dataclass
class Bonus:
    """由多项来源构成的加值。可追溯是硬要求，不是可选项。"""
    parts: list = field(default_factory=list)

    def add(self, source, value):
        if value:
            self.parts.append(Contribution(source, value))
        return self

    @property
    def total(self):
        return sum(p.value for p in self.parts)

    def explain(self):
        if not self.parts:
            return "+0"
        return " + ".join(str(p) for p in self.parts) + f" = {self.total:+d}"

    def __int__(self):
        return self.total


# ---------------------------------------------------------------- 护甲与生命值

# 均为「不含盾牌」的基础 AC
ARMOR_TYPES = ("none", "light", "medium", "heavy", "mage_armor",
               "monk_unarmored", "barbarian_unarmored")
ARMOR_CN = {
    "none": "未着装", "light": "轻甲", "medium": "中甲", "heavy": "重甲",
    "mage_armor": "法师护甲", "monk_unarmored": "武僧无甲防御",
    "barbarian_unarmored": "狂战士无甲防御",
}


def armor_class(armor, prof, dex_mod, con_mod=0, wis_mod=0, shield=False):
    """按护甲类型计算 AC。5z 的 AC 一律含熟练加值（与 5e 不同）。"""
    if armor == "none":
        base = 8 + prof + dex_mod
    elif armor == "light":
        base = 10 + prof + dex_mod
    elif armor == "medium":
        base = 13 + prof + min(dex_mod, 2)
    elif armor == "heavy":
        base = 16 + prof
    elif armor == "mage_armor":
        base = 11 + prof + dex_mod
    elif armor == "monk_unarmored":
        base = 8 + prof + dex_mod + wis_mod
    elif armor == "barbarian_unarmored":
        base = 8 + prof + dex_mod + con_mod
    else:
        raise ValueError(f"未知护甲类型: {armor}")
    return base + (2 if shield else 0)


def max_hp(level, hit_die_faces, con_mod):
    """10 + 职业等级 ×（生命骰期望 + 体质调整值）。"""
    return 10 + level * ((hit_die_faces + 1) // 2 + con_mod)


# ---------------------------------------------------------------- 构筑 → 派生

@dataclass
class Build:
    """最小构筑输入。完整 Build（含职业/专长选择）待 resolver 层实现。"""
    level: int = 1
    base_scores: dict = field(default_factory=dict)      # 购点基础值
    racial: dict = field(default_factory=dict)           # 种族加值
    level_ups: dict = field(default_factory=dict)        # 升级加值（每3级主要+1 等）
    feat_bonus: dict = field(default_factory=dict)       # 专长加值
    item_bonus: dict = field(default_factory=dict)       # 装备加值
    hit_die_faces: int = 8
    armor: str = "none"
    shield: bool = False
    proficient_saves: tuple = ()
    weapon_attack_ability: str = "str"
    weapon_enhancement: int = 0
    misc_attack_bonus: list = field(default_factory=list)  # [(来源, 值)]
    casting_ability: str | None = None      # 施法关键属性；无施法则为 None


@dataclass
class Derived:
    level: int
    scores: dict
    mods: dict
    prof: int
    ac: int
    hp: int
    saves: dict
    attack: Bonus
    spell_attack: Bonus | None = None
    spell_dc: int | None = None

    def summary(self):
        lines = [f"等级 {self.level}｜熟练加值 +{self.prof}｜AC {self.ac}｜HP {self.hp}"]
        for a in ABILITIES:
            lines.append(f"  {ABILITY_CN[a]} {self.scores[a]}（{self.mods[a]:+d}）")
        lines.append("  豁免：" + "  ".join(
            f"{ABILITY_CN[a]}{self.saves[a]:+d}" for a in ABILITIES))
        lines.append(f"  攻击加值：{self.attack.explain()}")
        return "\n".join(lines)


def derive(build: Build) -> Derived:
    scores = {}
    for a in ABILITIES:
        scores[a] = (build.base_scores.get(a, 10)
                     + build.racial.get(a, 0)
                     + build.level_ups.get(a, 0)
                     + build.feat_bonus.get(a, 0)
                     + build.item_bonus.get(a, 0))
    mods = {a: ability_mod(scores[a]) for a in ABILITIES}
    prof = proficiency_bonus(build.level)

    ac = armor_class(build.armor, prof, mods["dex"],
                     con_mod=mods["con"], wis_mod=mods["wis"], shield=build.shield)
    hp = max_hp(build.level, build.hit_die_faces, mods["con"])

    saves = {a: mods[a] + (prof if a in build.proficient_saves else 0) for a in ABILITIES}

    atk = Bonus()
    atk.add(f"{ABILITY_CN[build.weapon_attack_ability]}调整值",
            mods[build.weapon_attack_ability])
    atk.add("熟练加值", prof)
    atk.add("武器增强", build.weapon_enhancement)
    for src, val in build.misc_attack_bonus:
        atk.add(src, val)

    # 施法数值：法术攻击加值 = 熟练加值 + 施法关键属性调整值
    #             法术豁免 DC   = 8 + 熟练加值 + 施法关键属性调整值
    spell_atk = spell_dc = None
    if build.casting_ability:
        cast_mod = mods[build.casting_ability]
        spell_atk = Bonus()
        spell_atk.add(f"{ABILITY_CN[build.casting_ability]}调整值", cast_mod)
        spell_atk.add("熟练加值", prof)
        spell_dc = 8 + prof + cast_mod

    return Derived(level=build.level, scores=scores, mods=mods, prof=prof,
                   ac=ac, hp=hp, saves=saves, attack=atk,
                   spell_attack=spell_atk, spell_dc=spell_dc)
