#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽取打击者职业的**职业能力**（职业特性 + 子职特性），生成 A 档数据。

用法：
    python extract_classes.py [--chm PATH] [--out PATH]

范围：仅规则书标注为「打击者」定位的 9 个玩家职业——
    圣武士、奇械师、契术师、武僧、法师、游侠、灵能武士、狂战士、盗贼

职业页没有模板字段，特性名与专长一样靠**版式**识别（整段 14pt 粗体）。
等级门槛优先从正文的「第 X 级起/时」提取；提取不到则留空，由职业表补（见文件末尾说明）。
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys

RULES_VERSION = "1.63"
# 全部玩家职业。早期只覆盖规则书标注「打击者」的 9 个，2026-10-23 按协作者意见
# 扩到全部 14 个——打击者构筑可以兼职，且牧师/德鲁伊等也有纯输出的子职。
# 召唤物相关能力一律不建模（协作者明确排除，复杂度不可控）。
PLAYER_CLASSES = ["圣武士", "奇械师", "契术师", "武僧", "法师", "游侠",
                  "灵能武士", "狂战士", "盗贼",                      # 标注「打击者」
                  "牧师", "德鲁伊", "吟游诗人", "灵能师", "召唤师"]  # 其余玩家职业
STRIKER_CLASSES = PLAYER_CLASSES[:9]
# 非特性的一般性小节标题，不进入特性库
SKIP_HEADINGS = {"定位", "生命值", "熟练项", "兼职", "子职", "职业法术",
                 "施法", "法术列表", "职业表", "简介", "概述",
                 "先决条件", "附录", "目录", "行为准则", "门派戒律"}
SKILL_RE = re.compile(r"^(力量|敏捷|体质|智力|感知|魅力)（[^）]+）$")


def is_non_feature(name):
    """职业页里混在特性章节中的非特性标题。

    - 「XX职业表」等表格标题
    - 「附录」「目录」等版面标题
    - 「敏捷（体操）」这类技能熟练列表项
    - 「通用祈唤」「刃之魔契专属祈唤」这类选项分组标题
    - 「战士职业特性替换」这类变体说明标题
    """
    if name in SKIP_HEADINGS:
        return True
    if name.endswith("职业表"):
        return True
    if name.endswith("祈唤") or name.endswith("职业特性替换"):
        return True
    if SKILL_RE.match(name):
        return True
    return False
MAX_NAME_LEN = 24
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

BOLD14 = re.compile(
    r'<span[^>]*font-weight:bold[^>]*font-size:14\.0000pt[^>]*>(.*?)</span>'
    r'|<span[^>]*font-size:14\.0000pt[^>]*font-weight:bold[^>]*>(.*?)</span>',
    re.S | re.I,
)
PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S | re.I)
LEVEL_RE = re.compile(r"第\s*([一二三四五六七八九十]+|\d+)\s*级")
SENTENCE_PUNCT = re.compile(r"[，。；：、！？]")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def decompile(chm_path, cache_dir):
    marker = os.path.join(cache_dir, "职业")
    if os.path.isdir(marker):
        return cache_dir
    os.makedirs(cache_dir, exist_ok=True)
    if not os.path.isfile(chm_path):
        sys.exit(f"CHM 不存在: {chm_path}")
    try:
        subprocess.run(
            ["hh.exe", "-decompile", os.path.abspath(cache_dir), os.path.abspath(chm_path)],
            check=True,
        )
    except FileNotFoundError:
        sys.exit("未找到 hh.exe：请在 Windows 环境运行，或手动解包后指定 --cache")
    return cache_dir


def read_html(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gb18030", "ignore")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def paragraphs(text):
    out = []
    for raw in PARA.findall(text):
        plain = strip_tags(raw)
        if not plain:
            continue
        bold_plain = strip_tags("".join(a or b for a, b in BOLD14.findall(raw)))
        is_name = (
            bool(bold_plain)
            and bold_plain == plain
            and len(plain) <= MAX_NAME_LEN
            and not SENTENCE_PUNCT.search(plain)
        )
        out.append((is_name, plain))
    return out


def parse_level(body):
    m = LEVEL_RE.search(body)
    if not m:
        return None
    raw = m.group(1)
    return int(raw) if raw.isdigit() else CN_NUM.get(raw)


def walk_class(job_dir, class_name, subclass_dir):
    """主职业页在 职业/<职业>.htm；子职页在 职业/<职业>/<子职>.htm。

    两者都要扫——主职特性（狂暴、鲁莽攻击、额外攻击……）在主职页上，
    只扫子目录会把它们整批漏掉。
    """
    records = []
    targets = []
    main = os.path.join(job_dir, f"{class_name}.htm")
    if os.path.isfile(main):
        targets.append((main, None))
    if subclass_dir and os.path.isdir(subclass_dir):
        for root, _, files in os.walk(subclass_dir):
            for f in sorted(files):
                if f.endswith(".htm"):
                    targets.append((os.path.join(root, f), f[:-4]))

    for path, subclass in targets:
        paras = paragraphs(read_html(path))
        starts = [i for i, (is_name, _) in enumerate(paras) if is_name]
        for n, idx in enumerate(starts):
            name = paras[idx][1]
            if is_non_feature(name):
                continue
            end = starts[n + 1] if n + 1 < len(starts) else len(paras)
            body = [t for _, t in paras[idx + 1:end]]
            full = "\n".join(body)
            rel = os.path.relpath(path, os.path.dirname(job_dir)).replace(os.sep, "/")
            records.append({
                "id": f"cf.{class_name}.{name}",
                "name": name,
                "class": class_name,
                "subclass": subclass,
                "level": parse_level(full),
                "is_variant": name.startswith("玩家可选变体"),
                "body": full,
                # 职业特性同样以规则正文为主，首段未必是风味。effect 取完整正文。
                "flavor_hint": body[0] if body else None,
                "effect": full,
                "source": {
                    "book": "5z",
                    "version": RULES_VERSION,
                    "file": f"职业/{rel}",
                    "anchor": name,
                },
                "status": "extracted",
            })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chm", default=os.path.join(REPO, "docs", "5z规则1.63版.chm"))
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "chm"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "class_features.jsonl"))
    args = ap.parse_args()

    root = decompile(args.chm, args.cache)
    records = []
    class_root = os.path.join(root, "职业")
    for cls in PLAYER_CLASSES:
        sub_dir = os.path.join(class_root, cls)
        main_htm = os.path.join(class_root, f"{cls}.htm")
        if not os.path.isdir(sub_dir) and not os.path.isfile(main_htm):
            print(f"跳过（不存在）: {cls}", file=sys.stderr)
            continue
        records.extend(walk_class(class_root, cls, sub_dir if os.path.isdir(sub_dir) else None))

    merged = {}
    for rec in records:
        key = rec["id"]
        if key in merged:
            merged[key]["sources"].append(rec["source"])
            if merged[key].get("level") is None and rec.get("level") is not None:
                merged[key]["level"] = rec["level"]
        else:
            rec["sources"] = [rec["source"]]
            merged[key] = rec
    final = list(merged.values())

    # 正文没写「第 X 级起」的特性，用职业表（parse_class_tables.py 的产物）补门槛。
    table_path = os.path.join(os.path.dirname(args.out), "class_tables.jsonl")
    filled = 0
    conflicts = 0
    if os.path.isfile(table_path):
        index = {}
        for line in open(table_path, encoding="utf-8"):
            row = json.loads(line)
            for feat in row["features"]:
                index.setdefault((row["class"], feat), row["level"])
        for rec in final:
            lvl = index.get((rec["class"], rec["name"]))
            if lvl is not None:
                # 职业表是权威来源：即使正文已推出等级，也以表为准，并记录差异
                if rec.get("level") is not None and rec["level"] != lvl:
                    rec["level_conflict"] = {"text": rec["level"], "table": lvl}
                    conflicts += 1
                rec["level"] = lvl
                rec["level_source"] = "class_table"
                if rec.get("needs_review"):
                    rec.pop("needs_review")
                if rec.get("level") == lvl and rec["level_source"] == "class_table":
                    filled += 1
            elif rec.get("level") is None:
                # 正文与职业表都没有给出门槛，进人工复核队列
                rec["needs_review"] = True
    else:
        print(f"提示：未找到 {table_path}，跳过职业表补全（先运行 parse_class_tables.py）",
              file=sys.stderr)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for rec in final:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_class = {}
    for r in final:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
    no_level = sum(1 for r in final if r["level"] is None)
    print(f"写入 {len(final)} 条 → {args.out}")
    print("按职业:", by_class)
    print(f"以职业表为准的条目: {filled} 条（其中与正文冲突 {conflicts} 条）")
    print(f"仍未提取到等级门槛: {no_level} / {len(final)}")


if __name__ == "__main__":
    main()
