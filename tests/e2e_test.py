# -*- coding: utf-8 -*-
"""端到端自测：注册 → 登录 → 文档 → 建索引 → 流式摘要 → 带出处问答 → 引用跳页 → 多用户隔离。

测试会临时注册账号，跑完自动恢复 users.json 并删掉测试目录（即使中途崩了也会清理）。
"""
import atexit
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS = os.path.join(ROOT, "backend", "data", "users.json")
BACKUP = USERS + ".e2e-bak"
UPLOADS = os.path.join(ROOT, "backend", "uploads")
results = []
_created_ids = []


def restore_accounts():
    """把账号文件恢复原样，并删掉测试账号的目录。"""
    if os.path.exists(BACKUP):
        shutil.move(BACKUP, USERS)
        print("  [清理] 账号文件已恢复原样")
    for user_id in _created_ids:
        folder = os.path.join(UPLOADS, user_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print(f"  [清理] 已删除测试目录 {user_id}")


atexit.register(restore_accounts)


def find_sample_pdf() -> str:
    """随便找一篇 PDF 用于测试上传；也可指定：python tests/e2e_test.py 某个.pdf"""
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        return sys.argv[1]
    for folder, _dirs, files in os.walk(ROOT):
        for name in files:
            if name.lower().endswith(".pdf"):
                return os.path.join(folder, name)
    raise SystemExit("没找到可用的 PDF，请指定：python tests/e2e_test.py 某个.pdf")


PDF = find_sample_pdf()


def call(method, path, body=None, token=None, raw=False, timeout=180):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            if raw:
                return response.status, payload, dict(response.headers)
            try:
                return response.status, json.loads(payload), dict(response.headers)
            except json.JSONDecodeError:
                return response.status, payload.decode("utf-8", "replace"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload), dict(exc.headers)
        except json.JSONDecodeError:
            return exc.code, payload.decode("utf-8", "replace"), dict(exc.headers)


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"  {'[通过]' if condition else '[失败]'} {name}" + (f"  — {detail}" if detail else ""))
    return bool(condition)


def call_upload(token, filepath, filename=None):
    """multipart 上传，返回 (状态码, JSON)。"""
    boundary = "----e2e" + uuid.uuid4().hex
    name = filename or os.path.basename(filepath)
    with open(filepath, "rb") as handle:
        content = handle.read()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + content + \
           f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(BASE + "/api/upload", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read()), {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw), {}
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:200]}, {}


def stream(path, token, timeout=300):
    """读取 SSE 流，返回 (事件列表, 拼接后的文本)"""
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


def main():
    stamp = uuid.uuid4().hex[:6]
    # 用户名不允许下划线等符号，只能用字母数字
    user1, user2 = f"selftest{stamp}", f"other{stamp}"
    password = "test123456"

    print("\n=== 1. 注册第一个用户（应继承升级前的文献）===")
    shutil.copy2(USERS, BACKUP)
    status, data, _ = call("POST", "/api/auth/register", {"username": user1, "password": password})
    check("注册成功", status == 200, f"HTTP {status} {data if status != 200 else ''}")
    token1 = data.get("token", "")
    if data.get("user", {}).get("id"):
        _created_ids.append(data["user"]["id"])
    check("拿到令牌", bool(token1))
    claimed = data.get("claimed_legacy", 0)
    if claimed > 0:
        check("继承了升级前的文献", True, f"继承 {claimed} 个文件")
    else:
        # 机器上已经有账号了（老文献早被第一个账号继承走），这一项没得测，跳过不算失败
        print("  [跳过] 继承了升级前的文献  — 当前不是全新安装，没有待继承的文献")

    print("\n=== 2. 文档列表与后台建索引 ===")
    status, docs, _ = call("GET", "/api/documents", token=token1)
    if not docs:
        # 新注册的账号名下是空的，自己传一篇 PDF 进来，让后续用例有东西可测
        print("  （新账号没有文献，自己上传一篇用于测试）")
        status, uploaded, _ = call_upload(token1, PDF, "e2e_sample.pdf")
        check("测试用 PDF 上传成功", status == 200, f"HTTP {status}")
        docs = [uploaded.get("document", {})]
    check("能列出文档", status == 200 and len(docs) > 0, f"{len(docs)} 篇")
    for doc in docs:
        print(f"      · {doc['filename'][:40]}  {doc.get('page_count')}页/{doc['chunk_count']}段  索引={doc.get('index_status')}")

    print("  等待索引完成（GPU 上应该几秒）…")
    deadline = time.time() + 420
    ready = []
    while time.time() < deadline:
        status, docs, _ = call("GET", "/api/documents", token=token1)
        ready = [d for d in docs if d.get("index_status") == "ready"]
        if len(ready) == len(docs):
            break
        time.sleep(4)
    check("全部索引完成", len(ready) == len(docs), f"{len(ready)}/{len(docs)} 就绪")

    target = None
    for doc in docs:
        if (doc.get("page_count") or 0) > 1:
            target = doc
            break
    if target is None:
        target = docs[0]
    check("文档带页码信息", (target.get("page_count") or 0) > 0, f"page_count={target.get('page_count')}")

    print("\n=== 3. 流式摘要（带页码出处）===")
    events, text = stream(f"/api/documents/{target['id']}/summary/stream", token1)
    kinds = [e["type"] for e in events]
    check("摘要流式返回", kinds.count("delta") > 0, f"{kinds.count('delta')} 个增量包")
    check("摘要无错误", "error" not in kinds, str([e for e in events if e["type"] == "error"])[:120])
    check("摘要内容非空", len(text.strip()) > 50, f"{len(text)} 字")
    done = [e for e in events if e["type"] == "done"]
    citations = done[0].get("citations", []) if done else []
    check("摘要带页码出处", len(citations) > 0, f"引用页码 {citations}")
    check("摘要带参考原文", bool(done and done[0].get("passages")),
          f"{len(done[0].get('passages', [])) if done else 0} 个片段")
    print("      摘要开头：" + text.strip().replace("\n", " ")[:100] + "…")

    print("\n=== 4. 溯源问答 ===")
    question = "这篇文献研究的是什么问题？用了什么方法？"
    events, answer = stream(
        f"/api/documents/{target['id']}/ask/stream?q=" + urllib.parse.quote(question), token1)
    kinds = [e["type"] for e in events]
    check("问答流式返回", kinds.count("delta") > 0, f"{kinds.count('delta')} 个增量包")
    check("问答无错误", "error" not in kinds, str([e for e in events if e["type"] == "error"])[:150])
    done = [e for e in events if e["type"] == "done"]
    answer_citations = done[0].get("citations", []) if done else []
    check("答案带页码出处", len(answer_citations) > 0, f"引用页码 {answer_citations}")
    check("答案正文里有 [[p.N]] 标记", "[[p." in answer, answer[:80].replace("\n", " "))
    passages = done[0].get("passages", []) if done else []
    check("参考片段带页码", bool(passages) and all(p.get("page") for p in passages),
          f"页码 {[p['page'] for p in passages]}")
    print("      回答：" + answer.strip().replace("\n", " ")[:140] + "…")

    print("\n=== 5. 原文 PDF 服务（引用跳转要用）===")
    status, payload, headers = call("GET", f"/api/documents/{target['id']}/file?token={token1}",
                                    token=token1, raw=True)
    check("能取到原始 PDF", status == 200 and payload[:4] == b"%PDF", f"HTTP {status}, {len(payload)} 字节")
    check("Content-Type 正确", "pdf" in headers.get("Content-Type", ""), headers.get("Content-Type", ""))
    status, _, _ = call("GET", f"/api/documents/{target['id']}/file?token=badtoken", raw=True)
    check("错误令牌被拒绝", status == 401, f"HTTP {status}")

    print("\n=== 6. 上传新文献（新格式）===")
    boundary = "----zd" + uuid.uuid4().hex
    with open(PDF, "rb") as handle:
        content = handle.read()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"selftest_upload.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode() + content + \
           f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(BASE + "/api/upload", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", "Bearer " + token1)
    with urllib.request.urlopen(request, timeout=180) as response:
        uploaded = json.loads(response.read())
    new_id = uploaded["document"]["id"]
    check("上传成功", uploaded.get("success") is True, f"{uploaded['document']['chunk_count']} 段")
    check("同名文件不覆盖", uploaded["document"]["filename"] == "selftest_upload.pdf")

    deadline = time.time() + 300
    while time.time() < deadline:
        status, state, _ = call("GET", f"/api/documents/{new_id}/status", token=token1)
        if state.get("index_status") in ("ready", "error"):
            break
        time.sleep(3)
    check("新文档索引完成", state.get("index_status") == "ready", json.dumps(state, ensure_ascii=False)[:150])

    print("\n=== 7. 多用户隔离 ===")
    status, data2, _ = call("POST", "/api/auth/register", {"username": user2, "password": password})
    check("第二个用户注册成功", status == 200, f"HTTP {status}")
    token2 = data2.get("token", "")
    if data2.get("user", {}).get("id"):
        _created_ids.append(data2["user"]["id"])
    check("第二个用户不继承文献", data2.get("claimed_legacy", 0) == 0)
    status, docs2, _ = call("GET", "/api/documents", token=token2)
    check("看不到别人的文献", isinstance(docs2, list) and len(docs2) == 0, f"{len(docs2)} 篇")
    status, _, _ = call("GET", f"/api/documents/{target['id']}/status", token=token2)
    check("访问别人文档返回 404", status == 404, f"HTTP {status}")
    status, _, _ = call("GET", f"/api/documents/{target['id']}/summary/stream", token=token2)
    check("拿别人文档生成摘要被拒", status == 404, f"HTTP {status}")
    status, _, _ = call("DELETE", f"/api/documents/{target['id']}/delete", token=token2)
    check("删不了别人的文档", status == 404, f"HTTP {status}")

    print("\n=== 8. 未登录访问 ===")
    status, _, _ = call("GET", "/api/documents")
    check("未登录被拦", status == 401, f"HTTP {status}")
    status, _, _ = call("POST", "/api/upload")
    check("未登录不能上传", status == 401, f"HTTP {status}")

    print("\n=== 9. 密码与会话 ===")
    status, _, _ = call("POST", "/api/auth/login", {"username": user1, "password": "wrongpass"})
    check("错误密码被拒", status == 401, f"HTTP {status}")
    status, data, _ = call("POST", "/api/auth/login", {"username": user1, "password": password})
    check("正确密码可登录", status == 200 and data.get("token"), f"HTTP {status}")
    status, data, _ = call("GET", "/api/auth/me", token=data.get("token", ""))
    check("/me 返回当前用户", status == 200 and data["user"]["username"] == user1)
    status, _, _ = call("POST", "/api/auth/register", {"username": user1, "password": password})
    check("重复用户名被拒", status == 400, f"HTTP {status}")
    status, _, _ = call("POST", "/api/auth/register", {"username": "ab", "password": "123"})
    check("弱密码被拒", status == 400, f"HTTP {status}")

    print("\n=== 10. 收尾清理 ===")
    status, _, _ = call("DELETE", f"/api/documents/{new_id}/delete", token=token1)
    check("删除测试文档", status == 200, f"HTTP {status}")
    status, state, _ = call("GET", f"/api/documents/{new_id}/status", token=token1)
    check("删除后不可见", status == 404, f"HTTP {status}")

    restore_accounts()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 60}\n自测结果：{passed}/{len(results)} 项通过\n{'=' * 60}")
    for name, ok, detail in results:
        if not ok:
            print(f"  未通过：{name}  {detail}")
    print(f"\n测试账号：{user1} / {user2}（密码 {password}）")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import urllib.parse  # noqa: E402
    sys.exit(main())
