# -*- coding: utf-8 -*-
"""把 index.html 里的内联 JS 抽出来做语法检查（用 node --check），并检查 HTML 基本结构。"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "index.html"
text = HTML.read_text(encoding="utf-8")

scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S)
print(f"内联 script 块数量：{len(scripts)}")

ok = True
for index, code in enumerate(scripts, 1):
    if not code.strip():
        continue
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(code)
        path = handle.name
    result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  [通过] 第 {index} 块 JS 语法正确（{len(code)} 字符）")
    else:
        ok = False
        print(f"  [失败] 第 {index} 块 JS 语法错误：")
        print("    " + (result.stderr or "").strip()[:600])
    Path(path).unlink(missing_ok=True)

print("\n--- 结构检查 ---")
checks = {
    "登录遮罩存在": 'id="auth-overlay"' in text,
    "PDF 预览 iframe 存在": 'id="pdf-frame"' in text,
    "引用跳转函数存在": "function openPage" in text,
    "SSE 流式解析存在": "async function streamRequest" in text,
    "引用标记渲染存在": "cite-chip" in text,
    "本地 tailwind 引用": 'src="vendor/js/tailwind.js"' in text,
    "本地图标引用": 'href="vendor/css/font-awesome.min.css"' in text,
    "已移除 pdf.js 依赖": "pdfjsLib" not in text and "cdnjs" not in text,
    "已移除 CDN tailwind": "cdn.tailwindcss.com" not in text,
    "Bearer 令牌上报": "Authorization" in text,
}
for name, passed in checks.items():
    print(f"  {'[通过]' if passed else '[失败]'} {name}")
    ok = ok and passed

# 标签配对粗检
for tag in ("div", "script", "section", "aside", "button"):
    opened = len(re.findall(rf"<{tag}[\s>]", text))
    closed = len(re.findall(rf"</{tag}>", text))
    flag = "[通过]" if opened == closed else "[失败]"
    if opened != closed:
        ok = False
    print(f"  {flag} <{tag}> 开 {opened} / 闭 {closed}")

print("\n结论：" + ("全部通过" if ok else "存在问题"))
sys.exit(0 if ok else 1)
