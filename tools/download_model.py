# -*- coding: utf-8 -*-
"""从 ModelScope（阿里魔搭）下载 Qwen3-Embedding-0.6B 模型。

为什么不用 huggingface_hub：实测它校验不过国内镜像返回的元数据，
会报 FileMetadataError，模型根本下不下来。这里直接按文件下，绕开那个问题。

用法：
    python tools/download_model.py                       # 下到当前目录的 model 文件夹
    python tools/download_model.py D:\\model             # 下到指定目录
"""
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
BASE = f"https://www.modelscope.cn/api/v1/models/{MODEL_ID}/repo?Revision=master&FilePath="
FILES = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "1_Pooling/config.json",
]


def human(num):
    return f"{num / 1024 ** 3:.2f} GB" if num >= 1024 ** 3 else f"{num / 1024 ** 2:.1f} MB"


def download(url: str, dest: Path) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=180) as response, open(dest, "wb") as handle:
        total = int(response.headers.get("Content-Length") or 0)
        got = 0
        last = 0.0
        started = time.time()
        while True:
            chunk = response.read(1024 * 512)
            if not chunk:
                break
            handle.write(chunk)
            got += len(chunk)
            now = time.time()
            if total and now - last > 1.5:
                last = now
                done = got * 100.0 / total
                speed = got / max(now - started, 0.001) / 1024 ** 2
                sys.stdout.write(f"\r    {done:5.1f}%  {human(got)}/{human(total)}  "
                                 f"{speed:.1f} MB/s   ")
                sys.stdout.flush()
        if total:
            sys.stdout.write("\r" + " " * 60 + "\r")
    return got


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("model")
    target.mkdir(parents=True, exist_ok=True)
    print(f"模型：{MODEL_ID}")
    print(f"来源：ModelScope（魔搭）")
    print(f"目标：{target.resolve()}")
    print()

    failed = []
    started = time.time()
    for index, filename in enumerate(FILES, 1):
        dest = target / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            print(f"[{index}/{len(FILES)}] {filename}  已存在，跳过")
            continue
        print(f"[{index}/{len(FILES)}] {filename}")
        url = BASE + urllib.parse.quote(filename)
        try:
            size = download(url, dest)
            print(f"    ✅ {human(size)}")
        except urllib.error.HTTPError as exc:
            print(f"    ❌ HTTP {exc.code}")
            failed.append(filename)
        except Exception as exc:  # noqa: BLE001
            print(f"    ❌ {type(exc).__name__}: {str(exc)[:100]}")
            failed.append(filename)

    print()
    total = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    print(f"完成：{len(FILES) - len(failed)}/{len(FILES)} 个文件，共 {human(total)}，"
          f"用时 {time.time() - started:.0f} 秒")

    if failed:
        print(f"失败：{failed}")
        print("重跑一次本脚本即可（已下好的会自动跳过）")
        return 1

    if not (target / "model.safetensors").exists() or not (target / "config.json").exists():
        print("⚠ 关键文件缺失，请检查")
        return 1

    print()
    print("=" * 60)
    print("接下来在 backend\\.env 里加一行（把路径换成上面那个“目标”）：")
    print(f"EMBEDDING_MODEL_PATH={target.resolve()}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
