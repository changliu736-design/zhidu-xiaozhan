# -*- coding: utf-8 -*-
"""管理员命令行工具。

用法（在 backend 目录下）：
    python manage.py create-admin
    python manage.py create-admin --username admin
    python manage.py list-users
"""
import argparse
import getpass
import sys

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import auth


def create_admin(args) -> int:
    username = (args.username or "").strip()
    if not username:
        username = input("管理员用户名：").strip()

    existing = next((user for user in auth.all_users()
                     if user.get("username", "").lower() == username.lower()), None)
    if existing is not None:
        try:
            user = auth.create_admin(username, "")
        except auth.AuthError as exc:
            print(f"提升失败：{exc}")
            return 1
        print(f"已把 {user['username']} 提升为管理员（密码不变）")
        return 0

    password = args.password
    if not password:
        password = getpass.getpass("管理员密码：")
        confirm = getpass.getpass("再输一次密码：")
        if password != confirm:
            print("两次输入的密码不一致")
            return 1
    try:
        user = auth.create_admin(username, password)
    except auth.AuthError as exc:
        print(f"创建失败：{exc}")
        return 1
    print(f"管理员已就绪：{user['username']}（{user['id']}）")
    print("用这个账号登录后，页面顶部会出现「管理后台」入口。")
    return 0


def list_users(_args) -> int:
    users = sorted(auth.all_users(), key=lambda u: u.get("created_at", ""))
    if not users:
        print("还没有任何账号。")
        return 0
    print(f"{'用户名':<20}{'角色':<8}{'状态':<8}{'创建时间':<22}ID")
    print("-" * 90)
    for user in users:
        role = "管理员" if auth.is_admin(user) else "用户"
        status = "已封禁" if auth.is_banned(user) else "正常"
        print(f"{user.get('username', ''):<20}{role:<8}{status:<8}"
              f"{user.get('created_at', ''):<22}{user.get('id', '')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="智读小站管理员工具")
    sub = parser.add_subparsers(dest="command", required=True)

    admin_parser = sub.add_parser("create-admin", help="创建管理员或把已有账号提升为管理员")
    admin_parser.add_argument("--username", help="用户名（不给则交互输入）")
    admin_parser.add_argument("--password", help="密码（不建议写在命令里，默认交互输入）")
    admin_parser.set_defaults(func=create_admin)

    list_parser = sub.add_parser("list-users", help="查看所有账号")
    list_parser.set_defaults(func=list_users)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
