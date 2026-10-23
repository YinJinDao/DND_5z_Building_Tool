#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 CHM 内的 5z1.49法术词典.xlsx 抽取法术条目，生成 A 档结构化数据。

用法：
    python extract_spells.py [--chm PATH] [--out PATH]

注意版本落差：该 xlsx 的文件名为 **1.49** 版，而规则书为 1.63 版。
xlsx 只是快速查询表，权威正文在 CHM 的 `法术/` 目录。本脚本抽取的是 xlsx，
每条记录带 `source.note` 标注此风险；与 `法术/*.htm` 的一致性校验留待 B 档。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from xml.etree import ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XLSX_NAME = "5z1.49法术词典.xlsx"
SHEET_NAME = "法术词典"

# xlsx 列位（见 docs 中该表的表头行）
COL = {
    "name": "A", "level": "B", "ritual": "C", "school": "D",
    "casting_time": "E", "range": "F", "target": "G", "duration": "H",
    "concentration": "I", "components": "J", "effect": "K",
}
CLASS_COLS = list("LMNOPQRSTU")  # 圣武士 游侠 灵能武士 奇械师 契术师 吟游诗人 牧师 德鲁伊 灵能师 法师

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def decompile(chm_path, cache_dir):
    marker = os.path.join(cache_dir, XLSX_NAME)
    if os.path.isfile(marker):
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
    if not os.path.isfile(marker):
        sys.exit(f"解包后未找到 {XLSX_NAME}")
    return cache_dir


def col_of(ref):
    return re.match(r"([A-Z]+)", ref).group(1)


def read_sheet(xlsx_path, sheet_name):
    """返回 {列字母: 值} 的行列表。"""
    zf = zipfile.ZipFile(xlsx_path)
    shared = []
    if "xl/sharedStrings.xml" in zf.namelist():
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in root.findall(NS + "si"):
            shared.append("".join(t.text or "" for t in si.iter(NS + "t")))

    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    names = [s.get("name") for s in wb.iter(NS + "sheet")]
    if sheet_name not in names:
        sys.exit(f"找不到工作表 {sheet_name}，现有：{names}")
    idx = names.index(sheet_name) + 1

    sheet = ET.fromstring(zf.read(f"xl/worksheets/sheet{idx}.xml"))
    rows = []
    for row in sheet.iter(NS + "row"):
        cells = {}
        for c in row.findall(NS + "c"):
            v = c.find(NS + "v")
            if v is None:
                continue
            val = shared[int(v.text)] if c.get("t") == "s" else v.text
            if val is not None:
                cells[col_of(c.get("r"))] = val
        if cells:
            rows.append(cells)
    return rows


def flag(raw):
    """'-' / 空 → False；其余视为 True。"""
    if raw is None:
        return None
    return str(raw).strip() not in ("-", "", "None", "无")


def num(raw):
    try:
        f = float(raw)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chm", default=os.path.join(REPO, "docs", "5z规则1.63版.chm"))
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "chm"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "spells.jsonl"))
    args = ap.parse_args()

    root = decompile(args.chm, args.cache)
    rows = read_sheet(os.path.join(root, XLSX_NAME), SHEET_NAME)
    if not rows:
        sys.exit("工作表为空")

    header = rows[0]
    class_names = {c: header.get(c) for c in CLASS_COLS if header.get(c)}

    records = []
    for r in rows[1:]:
        name = (r.get("A") or "").strip()
        if not name:
            continue
        rec = {"id": "sp." + re.sub(r"\s+", "", name), "name": name}
        for key, col in COL.items():
            if key == "name":
                continue
            val = r.get(col)
            rec[key] = val.strip() if isinstance(val, str) else val
        rec["level"] = num(rec.get("level"))
        rec["ritual"] = flag(rec.get("ritual"))
        rec["concentration"] = flag(rec.get("concentration"))
        avail = {}
        for col, cname in class_names.items():
            raw = r.get(col)
            if raw is None:
                continue
            v = num(raw)
            if v:
                avail[cname] = v
        rec["class_lists"] = avail
        rec["source"] = {
            "book": "5z",
            "file": XLSX_NAME,
            "sheet": SHEET_NAME,
            "note": "xlsx 为 1.49 版查询表，权威正文在 CHM 法术/ 目录，版本一致性待校验",
        }
        rec["status"] = "extracted"
        records.append(rec)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    no_level = [r["name"] for r in records if r["level"] is None]
    print(f"写入 {len(records)} 条 → {args.out}")
    if no_level:
        print(f"警告：{len(no_level)} 条无环阶: {no_level[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
