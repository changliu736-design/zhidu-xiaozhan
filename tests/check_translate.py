# -*- coding: utf-8 -*-
"""实测翻译功能：能否用、质量如何、有什么边界限制。

会临时注册一个测试账号，测完从 users.json 里精确删掉，不留痕迹。
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = "http://127.0.0.1:5000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USERS = os.path.join(ROOT, "backend", "data", "users.json")
BACKUP = USERS + ".probe-bak"
PROBE_USER = "translateprobe"
PROBE_PASS = "probe123456"

results = []


def call(method, path, body=None, token=None, timeout=240):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode("utf-8", "replace")[:200]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def stream(path, token, timeout=240):
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
    results.append((name, ok, detail))
    print(f"  {'[通过]' if ok else '[失败]'} {name}" + (f"  — {detail}" if detail else ""))


def translate(token, text, source="en", target="zh"):
    """走前端真正的流式接口（GET + query string），这样连 URL 长度限制一起测。"""
    url = (f"/api/translate/stream?text={urllib.parse.quote(text)}"
           f"&source_lang={source}&target_lang={target}")
    return len(url), stream(url, token)


def main():
    shutil.copy2(USERS, BACKUP)
    print("已备份账号文件\n")

    status, data = call("POST", "/api/auth/register",
                        {"username": PROBE_USER, "password": PROBE_PASS})
    if status != 200:
        print("注册测试账号失败:", status, data)
        shutil.move(BACKUP, USERS)
        return 1
    token = data["token"]
    print("测试账号已就绪\n")

    try:
        print("=== 1. 未登录能不能用 ===")
        status, _ = call("POST", "/api/translate", {"text": "hello"})
        show("未登录被拦截", status == 401, f"HTTP {status}")

        print("\n=== 2. 术语翻译（英→中）===")
        status, data = call("POST", "/api/translate",
                            {"text": "supercapacitor", "source_lang": "en", "target_lang": "zh"},
                            token=token)
        show("接口返回成功", status == 200, f"HTTP {status}")
        if status == 200:
            print(f"      原文: {data.get('original')}")
            print(f"      译文: {data.get('translated')}")
            show("译文非空且是中文", bool(data.get("translated")) and
                 any('\u4e00' <= c <= '\u9fff' for c in data.get("translated", "")))
            show("走了大模型翻译（不是假翻译）",
                 data.get("method") == "llm_translation", str(data.get("method")))

        print("\n=== 3. 学术句子翻译（英→中）===")
        sentence = ("The proposed device demonstrates excellent performance in terms of "
                    "energy density and cycle stability over 10,000 cycles.")
        status, data = call("POST", "/api/translate",
                            {"text": sentence, "source_lang": "en", "target_lang": "zh"},
                            token=token)
        if status == 200:
            print(f"      原文: {sentence}")
            print(f"      译文: {data.get('translated')}")
            show("学术术语翻译合理", "循环" in data.get("translated", "") or
                 "稳定性" in data.get("translated", ""))

        print("\n=== 4. 反方向（中→英）===")
        status, data = call("POST", "/api/translate",
                            {"text": "本文提出了一种面向医学图像分割的跨模态条件重建方法。",
                             "source_lang": "zh", "target_lang": "en"},
                            token=token)
        if status == 200:
            print(f"      译文: {data.get('translated')}")
            show("中→英正常", len(data.get("translated", "")) > 10)

        print("\n=== 5. 长文本能不能完整翻译（约 3200 字、内容不重复）===")
        long_en = (
            "Medical image segmentation plays a critical role in computer-aided diagnosis and "
            "treatment planning. Unlike natural images, medical scans often exhibit low contrast "
            "between lesions and surrounding tissue, which makes boundary delineation particularly "
            "challenging. To mitigate this difficulty, recent studies have introduced textual reports "
            "as auxiliary supervision signals.\n\n"
            "Nevertheless, aligning free-form clinical language with dense pixel-level annotations "
            "remains an open problem. A naive concatenation of text embeddings and visual features "
            "frequently causes the network to ignore the linguistic modality altogether, a phenomenon "
            "known as modality collapse. Our approach instead reconstructs masked visual tokens "
            "conditioned on the language representation, forcing the model to preserve semantic "
            "consistency across modalities.\n\n"
            "We evaluate the proposed method on three publicly available benchmarks, including chest "
            "X-ray and CT colonography datasets. Across all settings, the framework yields consistent "
            "gains over strong baselines, with an average improvement of 3.2 percentage points in "
            "Dice similarity coefficient. Ablation studies further confirm that removing either the "
            "textual branch or the reconstruction objective leads to a marked performance drop.\n\n"
            "From an engineering perspective, the inference overhead introduced by the additional "
            "cross-attention layers is modest. On a single consumer GPU, the throughput decreases by "
            "less than eight percent compared with the vision-only backbone, while memory consumption "
            "grows by roughly three hundred megabytes. This trade-off is acceptable for clinical "
            "deployment scenarios where accuracy is prioritised over latency.\n\n"
            "Finally, we discuss the limitations of the current study. The method assumes the "
            "availability of paired reports, which may not hold in every clinical scenario. Future "
            "work will explore weakly supervised settings and investigate whether large language "
            "models can generate pseudo-reports that are faithful enough for training segmentation "
            "networks."
        )
        print(f"      输入 {len(long_en)} 字符")
        status, data = call("POST", "/api/translate",
                            {"text": long_en, "source_lang": "en", "target_lang": "zh"},
                            token=token)
        if status == 200:
            out = data.get("translated") or ""
            print(f"      输入 {len(long_en)} 字符 → 输出 {len(out)} 字符")
            print(f"      结尾: …{out[-45:]}")
            show("长文本有输出（不是 null）", bool(out.strip()),
                 "返回了空/null —— 接口坏了" if not out.strip() else "")
            # 完整翻译的标志：译文结尾应当对应原文最后一句（"网络"）
            reached_end = "网络" in out[-60:]
            show("长文本完整译完最后一段", reached_end,
                 "" if reached_end else "结尾没到全文最后一句，可能提前停了")

        print("\n=== 6. 长中文段落 —— URL 长度限制（前端把文本塞在网址里）===")
        long_zh = ("医学图像分割是计算机辅助诊断中的基础任务。近年来的研究强调了文本信息在提升"
                   "医学图像分析模型方面的潜力。然而现有工作采用过于隐式和模糊的架构引入文本"
                   "指导，导致分割结果与语言表达的语义不一致，有时甚至显著偏离。") * 12
        print(f"      中文输入 {len(long_zh)} 字符")
        url_len, (events, out) = translate(token, long_zh, "zh", "en")
        kinds = [e["type"] for e in events]
        print(f"      请求网址长度 {url_len} 字符")
        show("超长中文没有把请求撑爆", "error" not in kinds and bool(out.strip()),
             f"收到 {len(out)} 字符" if out.strip() else str([e for e in events if e['type'] == 'error'])[:120])
        if out.strip():
            print(f"      译文开头: {out.strip()[:80]}…")

        print("\n=== 7. 流式输出（打字机效果的数据源）===")
        url_len, (events, out) = translate(
            token, "Cross-modal conditioned reconstruction for language-guided segmentation.", "en", "zh")
        kinds = [e["type"] for e in events]
        show("流式返回多个增量包", kinds.count("delta") > 1, f"{kinds.count('delta')} 个 delta")
        show("有结束事件", "done" in kinds)
        print(f"      译文: {out.strip()}")

        print("\n=== 8. 边界情况 ===")
        status, data = call("POST", "/api/translate", {"text": "   "}, token=token)
        show("空文本被拒", status == 400, f"HTTP {status}")
        status, data = call("GET", "/api/translate/stream?text=hello&source_lang=en&target_lang=en",
                            token=token)
        show("源语言=目标语言被拒", status == 400, f"HTTP {status}")
        status, data = call("POST", "/api/translate",
                            {"text": "test", "source_lang": "en", "target_lang": "fr"}, token=token)
        show("未在界面提供的语向（英→法）后端也支持", status == 200, f"HTTP {status}")

    finally:
        print("\n=== 清理：把账号文件恢复原样 ===")
        shutil.move(BACKUP, USERS)
        with open(USERS, encoding="utf-8") as handle:
            users = json.load(handle)
        names = [v["username"] for v in users.values()]
        print(f"      恢复后账号: {names}")
        show("测试账号已彻底移除", PROBE_USER not in names)
        probe_dir = os.path.join(ROOT, "backend", "uploads",
                                 "u_" + str(abs(hash(PROBE_USER)))[:8])
        # 注册本身不会建目录，这里只做提示
        leftovers = [d for d in os.listdir(os.path.join(ROOT, "backend", "uploads"))
                     if not d.startswith("u_") is False and os.path.isdir(
                         os.path.join(ROOT, "backend", "uploads", d))]
        print(f"      uploads 下的账号目录: {leftovers}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 58}\n实测结果：{passed}/{len(results)} 项通过\n{'=' * 58}")
    for name, ok, detail in results:
        if not ok:
            print(f"  未通过：{name}  {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
