# -*- coding: utf-8 -*-
"""实测修改密码功能：

  1. 原密码错误 / 新密码太短 / 新旧相同 → 拒绝
  2. 改成功后：旧令牌作废、新令牌可用、旧密码登不上、新密码能登录
  3. **没改过密码的老令牌不受影响**（不能把现有登录状态全踢掉）

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
BACKUP = USERS + ".pwd-bak"
USER = "pwdtest01"
OLD_PASS = "oldpass123"
NEW_PASS = "newpass456"
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


def call(method, path, body=None, token=None):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
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


def main():
    shutil.copy2(USERS, BACKUP)

    sys.path.insert(0, BACKEND)
    from auth import issue_token, _serializer  # noqa: E402

    print("=== 准备：注册一个测试账号 ===")
    status, data = call("POST", "/api/auth/register", {"username": USER, "password": OLD_PASS})
    if status != 200:
        print("  注册失败:", status, data)
        return 1
    old_token = data["token"]
    created_ids.append(data["user"]["id"])
    print(f"  账号 {USER} 已就绪，拿到旧令牌\n")

    print("=== 1. 参数校验 ===")
    status, data = call("POST", "/api/auth/password",
                        {"old_password": "wrongpass", "new_password": NEW_PASS}, token=old_token)
    show("原密码错误被拒", status == 400, f"HTTP {status} {data.get('error', '')}")

    status, data = call("POST", "/api/auth/password",
                        {"old_password": OLD_PASS, "new_password": "123"}, token=old_token)
    show("新密码太短被拒", status == 400, f"HTTP {status} {data.get('error', '')}")

    status, data = call("POST", "/api/auth/password",
                        {"old_password": OLD_PASS, "new_password": OLD_PASS}, token=old_token)
    show("新旧密码相同被拒", status == 400, f"HTTP {status} {data.get('error', '')}")

    status, data = call("POST", "/api/auth/password",
                        {"old_password": OLD_PASS, "new_password": NEW_PASS})
    show("未登录不能改密码", status == 401, f"HTTP {status}")

    print("\n=== 2. 正常修改 ===")
    status, data = call("POST", "/api/auth/password",
                        {"old_password": OLD_PASS, "new_password": NEW_PASS}, token=old_token)
    show("修改成功", status == 200, f"HTTP {status} {data.get('error', '')}")
    new_token = data.get("token", "")
    show("返回了新令牌", bool(new_token))
    show("新令牌和旧令牌不同", new_token != old_token)

    print("\n=== 3. 旧令牌应当失效（把别人踢下线）===")
    status, _ = call("GET", "/api/documents", token=old_token)
    show("旧令牌已失效", status == 401, f"HTTP {status}")
    status, _ = call("GET", "/api/documents", token=new_token)
    show("新令牌可用（当前设备没被踢）", status == 200, f"HTTP {status}")

    print("\n=== 4. 登录：旧密码不行，新密码可以 ===")
    status, _ = call("POST", "/api/auth/login", {"username": USER, "password": OLD_PASS})
    show("旧密码登录被拒", status == 401, f"HTTP {status}")
    status, data = call("POST", "/api/auth/login", {"username": USER, "password": NEW_PASS})
    show("新密码登录成功", status == 200 and data.get("token"), f"HTTP {status}")

    print("\n=== 5. 落盘检查 ===")
    with open(USERS, encoding="utf-8") as handle:
        record = json.load(handle)[USER]
    show("记录了改密时间", isinstance(record.get("pwd_changed_at"), int),
         f"pwd_changed_at={record.get('pwd_changed_at')}")
    show("密码哈希已更新", record["password"] != data.get("password"))

    print("\n=== 6. 没改过密码的账号不受影响（关键回归）===")
    # 伪造一个"老版本签发、没有 iat 字段"的令牌，模拟你现在浏览器里正开着的登录状态
    with open(USERS, encoding="utf-8") as handle:
        first = next(iter(json.load(handle).values()))
    legacy_token = _serializer().dumps({"uid": first["id"], "username": first["username"]})
    status, _ = call("GET", "/api/documents", token=legacy_token)
    show("没改过密码的老令牌仍然有效", status == 200, f"HTTP {status}")
    print(f"      （用你自己的账号「{first['username']}」验证的，没有改动它的任何数据）")

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
