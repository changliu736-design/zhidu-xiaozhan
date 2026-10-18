# -*- coding: utf-8 -*-
"""实测用户名规则：

  1. 注册时中文被拒绝
  2. 各种非法格式被拒绝、合法格式通过
  3. **历史上用中文注册的老账号仍然可以登录**（这是关键，不能把用户锁在门外）

会临时改动 users.json，跑完自动恢复。
"""
import atexit
import json
import os
import shutil
import sys
import urllib.error
import urllib.request

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
USERS = os.path.join(BACKEND, "data", "users.json")
UPLOADS = os.path.join(BACKEND, "uploads")
BACKUP = USERS + ".auth-bak"
LEGACY_NAME = "红参二号"           # 模拟"以前用中文注册的老账号"
LEGACY_PASS = "legacy123456"
LEGACY_ID = "u_legacy_chinese_probe"
created_ids = []
results = []


def restore():
    if os.path.exists(BACKUP):
        shutil.move(BACKUP, USERS)
        print("  [清理] users.json 已恢复原样")
    for user_id in created_ids:
        folder = os.path.join(UPLOADS, user_id)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print(f"  [清理] 已删除目录 {user_id}")


atexit.register(restore)


def call(method, path, body=None):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body else None, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:150]}


def show(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'[通过]' if ok else '[失败]'} {name}" + (f"  — {detail}" if detail else ""))


def expect_reject(username, why):
    status, data = call("POST", "/api/auth/register",
                        {"username": username, "password": "test123456"})
    show(f"拒绝「{username}」（{why}）", status == 400, f"HTTP {status} {data.get('error', '')}")


def main():
    shutil.copy2(USERS, BACKUP)

    # ---------- 先注入一个"历史上用中文注册的老账号" ----------
    sys.path.insert(0, BACKEND)
    from auth import hash_password  # noqa: E402

    with open(USERS, encoding="utf-8") as handle:
        users = json.load(handle)
    users[LEGACY_NAME] = {
        "id": LEGACY_ID,
        "username": LEGACY_NAME,
        "password": hash_password(LEGACY_PASS),
        "created_at": "2026-01-01T00:00:00",
    }
    with open(USERS, "w", encoding="utf-8") as handle:
        json.dump(users, handle, ensure_ascii=False, indent=2)

    print("=== 1. 老的中文账号还能不能登录（最关键）===")
    status, data = call("POST", "/api/auth/login",
                        {"username": LEGACY_NAME, "password": LEGACY_PASS})
    show(f"中文老账号「{LEGACY_NAME}」仍可登录", status == 200 and data.get("token"),
         f"HTTP {status} {data.get('error', '')}")
    status, data = call("POST", "/api/auth/login",
                        {"username": LEGACY_NAME, "password": "wrongpass"})
    show("中文老账号密码错误仍被拒", status == 401, f"HTTP {status}")

    print("\n=== 2. 注册中文用户名应当被拒绝 ===")
    expect_reject("中文名字", "纯中文")
    expect_reject("用户abc", "中文+字母")
    expect_reject("小明", "中文名")
    expect_reject("test中文", "字母+中文")

    print("\n=== 3. 其他非法格式也应当被拒绝 ===")
    expect_reject("a", "太短（1 位）")
    expect_reject("a" * 33, "太长（33 位）")
    expect_reject("a b", "含空格")
    expect_reject("a_b", "含下划线")
    expect_reject("li.si", "含点")
    expect_reject("wang-wu", "含横线")
    expect_reject("a@b", "含 @")
    expect_reject("a/b", "含斜杠")
    expect_reject("", "空用户名")

    print("\n=== 4. 合法用户名（纯字母数字）应当通过 ===")
    for name in ("student01", "zhang123", "li2024", "A1B2C3", "123456", "Zz"):
        status, data = call("POST", "/api/auth/register",
                            {"username": name, "password": "test123456"})
        ok = status == 200 and data.get("user", {}).get("username") == name
        show(f"接受「{name}」", ok, f"HTTP {status} {data.get('error', '')}")
        if ok:
            created_ids.append(data["user"]["id"])

    print("\n=== 5. 重复用户名仍应被拒 ===")
    status, data = call("POST", "/api/auth/register",
                        {"username": "student01", "password": "test123456"})
    show("重复用户名被拒", status == 400, f"HTTP {status} {data.get('error', '')}")

    print("\n=== 6. 大小写不同的同名也应被拒（避免混淆）===")
    status, data = call("POST", "/api/auth/register",
                        {"username": "STUDENT01", "password": "test123456"})
    show("大小写不同视为重名", status == 400, f"HTTP {status} {data.get('error', '')}")

    print("\n=== 7. 新注册的账号能正常登录 ===")
    status, data = call("POST", "/api/auth/login",
                        {"username": "zhang123", "password": "test123456"})
    show("新账号可登录", status == 200 and data.get("token"), f"HTTP {status}")

    restore()
    with open(USERS, encoding="utf-8") as handle:
        names = [v["username"] for v in json.load(handle).values()]
    show("账号文件已恢复（你原有的账号都在）", "红参" in names and "红色" in names, f"账号 {names}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 58}\n实测结果：{passed}/{len(results)} 项通过\n{'=' * 58}")
    for name, ok, detail in results:
        if not ok:
            print(f"  未通过：{name}  {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
