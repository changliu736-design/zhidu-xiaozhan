# -*- coding: utf-8 -*-
"""检测登录页的动态背景是否真的在运行（用 Edge --dump-dom 读取运行时状态）。

检查三件事：
  1. headless 环境是否命中了 prefers-reduced-motion（会主动停掉动画）
  2. 页面上到底注册了几个动画
  3. 丝带的 transform 是否随时间变化（真在动 vs 静止）
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent.parent
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROBE = ROOT / "vendor" / "probe-anim.html"

html = (ROOT / "index.html").read_text(encoding="utf-8")
html = html.replace('"vendor/', '"/vendor/').replace("'vendor/", "'/vendor/")
html = html.replace("</body>", """
<script>
// 无头浏览器的虚拟时钟不驱动 CSS 动画，所以这里手动把时间轴推到不同位置再读样式，
// 以此证明动画确实会产生位移，而不是只有一条静止的声明。
setTimeout(function () {
  var anims = document.getAnimations();
  var samples = {};
  anims.forEach(function (a, i) {
    var target = a.effect && a.effect.target;
    if (!target) return;
    var duration = a.effect.getTiming().duration || 0;
    a.pause();
    a.currentTime = 0;
    var start = getComputedStyle(target).transform;
    a.currentTime = duration / 2;
    var middle = getComputedStyle(target).transform;
    a.currentTime = duration;
    var end = getComputedStyle(target).transform;
    samples[a.animationName + '#' + i] = {
      duration: duration, start: start, middle: middle, end: end,
      moved: (start !== middle) || (middle !== end)
    };
  });
  var info = {
    reduced: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    count: anims.length,
    samples: samples
  };
  // 记录每个元素实际生效的周期，用来确认 JS 的随机化确实起了作用
  var durations = {};
  document.querySelectorAll('.aurora-base, .sweep, .orb, .ribbon').forEach(function (node) {
    var name = (node.className && node.className.baseVal !== undefined)
      ? node.className.baseVal : String(node.className);
    durations[name] = getComputedStyle(node).animationDuration;
  });
  info.durations = durations;
  var node = document.createElement('pre');
  node.id = 'ZD_PROBE';
  node.textContent = 'ZD_PROBE_JSON:' + JSON.stringify(info);
  document.body.appendChild(node);
}, 1000);
</script>
</body>""", 1)
PROBE.write_text(html, encoding="utf-8")


def dump(budget: int = 4000) -> dict:
    result = subprocess.run(
        [EDGE, "--headless=new", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={Path.home() / 'AppData/Local/Temp/edgeprobe'}",
         "--window-size=1440,900", f"--virtual-time-budget={budget}",
         "--dump-dom", "http://127.0.0.1:5000/vendor/probe-anim.html"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    match = re.search(r"ZD_PROBE_JSON:(\{.*?\})</pre>", result.stdout or "", re.S)
    if not match:
        print("  没拿到探针数据，DOM 片段：", (result.stdout or "")[:300])
        return {}
    return json.loads(match.group(1))


info = dump()
print(f"系统「减少动态效果」= {info.get('reduced')}")
print(f"页面活跃动画数量 = {info.get('count')}\n")

moving, still = 0, 0
print(f"{'动画名':<16}{'周期':>8}   是否产生位移")
print("-" * 56)
for name, sample in (info.get("samples") or {}).items():
    flag = "✓ 在动" if sample["moved"] else "✗ 静止"
    if sample["moved"]:
        moving += 1
    else:
        still += 1
    print(f"{name.split('#')[0]:<16}{sample['duration'] / 1000:>6.0f}s   {flag}")

print("\n=== 结论 ===")
if info.get("reduced"):
    print("  ⚠ 无头浏览器命中了「减少动态效果」，动画被主动关闭（真实浏览器不会）")
elif moving and not still:
    print(f"  ✓ {moving} 个动画全部会在时间轴上产生位移 → 登录页背景确实是动态的")
elif moving:
    print(f"  ✓ {moving} 个动画在动，{still} 个未检测到位移")
else:
    print("  ✗ 没有检测到任何位移，样式可能没生效")

# 顺带把丝带在 0% / 50% / 100% 三个时刻的矩阵打出来，直观看到它在移动
for name, sample in (info.get("samples") or {}).items():
    if name.startswith("ribbonFlow"):
        print(f"\n  丝带 {name.split('#')[1]} 号随时间的位置：")
        for label, key in (("起点", "start"), ("中段", "middle"), ("终点", "end")):
            print(f"    {label}: {sample[key]}")
        break

# 随机化检查
durations = info.get("durations") or {}
print("\n=== 每次刷新随机化 ===")
print(f"  采样到 {len(durations)} 个背景元素的周期：")
values = []
for name, value in sorted(durations.items()):
    print(f"    {name:<14} {value}")
    values.append(value)
unique = len(set(values))
if not values:
    print("  ✗ 没取到周期")
elif unique == len(values):
    print(f"  ✓ {len(values)} 个元素周期两两不同 → 随机化生效")
elif unique > 1:
    print(f"  ✓ {unique}/{len(values)} 个周期不同 → 随机化生效")
else:
    print("  ✗ 所有周期完全相同，随机化没生效")

PROBE.unlink(missing_ok=True)
