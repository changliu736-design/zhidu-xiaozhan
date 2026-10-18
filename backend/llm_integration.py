# -*- coding: utf-8 -*-
"""大模型集成：结构化摘要 / 带出处的问答 / 学术翻译，均支持流式输出。

两处关键改动：
  1. 删除了原来的 _generate_mock_* 兜底。那套兜底是从别的项目抄来的，
     会一本正经地回答"工作电压窗口 1.8V""能量密度 0.40 mWh cm⁻³"，
     与本项目毫无关系，一旦 API 异常就会在答辩现场输出错误内容。
     现在改为显式报错，前端能看到真实原因。
  2. 所有回答强制要求用 [[p.页码]] 标注出处，页码只能取自检索到的片段，
     前端把标记渲染成可点击的跳转按钮 —— 这就是"可溯源引用"。
"""
import json
import sys
from typing import Dict, Iterator, List, Optional

from dotenv import load_dotenv

import config

load_dotenv()

try:
    from openai import OpenAI

    OPENAI_AVAILABLE = True
except ImportError:  # pragma: no cover
    OPENAI_AVAILABLE = False

_singleton: Optional["LLMIntegration"] = None


class LLMUnavailable(RuntimeError):
    """大模型不可用。区别于"降级模拟"，这里必须让用户看到真实原因。"""


def citation_rule(unit: str = "页", allowed: Optional[List[int]] = None) -> str:
    """引用规则。unit 是出处单位：PDF 是"页"，txt/md/docx 是"段"。"""
    rule = (
        f"【引用规则】每条结论后面必须紧跟出处标记，格式严格为 [[p.编号]]。"
        f"编号只能取自我给你的片段开头「出处：第 N {unit}」里的那个 N，"
        f"禁止编造编号，也**不要把片段的先后次序当编号**"
        f"（同一{unit}可能被拆成好几个片段，它们的编号是相同的）。"
        f"如果某句话无法在片段中找到依据，就不要写这句话。"
    )
    if allowed:
        numbers = "、".join(str(n) for n in allowed[:60])
        rule += f"本次可用的编号只有：{numbers}；除此以外的编号一律不得使用。"
    return rule


SUMMARY_SYSTEM_HEAD = (
    "你是一位严谨的学术文献分析助手。你只依据用户提供的文献片段作答，"
    "绝不引用片段之外的知识。"
)

QA_SYSTEM_HEAD = (
    "你是「智读小站」的 AI 文献助手，既能分析用户上传的文献，"
    "也能像普通 AI 助手一样聊天和解答通用问题。"
    "你会看到最近的对话记录：当用户说「还有呢」「它的缺点」「再详细一点」这类短追问时，"
    "要接着上一轮的话题继续回答，不要另起话题，也不要要求用户重复背景。"
    "先判断用户的问题是否与提供的文献片段相关：相关的部分依据文献作答并标注出处；"
    "与文献无关的问题（闲聊、自我介绍、通用知识、写作等）就像普通 AI 助手一样直接回答，"
    "不要拒绝，也不要把无关片段硬套上去。被问到身份时，说明自己是智读小站的文献助手。"
)

TRANSLATE_SYSTEM = (
    "你是一位专业的学术文本翻译专家，擅长学术术语的准确翻译。只输出译文，不要任何解释。"
)

COMMAND_SYSTEM = (
    "你是「智读小站」的指令解析器。用户会对当前文献说一句话，"
    "你要判断这句话想做什么操作，只输出一个 JSON 对象，不要输出解释或 Markdown。\n"
    "action 只能是以下之一：\n"
    '  "mark"        给某页某段新增红色高亮\n'
    '  "unmark"      取消/删除/去掉某页某段已有的高亮标记\n'
    '  "edit"        修改某页某段的文字、颜色或字体\n'
    '  "unedit"      撤销/删除某页某段已有的文字修改或改色\n'
    '  "jump"        跳到某页或某段\n'
    '  "list"        查看标记或修改列表\n'
    '  "export"      把标记导出为 Word\n'
    '  "qa"          不是对文档的操作，应该按普通问题回答\n'
    '  "unsupported" 是文档操作，但当前不支持（加粗、斜体、下划线、字号、重排、插图、删除原文等）\n'
    "edit 的额外字段：edit_type 为 \"replace\" 或 \"recolor\"；"
    "text 是用户要写入的新文字，只取用户给出的内容，不要改写；"
    "color 为 红/蓝/绿/黑/橙/紫/灰 或 null；"
    "font 为 宋体/黑体/微软雅黑/楷体 或 null。\n"
    "位置字段：page 是从 1 开始的整数；paragraph 也从 1 开始，没说就是 1。"
    "用户没给页码时不要猜，action 用 \"unsupported\"，reply 里请他说清楚第几页第几段。"
    "但用户说「刚才那个标记」「上一处修改」时，可以根据消息里附带的最近记录确定位置；"
    "最近记录为空就不要猜。\n"
    "否定语义必须分清：「取消/删除/去掉/清除…标记」是 unmark，不是 mark；"
    "「撤销/还原…修改」是 unedit。\n"
    "如果用户只是在提问（例如「这一页讲了什么」「标记是什么意思」），action 用 \"qa\"。\n"
    "输出字段固定为：action, page, paragraph, edit_type, text, color, font, reply，"
    "没有值的字段填 null。\n"
    "示例：\n"
    '用户：取消第三页第一段的标记 → action=unmark, page=3, paragraph=1\n'
    '用户：把第四页第一段改成：新的文字 → action=edit, edit_type=replace, page=4, paragraph=1, text=新的文字\n'
    '用户：把第五页第一段改成红色黑体 → action=edit, edit_type=recolor, page=5, paragraph=1, color=红, font=黑体\n'
    '用户：撤销第三页第一段的修改 → action=unedit, page=3, paragraph=1\n'
    '用户：把第三页第一段加粗 → action=unsupported，reply 说明暂不支持加粗\n'
)


class LLMIntegration:
    def __init__(self) -> None:
        self.model = config.OPENAI_MODEL
        self.base_url = config.OPENAI_BASE_URL
        self.api_key = config.OPENAI_API_KEY
        self._client = None
        self.error: Optional[str] = None
        if not OPENAI_AVAILABLE:
            self.error = "未安装 openai 库"
        elif not self.api_key:
            self.error = "未配置 OPENAI_API_KEY（请在 backend/.env 中填写）"

    # ------------------------------------------------------------ 基础
    @property
    def use_openai(self) -> bool:
        return self.error is None

    def _get_client(self):
        if not self.use_openai:
            raise LLMUnavailable(f"大模型不可用：{self.error}")
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def _call(self, system: str, user: str, *, temperature: float,
              max_tokens: int, stream: bool):
        client = self._get_client()
        last_error: Optional[Exception] = None
        for attempt in range(2):  # 网络抖动重试一次
            try:
                return client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=180,
                    stream=stream,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 0:
                    continue
        raise LLMUnavailable(f"大模型调用失败：{last_error}")

    @staticmethod
    def _deltas(stream) -> Iterator[str]:
        for event in stream:
            if not event.choices:
                continue
            piece = event.choices[0].delta.content
            if piece:
                yield piece

    # ------------------------------------------------------------ 摘要
    @staticmethod
    def _allowed_text(allowed: Optional[List[int]], unit: str) -> str:
        if not allowed:
            return ""
        numbers = "、".join(f"第 {n} {unit}" for n in allowed[:60])
        return f"\n本次片段涉及的{unit}只有：{numbers}。引用编号只能从这里取。"

    @staticmethod
    def _structure_note(items: List[Dict]) -> str:
        """有结构化标记时才加说明，普通文献不额外消耗 token。"""
        texts = "".join(str(item.get("text") or "") for item in items)
        if not any(marker in texts for marker in
                   ("【表格】", "【公式】", "【图注】", "【表注】")):
            return ""
        return ("片段中的【表格】是按行列还原的表格内容，【公式】是识别出的公式，"
                "【图注】/【表注】是图表标题；请把它们作为证据使用，"
                "引用表格数据时说明是哪张表。\n")

    def _summary_user_prompt(self, passages: List[Dict], title: str, unit: str,
                             allowed: Optional[List[int]] = None) -> str:
        from retrieval import build_context

        return f"""请为下面这份学术文献生成结构化摘要。

文献标题：{title}

文献片段（每段开头用「出处：第 N {unit}」标注了它来自第几{unit}；
同一个{unit}可能被拆成多个片段，它们的编号相同）：
{build_context(passages, max_chars=7000, unit=unit)}
{self._structure_note(passages)}{self._allowed_text(allowed, unit)}

请严格按下面的纯文本格式输出，不要输出 JSON、不要输出 Markdown 代码块：

## 研究背景
（2~4 句，句末用 [[p.编号]] 标注出处）
## 研究方法
（2~4 句，同上）
## 研究结果
（2~4 句，同上）
## 研究结论
（2~4 句，同上）
## 关键词
（5 个关键词，用中文顿号「、」分隔，不要标编号）"""

    def stream_summary(self, passages: List[Dict], title: str = "",
                       unit: str = "页",
                       allowed: Optional[List[int]] = None) -> Iterator[str]:
        stream = self._call(
            SUMMARY_SYSTEM_HEAD + citation_rule(unit, allowed),
            self._summary_user_prompt(passages, title, unit, allowed),
            temperature=0.3, max_tokens=1200, stream=True,
        )
        yield from self._deltas(stream)

    def generate_summary(self, passages: List[Dict], title: str = "",
                         unit: str = "页",
                         allowed: Optional[List[int]] = None) -> Dict:
        return self._collect(self.stream_summary(passages, title, unit, allowed))

    # ------------------------------------------------------------ 问答
    @staticmethod
    def _history_text(history: Optional[List[Dict]], unit: str,
                      answer_limit: int = 400) -> str:
        lines = []
        for turn in (history or [])[-8:]:
            question = str(turn.get("q") or "").strip()
            answer = str(turn.get("a") or "").strip()
            pages = turn.get("pages") or []
            if question:
                lines.append(f"用户：{question}")
            if answer:
                lines.append(f"助手：{answer[:answer_limit]}")
            if pages:
                lines.append("（上一轮引用：" +
                             "、".join(f"第 {p} {unit}" for p in pages) + "）")
        if not lines:
            return ""
        return ("之前的对话（按时间顺序，用来理解「它」「还有呢」这类指代，"
                "回答时不要原样重复）：\n" + "\n".join(lines) + "\n\n")

    def _qa_user_prompt(self, question: str, passages: List[Dict], unit: str,
                        history: Optional[List[Dict]] = None,
                        allowed: Optional[List[int]] = None) -> str:
        from retrieval import build_context

        history_text = self._history_text(history, unit)
        return f"""下面是一篇文献的检索片段，供你回答问题时参考。

文献片段（每段开头用「出处：第 N {unit}」标注了它来自第几{unit}；
同一个{unit}可能被拆成多个片段，它们的编号相同）：
{build_context(passages, max_chars=6000, unit=unit)}
{self._structure_note(passages)}{self._allowed_text(allowed, unit)}

{history_text}用户的问题：{question}

请按下面的规则回答：
1. 问题与文献内容相关时：只依据片段作答，每条结论后用 [[p.编号]] 标注出处。
   编号只能取片段开头的「第 N {unit}」里的 N；禁止编造编号，
   也不要把片段的先后次序当成编号（同一{unit}可能被拆成多个片段）。
2. 问题与文献无关时（闲聊、问你是谁、通用知识、写作、代码、翻译等）：
   直接正常回答，不要提"文献片段中未包含相关信息"，不要加任何页码标记。
3. 问题部分相关时：先依据片段回答相关部分并标注出处，再另起一段，
   以「（以下为通用知识，非文献内容）」开头补充片段之外的信息。
4. 上面有「之前的对话」时，当前问题可能是追问（如「还有呢」「再详细一点」「它的缺点」）：
   必须接着最后一轮的主题继续回答，不要另起话题，也不要要求用户补充背景。
5. 如果追问所需的信息在片段里找不到，就直说片段中未提及，不要猜成别的主题。
6. 用中文回答，语气自然，不要输出 JSON，也不要声称自己只能回答文献里的内容。"""

    def stream_answer(self, question: str, passages: List[Dict],
                      unit: str = "页", history: Optional[List[Dict]] = None,
                      allowed: Optional[List[int]] = None) -> Iterator[str]:
        stream = self._call(
            QA_SYSTEM_HEAD + citation_rule(unit, allowed),
            self._qa_user_prompt(question, passages, unit, history, allowed),
            temperature=0.2, max_tokens=900, stream=True,
        )
        yield from self._deltas(stream)

    def answer_question(self, question: str, passages: List[Dict],
                        unit: str = "页", history: Optional[List[Dict]] = None,
                        allowed: Optional[List[int]] = None) -> Dict:
        return self._collect(self.stream_answer(question, passages, unit,
                                                history, allowed))

    def rewrite_query(self, question: str, history: Optional[List[Dict]],
                      unit: str = "页") -> str:
        """把口语化问题/追问改写成可独立检索的完整问题。

        所有提问都会走一次（追问结合上文补全指代，独立问题保留原意），
        失败时由调用方退回原问题。
        """
        client = self._get_client()
        context = (self._history_text(history, unit, answer_limit=300)
                   if history else "")
        user = (
            f"{context}用户的问题：{question}\n\n"
            "请把这个问题改写成一句适合检索学术文献的完整中文问题："
            "补全省略的主语和指代（如「它」「这个」），提取其中的关键概念，"
            "去掉寒暄和口语化表达，但保留专有名词、数字和原意。"
            "如果问题已经是完整清晰的检索问题，就原样输出；"
            "如果问题与文献检索无关（闲聊、自我介绍等），也原样输出。"
            "只输出这一句问题，不要回答，不要解释。"
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system",
                 "content": "你是文献检索的查询改写器，只输出一句完整的中文检索问题。"},
                {"role": "user", "content": user},
            ],
            temperature=0.0, max_tokens=100, timeout=20,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return ""
        first = text.splitlines()[0].strip()
        for prefix in ("检索问题：", "改写后：", "问题："):
            if first.startswith(prefix):
                first = first[len(prefix):].strip()
        return first.strip('"“”')

    # 兼容旧调用名
    def rewrite_followup(self, question: str, history: Optional[List[Dict]],
                         unit: str = "页") -> str:
        return self.rewrite_query(question, history, unit)

    # ------------------------------------------------------------ 跨文献问答 / 综述
    @staticmethod
    def _library_context(sources: List[Dict], max_chars: int = 16000) -> str:
        blocks: List[str] = []
        used = 0
        for source in sources:
            header = f"【来源{source['n']}｜文献《{source['filename']}》第{source['page']}{source['unit']}"
            if source.get("section"):
                header += f"｜{source['section']}"
            header += "】"
            block = f"{header}\n{source['text']}"
            if used + len(block) > max_chars and blocks:
                break
            blocks.append(block)
            used += len(block)
        return "\n\n".join(blocks)

    @staticmethod
    def _library_rule(allowed: Optional[List[int]]) -> str:
        rule = (
            "【来源规则】每条结论后必须紧跟来源标记，格式严格为 [[s.编号]]。"
            "编号只能取自我给你的来源开头「来源N」里的那个 N，禁止编造。"
            "一句话综合多篇文献时可以连写多个标记，如 [[s.1]][[s.3]]。"
            "跨文献场景下不要使用 [[p.页码]]（同一个页码可能对应多篇文献）。"
            "找不到依据的判断就不要写。"
        )
        if allowed:
            numbers = "、".join(str(n) for n in allowed[:60])
            rule += f"本次可用的来源编号只有：{numbers}；除此以外的编号一律不得使用。"
        return rule

    LIBRARY_SYSTEM_HEAD = (
        "你是「智读小站」的跨文献研究助手，正在阅读用户个人文献库中的多篇文献。"
        "你只依据提供的来源片段作答，能够综合多篇文献的观点、指出异同，"
        "并在依据不足时明确说明，绝不编造文献或结论。"
    )

    def _library_qa_prompt(self, question: str, sources: List[Dict],
                           allowed: Optional[List[int]],
                           history: Optional[List[Dict]] = None) -> str:
        history_text = self._history_text(history, "页") if history else ""
        return f"""下面是从用户文献库中检索到的来源片段。

{self._library_context(sources, max_chars=15000)}
{self._structure_note(sources)}

{history_text}用户的问题：{question}

请按下面的规则回答：
1. 问题与文献库内容相关时：只依据来源作答，每条结论后用 [[s.编号]] 标注来源
   （编号范围见系统规则里的可用列表）。
2. 做跨文献综合：说明哪些文献支持同一结论、哪些观点互补或存在冲突；
   提到具体文献时用《标题》称呼。
3. 如果来源不足以回答，明确说「文献库中关于这一点没有找到相关材料」，不要猜。
4. 问题与文献库无关时（闲聊、通用知识），直接正常回答，不要加来源标记。
5. 用中文回答；比较类问题按维度组织，先给结论再给依据，不要逐段复述原文。"""

    def stream_library_answer(self, question: str, sources: List[Dict],
                              allowed: Optional[List[int]] = None,
                              history: Optional[List[Dict]] = None) -> Iterator[str]:
        stream = self._call(
            self.LIBRARY_SYSTEM_HEAD + self._library_rule(allowed),
            self._library_qa_prompt(question, sources, allowed, history),
            temperature=0.2, max_tokens=1200, stream=True,
        )
        yield from self._deltas(stream)

    def answer_library_question(self, question: str, sources: List[Dict],
                                allowed: Optional[List[int]] = None,
                                history: Optional[List[Dict]] = None) -> Dict:
        return self._collect(self.stream_library_answer(question, sources,
                                                        allowed, history))

    def _review_prompt(self, topic: str, sources: List[Dict],
                       mode: str, allowed: Optional[List[int]]) -> str:
        kind = "对比分析" if mode == "compare" else "文献综述"
        if mode == "compare":
            format_text = """## 对比维度
（先列出用于对比的 3~5 个维度，并说明为什么选这些维度）
## 逐篇要点
（按文献分别概括其在上述维度上的做法与结论，用《标题》开头）
## 共同点
## 主要差异
（观点、方法、数据或结论上的分歧要具体写出）
## 结论与选型建议
（基于来源给出适用场景或选择建议）"""
        else:
            format_text = """## 研究概览
（这组文献共同关注的问题、背景和总体脉络）
## 主要方法与技术路线
（归纳不同文献的做法，按路线归类，不要逐篇流水账）
## 关键发现与证据
（有数据、指标或明确结论的地方优先）
## 异同与分歧
（观点一致、互补或冲突之处）
## 局限与展望
## 结论"""
        subject = f"「{topic}」" if topic else "文献库中的全部研究"
        return f"""请基于下面的来源片段，围绕{subject}写一份{kind}。

{self._library_context(sources, max_chars=18000)}
{self._structure_note(sources)}

请严格按下面的纯文本格式输出，用 ## 作为小标题，不要输出 JSON 或 Markdown 代码块：
{format_text}

要求：
1. 每个观点后必须用 [[s.编号]] 标注来源，编号范围见系统规则里的可用列表；
2. 综合多篇文献，而不是把每篇的摘要拼接起来；有冲突或不同结论时要明确指出。
3. 只写来源支持的内容；材料不足的维度直接说明覆盖不足，不要编造数据。
4. 语言学术、简洁，总长度控制在 900~1500 字。"""

    def stream_review(self, topic: str, sources: List[Dict],
                      mode: str = "review",
                      allowed: Optional[List[int]] = None) -> Iterator[str]:
        stream = self._call(
            self.LIBRARY_SYSTEM_HEAD + self._library_rule(allowed),
            self._review_prompt(topic, sources, mode, allowed),
            temperature=0.25, max_tokens=2000, stream=True,
        )
        yield from self._deltas(stream)

    # ------------------------------------------------------------ 翻译
    def stream_translate(self, text: str, source_lang: str, target_lang: str) -> Iterator[str]:
        lang_names = {
            "zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文",
            "fr": "法文", "de": "德文", "es": "西班牙文", "ru": "俄文",
        }
        source_name = lang_names.get(source_lang, source_lang)
        target_name = lang_names.get(target_lang, target_lang)
        user = (
            f"请将下面用 <<<原文开始>>> 和 <<<原文结束>>> 标记的文本，"
            f"从{source_name}完整翻译成{target_name}。\n"
            f"要求：逐句翻译，必须译完全部内容，不得省略、不得跳过、不得总结、"
            f"不得只翻译开头；只在全部内容都译完后才停止。"
            f"学术术语要准确，保持原有段落结构。\n"
            f"只输出译文，不要输出任何解释或前后缀。\n\n"
            f"<<<原文开始>>>\n{text}\n<<<原文结束>>>"
        )
        stream = self._call(TRANSLATE_SYSTEM, user, temperature=0.1, max_tokens=4000, stream=True)
        yield from self._deltas(stream)

    def translate_text(self, text: str, source_lang: str = "en", target_lang: str = "zh") -> Dict:
        if source_lang == target_lang:
            return {"original": text, "translated": text,
                    "source_lang": source_lang, "target_lang": target_lang,
                    "method": "same_language"}
        collected = self._collect(self.stream_translate(text, source_lang, target_lang))
        # 注意：_collect 返回的键名是 text（摘要/问答用），翻译接口对外必须是 translated，
        # 这里显式映射，否则调用方拿到的是 null。
        return {
            "original": text,
            "translated": collected.get("text", ""),
            "source_lang": source_lang,
            "target_lang": target_lang,
            "source": collected.get("source", "openai"),
            "model": collected.get("model", self.model),
            "method": "llm_translation",
        }

    # ------------------------------------------------------------ 工具
    def _collect(self, chunks: Iterator[str]) -> Dict:
        from retrieval import citation_pages

        pieces: List[str] = []
        try:
            for piece in chunks:
                pieces.append(piece)
        except LLMUnavailable as exc:
            if pieces:
                print(f"[大模型] 流式中断：{exc}", file=sys.stderr)
            else:
                raise
        text = "".join(pieces).strip()
        return {
            "text": text,
            "citations": citation_pages(text),
            "source": "openai",
            "model": self.model,
        }

    # ------------------------------------------------------------ 指令解析
    def interpret_command(self, text: str, context: Optional[Dict] = None) -> Dict:
        """把自然语言指令解析成结构化动作。只发指令本身，不发送文献正文。"""
        client = self._get_client()
        ctx = context or {}
        unit = ctx.get("unit", "页")
        user = (
            f"当前文献类型：{ctx.get('source_type', 'pdf')}，"
            f"共有 {ctx.get('page_count', '未知')} 个{unit}，位置单位是「{unit}」。\n"
        )
        for key, label in (("recent_marks", "最近的标记"),
                           ("recent_edits", "最近的修改")):
            items = ctx.get(key) or []
            if items:
                user += f"{label}：{json.dumps(items, ensure_ascii=False)}\n"
        user += f"用户指令：{text}"
        messages = [
            {"role": "system", "content": COMMAND_SYSTEM},
            {"role": "user", "content": user},
        ]

        last_error: Optional[Exception] = None
        # 优先用 JSON 模式；个别兼容接口不支持 response_format 时退回普通模式
        for json_mode in (True, False):
            kwargs = {
                "model": self.model, "messages": messages,
                "temperature": 0.0, "max_tokens": 400, "timeout": 40,
            }
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = client.chat.completions.create(**kwargs)
                raw = (response.choices[0].message.content or "").strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.lstrip().lower().startswith("json"):
                        raw = raw.lstrip()[4:]
                data = json.loads(raw.strip())
                if not isinstance(data, dict):
                    raise ValueError("返回的不是 JSON 对象")
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise LLMUnavailable(f"指令解析失败：{last_error}")

    def status(self) -> Dict:
        return {
            "available": self.use_openai,
            "model": self.model,
            "base_url": self.base_url,
            "error": self.error,
        }


def get_llm() -> LLMIntegration:
    global _singleton
    if _singleton is None:
        _singleton = LLMIntegration()
    return _singleton


# 兼容旧代码里的 `from llm_integration import llm`
class _Lazy:
    def __getattr__(self, item):
        return getattr(get_llm(), item)


llm = _Lazy()
