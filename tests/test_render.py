# -*- coding: utf-8 -*-
"""从 index.html 里抽出真实的渲染函数，交给 node 跑断言。

验证的就是页面上跑的那份代码，不是副本。
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).resolve().parent.parent
text = (ROOT / "index.html").read_text(encoding="utf-8")


def extract(name: str, source: str) -> str:
    """按大括号配对，抽出 function name(...) {...} 的完整定义。"""
    match = re.search(rf"function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    if not match:
        raise SystemExit(f"没找到函数 {name}")
    start = match.start()
    index = match.end() - 1
    depth = 0
    while index < len(source):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
        index += 1
    raise SystemExit(f"函数 {name} 大括号不配对")


def extract_const(name: str, source: str) -> str:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*\[.*?\n\];", source, re.S)
    if not match:
        raise SystemExit(f"没找到常量 {name}")
    return match.group(0)


pieces = [
    extract_const("SECTION_ICONS", text),
    extract("escapeHtml", text),
    extract("renderRich", text),
    extract("sectionIcon", text),
    extract("splitSections", text),
]

TEST = r"""
const document = {
  createElement() {
    let value = '';
    return {
      set textContent(v) { value = String(v); },
      get innerHTML() {
        return value.replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
      },
    };
  },
};

__PIECES__

let failed = 0;
function check(name, condition, detail) {
  console.log((condition ? '  [通过] ' : '  [失败] ') + name + (detail ? '  — ' + detail : ''));
  if (!condition) failed++;
}

console.log('--- 引用标记渲染 renderRich ---');
const rich = renderRich('实验在三个数据集上验证了有效性 [[p.3]]，消融实验也支持该结论 [[p.12]]。');
check('生成可点击出处按钮', (rich.match(/class="cite-chip"/g) || []).length === 2);
check('编号写入 data-page', rich.includes('data-page="3"') && rich.includes('data-page="12"'));
check('原始标记已被替换', !rich.includes('[[p.'));
check('按钮显示编号+单位', rich.includes('>3页<') && rich.includes('>12页<'));
check('支持空格写法 [[p. 7]]', renderRich('看这里 [[p. 7]]').includes('data-page="7"'));

// 文本类文档（txt/md/docx）的单位应当是"段"
currentUnit = '段';
const richText = renderRich('结论见 [[p.5]]。');
check('文本类文档显示"段"', richText.includes('>5段<') && richText.includes('第 5 段'));
currentUnit = '页';

console.log('--- XSS 防护 escapeHtml ---');
const evil = renderRich('<img src=x onerror=alert(1)> [[p.1]]');
check('尖括号被转义', !evil.includes('<img') && evil.includes('&lt;img'));
check('转义后仍能生成引用按钮', evil.includes('data-page="1"'));

console.log('--- 摘要分节 splitSections ---');
const summary = '## 研究背景\n该研究关注医学图像分割[[p.1]]。\n## 研究方法\n提出了 RecLMIS[[p.4]]。\n## 关键词\n跨模态、医学图像分割、条件重建';
const sections = splitSections(summary);
check('切出 3 个小节', sections.length === 3, JSON.stringify(sections.map(s => s.title)));
check('小节标题正确', sections[0].title === '研究背景' && sections[1].title === '研究方法');
check('正文归属正确', sections[0].body.includes('[[p.1]]') && sections[1].body.includes('[[p.4]]'));
check('关键词被单独切出', sections[2].title === '关键词');

console.log('--- 图标匹配 sectionIcon ---');
check('研究背景 → info 图标', sectionIcon('研究背景') === 'fa-info-circle');
check('研究方法 → flask 图标', sectionIcon('研究方法') === 'fa-flask');
check('研究结果 → 柱状图', sectionIcon('研究结果') === 'fa-bar-chart');
check('关键词 → 标签图标', sectionIcon('关键词') === 'fa-tags');
check('未知标题有兜底图标', sectionIcon('随便什么') === 'fa-file-text-o');

console.log('--- 流式未完成时的半截文本 ---');
const partial = splitSections('## 研究背景\n语言引导的医学图像分割仍面临挑战');
check('半截输出也能渲染', partial.length === 1 && partial[0].body.includes('面临挑战'));

process.exit(failed === 0 ? 0 : 1);
"""

script = TEST.replace("__PIECES__", "\n\n".join(pieces))
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
    handle.write(script)
    path = handle.name

result = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
print(result.stdout or "")
if result.stderr:
    print("stderr:", result.stderr[:800])
Path(path).unlink(missing_ok=True)
sys.exit(result.returncode)
