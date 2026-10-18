# -*- coding: utf-8 -*-
"""查看当前各账号的文献与索引状态（只读，不修改任何东西）。"""
import glob
import json
import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS = os.path.join(ROOT, "backend", "uploads")
DATA = os.path.join(ROOT, "backend", "data")

users = {}
path = os.path.join(DATA, "users.json")
if os.path.exists(path):
    with open(path, encoding="utf-8") as handle:
        users = {v["id"]: v["username"] for v in json.load(handle).values()}

print(f"已注册账号：{list(users.values()) or '无'}\n")

for folder in sorted(glob.glob(os.path.join(UPLOADS, "*"))):
    if not os.path.isdir(folder):
        continue
    name = users.get(os.path.basename(folder), "（无主）")
    print(f"【{name}】{os.path.basename(folder)}")
    infos = sorted(glob.glob(os.path.join(folder, "*_info.json")))
    if not infos:
        print("    （空账号，没有文献）")
    for info_path in infos:
        with open(info_path, encoding="utf-8") as handle:
            info = json.load(handle)
        chunks = info.get("chunks") or info.get("text_chunks") or []
        doc_id = info.get("id", "?")
        index_file = os.path.join(folder, f"{doc_id}_index.faiss")
        print(f"    · {info.get('original_filename', '?')}")
        print(f"      schema v{info.get('schema_version')} | "
              f"{info.get('page_count')} 页 | {len(chunks)} 段 | "
              f"索引={info.get('index_status')} | 错误={info.get('index_error')}")
        # 打印接口实际会返回的字段，确认老文档也能正确显示"页"而不是空白
        try:
            sys.path.insert(0, os.path.join(ROOT, "backend"))
            from documents import summary_of
            view = summary_of(info)
            print(f"      接口视图: unit={view['unit']} source_type={view['source_type']} "
                  f"page_count={view['page_count']} chunk_count={view['chunk_count']}")
        except Exception as exc:  # noqa: BLE001
            print(f"      接口视图计算失败: {exc}")
        if os.path.exists(index_file):
            print(f"      索引文件 {os.path.getsize(index_file) / 1024:.0f} KB")
        else:
            print("      ⚠ 索引文件不存在")
    for pdf in sorted(glob.glob(os.path.join(folder, "*.pdf"))):
        print(f"      原文 {os.path.basename(pdf)}  {os.path.getsize(pdf) / 1024 / 1024:.1f} MB")
    print()
