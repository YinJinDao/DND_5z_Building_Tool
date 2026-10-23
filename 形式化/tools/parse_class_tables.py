#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解析打击者职业的「职业表」，产出 等级 → 特性 / 资源列 的映射。

用法：
    python parse_class_tables.py [--chm PATH] [--out PATH]

职业表在 HTML 中是规整的 <table>（不是 Word 导出后被压平的文本），可直接按
<tr>/<td> 解析。表头首列固定为「等级」、次列为「特性」，其余列为该职业的资源
曲线（狂暴次数、法术位、气、偷袭、武艺骰……），这些是资源时钟的等级索引输入。

产物同时用于给 extract_classes.py 补全缺失的等级门槛。
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys

RULES_VERSION = "1.63"
STRIKER_CLASSES = ["圣武士", "奇械师", "契术师", "武僧", "法师",
                   "游侠", "灵能武士", "狂战士", "盗贼"]
# 职业表里出现、但不是特性名的占位
NON_FEATURES = {"子职", "属性值提升", "原初斗士", ""}
SPLIT_RE = re.compile(r"[，,、]")
PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
TABLE_RE = re.compile(r"<table[^>]*>.*?</table>", re.S | re.I)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)

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


def cell_text(raw):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw))).strip()


def find_progression_table(text):
    """返回 (表头, 行列表)；找不到则 (None, None)。"""
    for tb in TABLE_RE.findall(text):
        rows = ROW_RE.findall(tb)
        if not rows:
            continue
        head = [cell_text(c) for c in CELL_RE.findall(rows[0])]
        if head and "等级" in head[0]:
            body = [[cell_text(c) for c in CELL_RE.findall(r)] for r in rows[1:]]
            return head, body
    return None, None


def split_features(cell):
    names = []
    for part in SPLIT_RE.split(cell):
        name = PAREN_RE.sub("", part).strip()
        if name and name not in NON_FEATURES:
            names.append(name)
    return names


def parse_class(path, class_name):
    head, body = find_progression_table(read_html(path))
    if head is None:
        return []
    rel = f"职业/{os.path.basename(path)}"
    records = []
    for row in body:
        if not row:
            continue
        try:
            level = int(row[0].strip())
        except (ValueError, IndexError):
            continue
        features = split_features(row[1]) if len(row) > 1 else []
        columns = {head[i]: row[i] for i in range(2, min(len(head), len(row))) if row[i]}
        records.append({
            "id": f"ct.{class_name}.{level}",
            "class": class_name,
            "level": level,
            "features": features,
            "columns": columns,
            "source": {
                "book": "5z", "version": RULES_VERSION,
                "file": rel, "anchor": head[0],
            },
        })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chm", default=os.path.join(REPO, "docs", "5z规则1.63版.chm"))
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "chm"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "class_tables.jsonl"))
    args = ap.parse_args()

    root = decompile(args.chm, args.cache)
    class_root = os.path.join(root, "职业")

    records = []
    for cls in STRIKER_CLASSES:
        path = os.path.join(class_root, f"{cls}.htm")
        if not os.path.isfile(path):
            print(f"跳过（不存在）: {cls}", file=sys.stderr)
            continue
        recs = parse_class(path, cls)
        if not recs:
            print(f"警告：{cls} 未找到职业表", file=sys.stderr)
        records.extend(recs)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_class = {}
    for r in records:
        by_class.setdefault(r["class"], []).append(r["level"])
    print(f"写入 {len(records)} 条 → {args.out}")
    for cls, levels in by_class.items():
        covered = set(levels)
        missing = [n for n in range(1, 21) if n not in covered]
        flag = f" 缺等级 {missing}" if missing else ""
        print(f"  {cls}: {len(levels)} 级{flag}")


if __name__ == "__main__":
    main()
