# -*- coding: utf-8 -*-
"""兼容层。

原来的 ai_functions.py 里有一份独立的 SentenceTransformer 实例（导致模型被加载两遍），
现在统一收敛到 documents.py + retrieval.py + llm_integration.py。
这里只保留旧接口名，方便老代码/文档引用，不再重复加载任何模型。
"""
from documents import ensure_index_started, select_summary_passages  # noqa: F401
from llm_integration import get_llm

__all__ = ["answer_question", "generate_summary", "translate_text",
           "ensure_index_started", "select_summary_passages"]


def generate_summary(passages):
    return get_llm().generate_summary(passages)


def answer_question(question, passages):
    return get_llm().answer_question(question, passages)


def translate_text(text, source_lang="en", target_lang="zh"):
    return get_llm().translate_text(text, source_lang, target_lang)
