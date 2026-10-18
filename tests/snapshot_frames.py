# -*- coding: utf-8 -*-
"""把动画暂停到指定时刻再截图，用来直观对比不同时间点的画面。

无头浏览器的虚拟时钟不驱动 CSS 动画，所以这里直接操作 Web Animations 的时间轴。
"""
import subprocess
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent.parent
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROBE = ROOT / "vendor" / "probe-frame.html"
SHOTS = ROOT / "docs" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)

html = (ROOT / "index.html").read_text(encoding="utf-8")
html = html.replace('"vendor/', '"/vendor/').replace("'vendor/", "'/vendor/")
html = html.replace("</body>", """
<script>
// #t=秒数 ：把所有动画停在该时刻，方便逐帧对比
setTimeout(function () {
  var t = parseFloat((location.hash.match(/t=([\\d.]+)/) || [0, 0])[1]) || 0;
  document.getAnimations().forEach(function (a) {
    a.pause();
    a.currentTime = t * 1000;
  });
}, 600);
</script>
</body>""", 1)
PROBE.write_text(html, encoding="utf-8")

frames = [0.4, 3.0, 6.5]
for index, moment in enumerate(frames, 1):
    target = SHOTS / f"frame-{index}.png"
    subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={Path.home() / 'AppData/Local/Temp/edgeframe'}",
         "--window-size=1440,900", "--virtual-time-budget=4000",
         f"--screenshot={target}",
         f"http://127.0.0.1:5000/vendor/probe-frame.html#t={moment}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    size = target.stat().st_size if target.exists() else 0
    print(f"  第 {index} 帧 t={moment}s → {target.name}  {size / 1024:.0f} KB")

# 用哈希对比三帧是否真的不同
import hashlib

hashes = []
for index in range(1, len(frames) + 1):
    path = SHOTS / f"frame-{index}.png"
    hashes.append(hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else "")
print(f"\n三帧是否互不相同：{'是（画面确实在变）' if len(set(hashes)) == len(hashes) else '否'}")

PROBE.unlink(missing_ok=True)
