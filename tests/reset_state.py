# -*- coding: utf-8 -*-
"""把项目恢复到「还没有人注册过」的状态。

⚠️ 危险操作：会删除所有账号（含密码），并把各账号名下的文献移回 uploads 根目录，
   让下一个注册的人继承。默认只做「预演」，不实际执行。

预演：  python tests/reset_state.py
执行：  python tests/reset_state.py --force
"""
import json
import shutil
import sys
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BACKEND = Path(__file__).resolve().parent.parent / "backend"
UPLOADS = BACKEND / "uploads"
DATA = BACKEND / "data"


def existing_users():
    path = DATA / "users.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return [v.get("username", "?") for v in json.load(handle).values()]
    except (json.JSONDecodeError, OSError):
        return []


def user_folders():
    if not UPLOADS.is_dir():
        return []
    return [p for p in UPLOADS.iterdir() if p.is_dir()]


def describe():
    users = existing_users()
    folders = user_folders()
    files = sum(len(list(f.iterdir())) for f in folders)
    print("将要执行的操作：")
    print(f"  1. 删除 backend/data/users.json 和 secret.key" if users
          else "  1. （还没有账号文件，跳过）")
    if users:
        print(f"     ⚠ 这会删除以下 {len(users)} 个账号（密码一起丢失）：{'、'.join(users)}")
    print(f"  2. 把 {len(folders)} 个账号目录里的 {files} 个文献文件移回 uploads 根目录")
    print("  3. 清掉这些文献的账号归属，等下一个注册的人重新接管")


def main():
    force = "--force" in sys.argv
    users = existing_users()

    if not force:
        describe()
        print()
        if users:
            print("⚠️  检测到你已经有真实账号在用了，请确认这确实是你想要的。")
        print("这只是预演，什么都没做。确实要执行就加 --force：")
        print("    python tests/reset_state.py --force")
        return 0

    moved = 0
    for folder in user_folders():
        for item in folder.iterdir():
            target = UPLOADS / item.name
            if target.exists():
                target.unlink()
            shutil.move(str(item), str(target))
            moved += 1
        folder.rmdir()
        print(f"  回收账号目录 {folder.name}")
    print(f"  把 {moved} 个文件移回 uploads 根目录")

    for name in ("users.json", "secret.key"):
        path = DATA / name
        if path.exists():
            path.unlink()
            print(f"  删除 {name}")

    # 清掉 info.json 里的用户归属，让下一个注册的人重新接管
    for path in UPLOADS.glob("*_info.json"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
            info.pop("user_id", None)
            info["has_index"] = False
            info["index_status"] = "pending"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(info, handle, ensure_ascii=False)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  {path.name} 处理失败：{exc}")

    print("\n当前 uploads 根目录：")
    for item in sorted(UPLOADS.iterdir()):
        print(f"  {'[目录]' if item.is_dir() else '      '} {item.name}")
    print("\n完成。下次有人注册时，会接管这些文献。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
