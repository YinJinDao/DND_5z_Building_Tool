#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 CHM 的「法术/法术详述/<N>环.htm」抽取法术条目，生成 A 档结构化数据。

用法：
    python extract_spells.py [--chm PATH] [--out PATH]

**以规则书正文为准**，不使用 `5z1.49法术词典.xlsx`（该表为 1.49 版，落后于规则书 1.63，
且只是快速查询表）。规则书正文的模板比 xlsx 更规整：

    剑刃防护Blade Ward          ← 名称 + 英文名
    戏法 防护                    ← 环阶行：戏法 | 一~九环，后接学派，可带（仪式）
    施法时间：1动作
    施法距离：自身
    法术目标：无
    法术成分：V、S
    持续时间：一轮               ← 专注以"专注，至多 X"的形式写在持续时间里
    [效果正文 ...]
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys

RULES_VERSION = "1.63"
DETAIL_DIR = os.path.join("法术", "法术详述")
LEVEL_FILES = [f"{n}环.htm" for n in range(0, 10)]
FIELD_NAMES = ("施法时间", "施法距离", "法术目标", "法术成分", "持续时间")
HEADER_RE = re.compile(r"^(戏法|[一二三四五六七八九]环)\s+(\S+?)(（仪式）)?$")
CN_LEVEL = {"戏法": 0, "一环": 1, "二环": 2, "三环": 3, "四环": 4,
            "五环": 5, "六环": 6, "七环": 7, "八环": 8, "九环": 9}
NAME_EN_RE = re.compile(r"^(.*?)([A-Za-z][A-Za-z\s'’/,.-]*)$")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def decompile(chm_path, cache_dir):
    marker = os.path.join(cache_dir, DETAIL_DIR)
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


def to_lines(text):
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table|br)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return [ln.strip() for ln in html.unescape(text).split("\n") if ln.strip()]


def _has_concentration(duration, effect):
    return "专注" in (duration or "") or "终止专注" in (effect or "") or "专注" in (effect or "") and "维持专注" in (effect or "")


def split_name_en(raw):
    """'剑刃防护Blade Ward' → ('剑刃防护', 'Blade Ward')。"""
    m = NAME_EN_RE.match(raw)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    return raw.strip(), None


def parse_file(path, level_from_file):
    lines = to_lines(read_html(path))
    starts = [i for i in range(len(lines) - 1) if HEADER_RE.match(lines[i + 1])]
    records = []
    for n, idx in enumerate(starts):
        name, name_en = split_name_en(lines[idx])
        m = HEADER_RE.match(lines[idx + 1])
        level_txt, school, ritual = m.group(1), m.group(2), bool(m.group(3))

        fields = {}
        j = idx + 2
        while j < len(lines):
            hit = None
            for fld in FIELD_NAMES:
                if lines[j].startswith(fld + "："):
                    fields[fld] = lines[j][len(fld) + 1:].strip()
                    hit = fld
                    break
            if hit is None:
                break
            j += 1

        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        body = lines[j:end]

        records.append({
            "id": f"sp.{name}",
            "name": name,
            "name_en": name_en,
            "level": CN_LEVEL.get(level_txt, level_from_file),
            "school": school,
            "ritual": ritual,
            "casting_time": fields.get("施法时间"),
            "range": fields.get("施法距离"),
            "target": fields.get("法术目标"),
            "components": fields.get("法术成分"),
            "duration": fields.get("持续时间"),
            # 专注可能写在「持续时间」（专注，至多1分钟）也可能只写在正文
            # （如「直到你终止专注」）。只看持续时间会漏掉后者——实测漏了约 20 条。
            "concentration": _has_concentration(fields.get("持续时间"), "\n".join(body)),
            "effect": "\n".join(body),
            "source": {
                "book": "5z",
                "version": RULES_VERSION,
                "file": f"{DETAIL_DIR.replace(os.sep, '/')}/{os.path.basename(path)}",
                "anchor": name,
            },
            "status": "extracted",
        })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chm", default=os.path.join(REPO, "docs", "5z规则1.63版.chm"))
    ap.add_argument("--cache", default=os.path.join(REPO, ".cache", "chm"))
    ap.add_argument("--out", default=os.path.join(REPO, "形式化", "data", "spells.jsonl"))
    args = ap.parse_args()

    root = decompile(args.chm, args.cache)
    detail = os.path.join(root, DETAIL_DIR)

    records = []
    for n, fname in enumerate(LEVEL_FILES):
        path = os.path.join(detail, fname)
        if not os.path.isfile(path):
            print(f"跳过（不存在）: {fname}", file=sys.stderr)
            continue
        records.extend(parse_file(path, n))

    # 同名法术可能重复收录，按 id 去重并保留全部 source
    merged = {}
    for rec in records:
        if rec["id"] in merged:
            merged[rec["id"]]["sources"].append(rec["source"])
        else:
            rec["sources"] = [rec["source"]]
            merged[rec["id"]] = rec
    final = list(merged.values())

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for rec in final:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_level = {}
    for r in final:
        by_level[r["level"]] = by_level.get(r["level"], 0) + 1
    print(f"写入 {len(final)} 条（原始 {len(records)}，去重 {len(records) - len(final)}）→ {args.out}")
    print("环阶分布:", dict(sorted(by_level.items())))
    no_ct = [r["name"] for r in final if not r["casting_time"]]
    if no_ct:
        print(f"警告：{len(no_ct)} 条无施法时间: {no_ct[:5]}", file=sys.stderr)


if __name__ == "__main__":
    main()
