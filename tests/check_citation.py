# -*- coding: utf-8 -*-
"""回归测试：只有 1 页的文档，出处编号必须都是 p.1，不能出现 p.1/p.2/p.3。

原来的 bug：给模型的上下文每条开头是「片段1 | 第1页」，同时出现两个数字，
文档只有一页时模型就把片段序号当成了页码。

另外检查章节识别不会把手机号、日期这类数字行误判成标题。
会临时注册账号，跑完自动恢复。
"""
import atexit
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
USERS = os.path.join(BACKEND, "data", "users.json")
UPLOADS = os.path.join(BACKEND, "uploads")
BACKUP = USERS + ".cite-bak"
WORK = os.path.join(ROOT, "tests", "_tmp_cite")
USER, PASSWORD = "citeprobe01", "citepass123"
results = []
created = []


def cleanup():
    if os.path.exists(BACKUP):
        shutil.move(BACKUP, USERS)
        print("  [清理] users.json 已恢复原样")
    for user_id in created:
        folder = os.path.join(UPLOADS, user_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print(f"  [清理] 已删除目录 {user_id}")
    shutil.rmtree(WORK, ignore_errors=True)


atexit.register(cleanup)


def call(method, path, body=None, token=None):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:150]}


def stream(path, token, timeout=300):
    request = urllib.request.Request(BASE + path)
    request.add_header("Authorization", "Bearer " + token)
    events, text = [], []
    with urllib.request.urlopen(request, timeout=timeout) as response:
        buffer = b""
        while True:
            block = response.read(1)
            if not block:
                break
            buffer += block
            if buffer.endswith(b"\n\n"):
                raw = buffer.decode("utf-8", "replace").strip()
                buffer = b""
                if raw.startswith("data:"):
                    event = json.loads(raw[5:].strip())
                    events.append(event)
                    if event.get("type") == "delta":
                        text.append(event["text"])
    return events, "".join(text)


def show(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'[通过]' if ok else '[失败]'} {name}" + (f"  — {detail}" if detail else ""))


def upload(token, filepath, filename):
    boundary = "----cite" + uuid.uuid4().hex
    with open(filepath, "rb") as handle:
        content = handle.read()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n').encode() + content + \
           f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(BASE + "/api/upload", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def make_onepage_pdf(path):
    """造一份只有 1 页、但文字够多会被切成 3 块以上的 PDF。"""
    import pymupdf

    body = (
        "Medical image segmentation is a fundamental task in computer-aided diagnosis. "
        "Recent developments underscore the potential of textual information in enhancing "
        "learning models for medical image analysis. However, existing work employs an overly "
        "implicit and ambiguous architecture to incorporate textual guidance, resulting in "
        "segmentation outputs that deviate from the semantics expressed in the language. "
        "To address this issue we propose a novel framework that explicitly conditions the "
        "reconstruction process on cross-modal information. Extensive experiments on three "
        "public benchmarks demonstrate consistent improvements over strong baselines, with an "
        "average gain of three point two percentage points in Dice similarity coefficient. "
        "Ablation studies further confirm that removing either the textual branch or the "
        "reconstruction objective leads to a marked performance drop. From an engineering "
        "perspective the additional inference overhead is modest: throughput decreases by less "
        "than eight percent while memory consumption grows by roughly three hundred megabytes. "
        "Finally we discuss the limitations of the current study and outline future work on "
        "weakly supervised settings and pseudo-report generation with large language models. "
    )
    full = body + body  # 加长到约 3000 字符，确保切成 3 块以上
    lines = [full[i:i + 88] for i in range(0, len(full), 88)]

    document = pymupdf.open()
    page = document.new_page()
    y = 55
    # 第一行做成章节标题，外加一行"手机号 + 邮箱"用来验证不会被误判成标题
    for line in ["1. Introduction", "15295908323 hongsheng@example.com"] + lines:
        if y > 835:
            break
        page.insert_text((40, y), line, fontsize=6.5)
        y += 8.5
    document.save(path)
    document.close()


def main():
    shutil.copy2(USERS, BACKUP)
    os.makedirs(WORK, exist_ok=True)

    print("=== 0. 章节识别单元检查（手机号不能被当成标题）===")
    sys.path.insert(0, BACKEND)
    from retrieval import _detect_heading  # noqa: E402

    cases = [
        ("1. Introduction", True), ("1.2 Methods", True), ("第一章 绪论", True),
        ("摘要", True), ("Results", True),
        ("15295908323 hongsheng@example.com", False),
        ("2024.09 - 2026.08", False), ("13800138000", False),
        ("本文提出了一种面向医学图像分割的跨模态条件重建方法，实验在三个公开数据集上进行。", False),
    ]
    for text, should_be_heading in cases:
        got = bool(_detect_heading(text))
        show(f"{'识别为标题' if should_be_heading else '不识别为标题'}：{text[:34]}",
             got == should_be_heading, f"实际={got}")

    print("\n=== 1. 造一份 1 页但有 3+ 块的 PDF ===")
    pdf = os.path.join(WORK, "onepage.pdf")
    make_onepage_pdf(pdf)
    show("PDF 已生成", os.path.exists(pdf), f"{os.path.getsize(pdf)} 字节")

    status, data = call("POST", "/api/auth/register", {"username": USER, "password": PASSWORD})
    if status != 200:
        print("  注册失败:", status, data)
        return 1
    token = data["token"]
    created.append(data["user"]["id"])

    status, up = upload(token, pdf, "单页测试.pdf")
    show("上传成功", status == 200, f"HTTP {status} {str(up)[:100]}")
    doc = up.get("document", {})
    print(f"      page_count={doc.get('page_count')}  chunk_count={doc.get('chunk_count')}")
    show("确实只有 1 页", doc.get("page_count") == 1, f"page_count={doc.get('page_count')}")
    show("但被切成了多块（复现条件成立）", (doc.get("chunk_count") or 0) >= 3,
         f"chunk_count={doc.get('chunk_count')}")

    deadline = time.time() + 240
    state = {}
    while time.time() < deadline:
        _, state = call("GET", f"/api/documents/{doc['id']}/status", token=token)
        if state.get("index_status") in ("ready", "error"):
            break
        time.sleep(3)
    show("索引完成", state.get("index_status") == "ready", json.dumps(state, ensure_ascii=False)[:100])

    print("\n=== 2. 模型看到的上下文（不应再出现「片段N」）===")
    _, blocks = call("GET", f"/api/documents/{doc['id']}/blocks", token=token)
    pages = sorted({b["page"] for b in blocks.get("blocks", [])})
    show("所有块的 page 都是 1", pages == [1], f"page 取值 {pages}")

    print("\n=== 3. 摘要的出处必须全是 p.1 ===")
    events, summary = stream(f"/api/documents/{doc['id']}/summary/stream", token)
    done = [e for e in events if e["type"] == "done"]
    citations = done[0].get("citations", []) if done else []
    show("摘要生成成功", len(summary.strip()) > 50, f"{len(summary)} 字")
    show("出处编号全部为 1", citations == [1],
         f"实际引用 {citations}" + ("  ← 又出现不存在的页号了" if citations != [1] else ""))
    show("正文里没有 p.2 / p.3", "[[p.2]]" not in summary and "[[p.3]]" not in summary,
         "正文出现了越界引用" if ("[[p.2]]" in summary or "[[p.3]]" in summary) else "干净")
    print("      摘要片段：" + summary.strip().replace("\n", " ")[:110] + "…")

    print("\n=== 4. 问答的出处也必须全是 p.1 ===")
    question = "这篇文章提出了什么方法？"
    events, answer = stream(
        f"/api/documents/{doc['id']}/ask/stream?q=" + urllib.parse.quote(question), token)
    done = [e for e in events if e["type"] == "done"]
    ask_citations = done[0].get("citations", []) if done else []
    show("问答有回答", len(answer.strip()) > 20, f"{len(answer)} 字")
    show("问答出处编号全部为 1", ask_citations == [1], f"实际引用 {ask_citations}")
    print("      回答：" + answer.strip().replace("\n", " ")[:110] + "…")

    print("\n=== 5. 多页文档不受影响（用真实 PDF 回归）===")
    sample = None
    for folder, _dirs, files in os.walk(UPLOADS):
        for name in files:
            if name.lower().endswith(".pdf") and "u_" in folder and "onepage" not in name:
                sample = os.path.join(folder, name)
                break
        if sample:
            break
    if sample:
        status, up2 = upload(token, sample, "既有多页文档.pdf")
        if status == 200:
            doc2 = up2["document"]
            deadline = time.time() + 300
            while time.time() < deadline:
                _, st2 = call("GET", f"/api/documents/{doc2['id']}/status", token=token)
                if st2.get("index_status") in ("ready", "error"):
                    break
                time.sleep(3)
            events, s2 = stream(f"/api/documents/{doc2['id']}/summary/stream", token)
            done = [e for e in events if e["type"] == "done"]
            cits2 = done[0].get("citations", []) if done else []
            in_range = all(1 <= c <= doc2["page_count"] for c in cits2)
            show("多页文档引用仍在有效范围内", bool(cits2) and in_range,
                 f"引用 {sorted(cits2)}，文档共 {doc2['page_count']} 页")
            call("DELETE", f"/api/documents/{doc2['id']}/delete", token=token)

    cleanup()
    with open(USERS, encoding="utf-8") as handle:
        names = [v["username"] for v in json.load(handle).values()]
    show("账号文件已恢复", "红参" in names and "红色" in names, f"账号 {names}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 58}\n实测结果：{passed}/{len(results)} 项通过\n{'=' * 58}")
    for name, ok, detail in results:
        if not ok:
            print(f"  未通过：{name}  {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
