# -*- coding: utf-8 -*-
"""生成智读小站演示用的两篇虚构论文（不包含任何真实研究或个人数据）。

用法：
    python samples/generate_demo_pdfs.py
"""
from pathlib import Path
import tempfile
import os

import fitz
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT_REG = r"C:\Windows\Fonts\arial.ttf"
FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
REG_METRICS = fitz.Font(fontfile=FONT_REG)
BOLD_METRICS = fitz.Font(fontfile=FONT_BOLD)

PAGE_W, PAGE_H = 595, 842
MARGIN = 56
CONTENT_W = PAGE_W - 2 * MARGIN
BODY = 10.5
INK = (0.08, 0.09, 0.12)
MUTED = (0.42, 0.45, 0.5)


class Writer:
    def __init__(self, doc):
        self.page = doc.new_page(width=PAGE_W, height=PAGE_H)
        self.page.insert_font(fontname="F0", fontfile=FONT_REG)
        self.page.insert_font(fontname="F1", fontfile=FONT_BOLD)
        self.y = MARGIN

    def wrap(self, text, size, fontname="F0", width=CONTENT_W):
        metrics = BOLD_METRICS if fontname == "F1" else REG_METRICS
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if metrics.text_length(trial, fontsize=size) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def center(self, text, size, fontname="F0", color=INK, gap=6):
        metrics = BOLD_METRICS if fontname == "F1" else REG_METRICS
        for line in self.wrap(text, size, fontname):
            width = metrics.text_length(line, fontsize=size)
            self.page.insert_text(((PAGE_W - width) / 2, self.y), line,
                                  fontname=fontname, fontsize=size, color=color)
            self.y += size * 1.4
        self.y += gap

    def heading(self, text, size=13.5, before=8, after=5):
        self.y += before
        for line in self.wrap(text, size, "F1"):
            self.page.insert_text((MARGIN, self.y), line, fontname="F1",
                                  fontsize=size, color=INK)
            self.y += size * 1.35
        self.y += after

    def para(self, text, size=BODY, after=6, indent=0):
        for line in self.wrap(text, size, "F0", CONTENT_W - indent):
            self.page.insert_text((MARGIN + indent, self.y), line, fontname="F0",
                                  fontsize=size, color=INK)
            self.y += size * 1.45
        self.y += after

    def bullet(self, text):
        self.para("•  " + text, after=3)

    def rule(self):
        self.page.draw_line((MARGIN, self.y), (PAGE_W - MARGIN, self.y),
                            color=(0.75, 0.78, 0.85), width=0.8)
        self.y += 12

    def box(self, text, size=10.5, fill=(0.94, 0.96, 1.0), center=False):
        inner_w = CONTENT_W - 20
        lines = self.wrap(text, size, "F0", inner_w)
        height = len(lines) * size * 1.5 + 18
        rect = fitz.Rect(MARGIN, self.y, PAGE_W - MARGIN, self.y + height)
        self.page.draw_rect(rect, color=(0.62, 0.68, 0.82), fill=fill, width=0.8)
        self.page.insert_textbox(rect + (10, 9, -10, -9), text, fontname="F0",
                                 fontsize=size, color=INK,
                                 align=1 if center else 0)
        self.y = rect.y1 + 10

    def table(self, rows, widths, size=9, row_h=21):
        x = MARGIN
        y = self.y
        for r, row in enumerate(rows):
            cx = x
            for c, cell in enumerate(row):
                rect = fitz.Rect(cx, y + r * row_h, cx + widths[c], y + (r + 1) * row_h)
                if r == 0:
                    self.page.draw_rect(rect, color=None, fill=(0.88, 0.91, 0.97))
                self.page.draw_rect(rect, color=(0.58, 0.63, 0.72), width=0.6)
                self.page.insert_textbox(rect + (4, 5, -4, -3), str(cell),
                                         fontname="F1" if r == 0 else "F0",
                                         fontsize=size, color=INK, align=0)
                cx += widths[c]
        self.y = y + row_h * len(rows) + 10

    def number(self, current, total):
        self.page.insert_text((PAGE_W / 2 - 34, PAGE_H - 28),
                              f"Page {current} of {total}",
                              fontname="F0", fontsize=8, color=MUTED)

    def check(self, name):
        if self.y > PAGE_H - 45:
            print(f"WARNING: {name} overflows (y={self.y:.0f})")


def ocr_page(doc, current, total):
    """第 8 页只放一张扫描图，没有任何文字层，用来触发 OCR。"""
    width, height = 1654, 2339  # A4 @ 200 DPI
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(FONT_BOLD, 46)
    body_font = ImageFont.truetype(FONT_REG, 34)
    small_font = ImageFont.truetype(FONT_REG, 24)

    y = 150
    draw.text((150, y), "6. Limitations and Conclusion", font=title_font, fill="black")
    y += 85

    blocks = [
        ("h", "Limitations"),
        ("p", "The benchmarks are English-only and synthetic."),
        ("p", "Results may not transfer to other languages or domains."),
        ("p", "Scanned pages depend on OCR and may contain recognition errors."),
        ("p", "Low-confidence OCR text is not filtered in this demo."),
        ("p", "Long documents use only the six highest-ranked chunks."),
        ("p", "Multi-hop questions across distant pages remain difficult."),
        ("p", "The system needs a local embedding model and a hosted LLM."),
        ("h", "Conclusion"),
        ("p", "LightRAG improves answer quality and citation accuracy with a small latency cost."),
        ("h", "Future Work"),
        ("p", "Future work includes multilingual benchmarks, OCR confidence filtering, and user studies."),
    ]

    for kind, text in blocks:
        if kind == "h":
            y += 10
            draw.text((150, y), text, font=title_font, fill="black")
            y += 70
            continue
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if body_font.getlength(trial) <= 1350:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        for line in lines:
            draw.text((150, y), line, font=body_font, fill="black")
            y += 52
        y += 18

    draw.text((760, 2250), f"Page {current} of {total}", font=small_font, fill=(110, 110, 110))
    tmp = Path(tempfile.gettempdir()) / "zhidu_ocr_demo_page.png"
    image.save(tmp)
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(page.rect, filename=str(tmp))
    os.remove(tmp)


def build_lightrag(path):
    total = 8
    doc = fitz.open()

    # ---------- page 1 ----------
    w = Writer(doc)
    w.center("SYNTHETIC DEMO DOCUMENT  •  NOT REAL RESEARCH", 8, color=MUTED, gap=26)
    w.center("LightRAG: A Lightweight Retrieval-Augmented Framework "
             "for Page-Aware Academic Question Answering", 19, "F1", gap=10)
    w.center("Demo Authors, Synthetic Systems Lab", 11, gap=2)
    w.center("demo@example.org  •  Demo Technical Report 2026-01", 9.5, color=MUTED, gap=14)
    w.rule()
    w.heading("Abstract", 13, before=2)
    w.para("Academic literature question answering requires answers that are both accurate and "
           "verifiable. We present LightRAG, a lightweight retrieval-augmented framework that combines "
           "page-aware chunking, hybrid dense-sparse retrieval, cross-encoder reranking, and "
           "citation-grounded generation. LightRAG splits documents along page and section boundaries, "
           "retrieves candidates with Qwen3-Embedding and BM25, fuses their scores with a weighted sum, "
           "and reranks the top candidates with a cross-encoder before generation. On two synthetic "
           "benchmarks, SciQA-Demo and PaperQA-Demo, LightRAG achieves an F1 of 0.82, outperforming "
           "BM25-RAG (0.69) and a dense-only baseline (0.74). An ablation study shows that page-aware "
           "chunking and reranking contribute 6.1 and 4.7 F1 points, respectively. The system answers "
           "in 1.8 seconds on average on a single consumer GPU. These results suggest that lightweight "
           "hybrid retrieval is a practical foundation for trustworthy literature assistants.")
    w.heading("Keywords", 11, before=6, after=2)
    w.para("retrieval-augmented generation; academic question answering; hybrid retrieval; "
           "cross-encoder reranking; citation grounding", 10, after=4)
    w.number(1, total)
    w.check("page 1")

    # ---------- page 2 ----------
    w = Writer(doc)
    w.heading("1. Introduction", 14, before=0)
    w.para("Researchers increasingly rely on large language models to skim, summarize, and compare "
           "academic papers. A general-purpose model, however, may produce fluent statements that are "
           "not supported by the source document. In a literature review, an unsupported sentence is "
           "worse than a missing one: it can send a reader to the wrong method, dataset, or conclusion.")
    w.para("Retrieval-augmented generation (RAG) addresses this problem by retrieving passages before "
           "generation. Yet three practical issues remain. First, fixed-size chunking destroys page and "
           "section boundaries, so a citation cannot reliably point back to the original page. Second, "
           "dense retrieval captures paraphrase but often misses exact terms such as model names, "
           "dataset names, and symbols. Third, a single retrieval stage returns noisy candidates, and "
           "the generator may over-trust them.")
    w.para("This demo report introduces LightRAG, a deliberately small framework that targets these "
           "issues. The main contributions are:")
    w.bullet("Page-aware chunking that preserves page, section, and block-type metadata for every chunk.")
    w.bullet("Hybrid retrieval that fuses dense and sparse scores, followed by cross-encoder reranking.")
    w.bullet("A citation-grounded prompt that requires every factual claim to carry a page reference.")
    w.bullet("A synthetic evaluation that reports F1, citation accuracy, latency, and an ablation study.")
    w.para("The remainder of this report describes related work, the method, the experimental setup, "
           "results, limitations, and conclusions. All data in this document are fictional and were "
           "created solely to demonstrate a document-reading interface.")
    w.number(2, total)
    w.check("page 2")

    # ---------- page 3 ----------
    w = Writer(doc)
    w.heading("2. Related Work", 14, before=0)
    w.heading("2.1 Retrieval-Augmented Generation", 11.5, before=4)
    w.para("RAG combines a retriever with a generator. The retriever selects passages from a corpus, "
           "and the generator conditions its answer on those passages. This design reduces the need to "
           "store factual knowledge in model parameters and makes it possible to attach evidence to an "
           "answer. However, the quality of the final answer is bounded by the quality of retrieval.")
    w.heading("2.2 Sparse and Dense Retrieval", 11.5, before=4)
    w.para("BM25 is a strong sparse baseline. It rewards exact term overlap and remains competitive "
           "when queries contain rare names or symbols. Dense retrievers such as DPR and Contriever "
           "map queries and passages into a shared vector space and handle paraphrase better. The two "
           "families fail in different ways, which motivates score fusion rather than a single retriever.")
    w.heading("2.3 Cross-Encoder Reranking", 11.5, before=4)
    w.para("A cross-encoder reads a query and a candidate passage jointly and produces a relevance "
           "score. It is more accurate than a bi-encoder but too expensive to run over an entire corpus. "
           "A common compromise is to retrieve a moderate candidate set and rerank only that set. In "
           "this demo, fifty candidates are reranked down to six passages.")
    w.heading("2.4 Citation-Grounded Question Answering", 11.5, before=4)
    w.para("Prior work has explored sentence-level attribution and post-hoc citation verification. "
           "These methods are valuable but add another model to the pipeline. LightRAG instead keeps "
           "page identifiers in the retrieved context and instructs the generator to cite them directly. "
           "This is simpler to inspect and easier to evaluate in a document reader.")
    w.number(3, total)
    w.check("page 3")

    # ---------- page 4 ----------
    w = Writer(doc)
    w.heading("3. Method", 14, before=0)
    w.heading("3.1 Page-Aware Chunking", 11.5, before=4)
    w.para("The parser walks through the PDF page by page. Text is split at section headings and then "
           "packed into chunks of at most 512 tokens with a 20 percent overlap. Every chunk stores its "
           "page number, section title, and block type. Tables and displayed formulas are kept as "
           "separate chunks so that their structure is not merged into surrounding prose. This metadata "
           "is later copied into the citation attached to each answer.")
    w.heading("3.2 Hybrid Retrieval", 11.5, before=4)
    w.para("Given a question q and a candidate chunk d, LightRAG computes a dense score with "
           "Qwen3-Embedding and a sparse score with BM25. Both scores are normalized to the interval "
           "[0, 1] and combined with a weighted sum:")
    w.box("S(q, d) = alpha * S_dense(q, d) + (1 - alpha) * S_bm25(q, d)", size=11.5, center=True)
    w.para("Unless stated otherwise, alpha is set to 0.6, which gives the dense retriever slightly more "
           "weight while retaining exact-term matching. BM25 uses k1 = 1.5 and b = 0.75. The fusion "
           "produces fifty candidates per question. A sensitivity check in Section 5 shows that "
           "alpha = 0.5 and alpha = 0.7 perform within 0.8 F1 points of the default.")
    w.heading("3.3 Cross-Encoder Reranking", 11.5, before=4)
    w.para("The fifty fused candidates are passed to a bge-reranker-base cross-encoder. The reranker "
           "scores each query-passage pair jointly and keeps the top six passages. On the demo GPU this "
           "step takes about 12 milliseconds per query. Reranking is especially helpful for questions "
           "that mention a model name or a numeric result, because the cross-encoder can compare the "
           "exact wording of the question with the candidate passage.")
    w.number(4, total)
    w.check("page 4")

    # ---------- page 5 ----------
    w = Writer(doc)
    w.heading("3.4 Citation-Grounded Generation", 12.5, before=0)
    w.para("The generator receives the question, the six reranked passages, and a strict instruction: "
           "every factual sentence must end with a page reference in the form [p.N]. If the passages do "
           "not contain enough evidence, the model must say so instead of guessing. The temperature is "
           "set to 0.2. A post-processing check parses the cited page numbers and highlights them in the "
           "reader; citations that do not correspond to a supplied passage are discarded.")
    w.heading("Algorithm 1: LightRAG query pipeline", 11.5, before=6)
    w.box("Input: question q, document D, fusion weight alpha\n"
          "1. Detect explicit page references in q; if present, read those pages directly.\n"
          "2. Otherwise, retrieve 50 candidates with dense + BM25 score fusion.\n"
          "3. Rerank the candidates with a cross-encoder and keep the top 6.\n"
          "4. Build a prompt containing page-labeled passages.\n"
          "5. Generate an answer with a [p.N] citation for every factual claim.\n"
          "6. Validate citations and return the answer with highlighted evidence.", size=9.6)
    w.para("Two design choices are worth emphasizing. First, explicit page questions bypass semantic "
           "retrieval entirely: if a user asks about page 7, the system reads page 7. Second, the "
           "generator is not allowed to invent a page number. This constraint makes the interface "
           "predictable and gives the reader a direct way to verify each statement.")
    w.number(5, total)
    w.check("page 5")

    # ---------- page 6 ----------
    w = Writer(doc)
    w.heading("4. Experiments", 14, before=0)
    w.heading("4.1 Datasets and Metrics", 11.5, before=3)
    w.para("SciQA-Demo contains 1,200 question-answer pairs built from 40 synthetic papers. "
           "PaperQA-Demo contains 800 pairs from 25 synthetic papers. Every question is annotated with "
           "the page that contains the answer. We report exact match (EM), token-level F1, citation "
           "accuracy (CA), and average latency. CA counts a citation as correct when the cited page "
           "overlaps the annotated evidence page.")
    w.heading("4.2 Baselines", 11.5, before=3)
    w.para("BM25-RAG uses sparse retrieval only. DPR uses a dense retriever without reranking. "
           "RAG-base uses dense retrieval with the same generator but no page-aware metadata. LightRAG "
           "uses hybrid retrieval and reranking. All methods use the same six-passage context budget.")
    w.heading("Table 1: Main results on the two synthetic benchmarks", 10.5, before=4, after=3)
    w.table([
        ["Method", "Retrieval", "F1", "CA", "Latency"],
        ["BM25-RAG", "Sparse", "0.69", "0.61", "0.9 s"],
        ["DPR", "Dense", "0.74", "0.66", "1.3 s"],
        ["RAG-base", "Dense", "0.78", "0.71", "1.5 s"],
        ["LightRAG", "Hybrid + rerank", "0.82", "0.79", "1.8 s"],
    ], [118, 145, 70, 70, 80])
    w.para("F1 is computed from precision P and recall R:", after=4)
    w.box("F1 = 2 * P * R / (P + R),   P = TP / (TP + FP),   R = TP / (TP + FN)",
          size=10.5, center=True)
    w.para("LightRAG improves F1 by 0.04 over the strongest dense baseline and by 0.13 over BM25-RAG. "
           "The gain in citation accuracy is larger, which is important for a reading interface: a "
           "reader can verify an answer only if the cited page actually contains the evidence.")
    w.number(6, total)
    w.check("page 6")

    # ---------- page 7 ----------
    w = Writer(doc)
    w.heading("5. Results and Discussion", 14, before=0)
    w.heading("5.1 Ablation Study", 11.5, before=3)
    w.para("Table 2 removes one component at a time. Page-aware chunking has the largest effect on "
           "citation accuracy, while reranking has the largest effect on F1 for questions containing "
           "exact model or dataset names.")
    w.heading("Table 2: Ablation results (F1)", 10.5, before=4, after=3)
    w.table([
        ["Variant", "F1", "CA"],
        ["LightRAG (full)", "0.82", "0.79"],
        ["w/o reranking", "0.773", "0.72"],
        ["w/o page-aware chunking", "0.759", "0.64"],
        ["w/o hybrid (dense only)", "0.781", "0.70"],
    ], [250, 90, 90])
    w.para("Removing reranking costs 4.7 F1 points. Removing page-aware chunking costs 6.1 F1 points "
           "and 15 citation-accuracy points, confirming that page metadata is not cosmetic. Replacing "
           "hybrid retrieval with dense-only retrieval costs 3.9 F1 points, mostly on questions that "
           "mention a precise model name or a numeric result.")
    w.heading("5.2 Error Analysis", 11.5, before=4)
    w.para("We manually inspected 100 incorrect answers. Retrieval missed the evidence in 38 cases, "
           "OCR noise affected 27 cases, multi-hop questions accounted for 21 cases, and generation "
           "errors accounted for 14 cases. The first two categories suggest that better page parsing "
           "and OCR confidence filtering are more valuable than a larger generator.")
    w.heading("5.3 Discussion", 11.5, before=4)
    w.para("The latency cost of LightRAG is 0.4 seconds relative to RAG-base. In a reading interface "
           "this is acceptable because the answer streams token by token and the user can continue "
           "reading while the response is produced. The results support a simple claim: keeping page "
           "structure and combining two retrieval signals is a practical way to make a literature "
           "assistant more trustworthy without a large increase in system complexity.")
    w.number(7, total)
    w.check("page 7")

    # ---------- page 8 (image only) ----------
    ocr_page(doc, 8, total)

    doc.set_metadata({
        "title": "LightRAG: A Lightweight Retrieval-Augmented Framework for Page-Aware Academic Question Answering",
        "author": "Demo Authors, Synthetic Systems Lab",
        "subject": "Synthetic demo document for Zhidu Xiaozhan",
        "keywords": "RAG, hybrid retrieval, reranking, citation grounding",
        "creator": "Zhidu Xiaozhan demo generator",
    })
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    print(f"wrote {path} ({path.stat().st_size/1024:.0f} KB)")


def build_bm25(path):
    total = 5
    doc = fitz.open()

    w = Writer(doc)
    w.center("SYNTHETIC DEMO DOCUMENT  •  NOT REAL RESEARCH", 8, color=MUTED, gap=24)
    w.center("BM25-RAG: A Sparse-Retrieval Baseline for Academic Question Answering",
             18, "F1", gap=10)
    w.center("Demo Authors, Synthetic Systems Lab", 11, gap=2)
    w.center("demo@example.org  •  Demo Technical Report 2026-01", 9.5, color=MUTED, gap=14)
    w.rule()
    w.heading("Abstract", 13, before=2)
    w.para("We describe BM25-RAG, a minimal retrieval-augmented baseline for academic question "
           "answering. The system splits documents into fixed 512-token chunks, retrieves the top six "
           "chunks with BM25, and asks a generator to answer with page citations. On the same synthetic "
           "benchmarks used by LightRAG, BM25-RAG reaches an F1 of 0.69 and a citation accuracy of 0.61 "
           "with an average latency of 0.9 seconds. It is fast, interpretable, and requires no GPU for "
           "retrieval, but it struggles when the question paraphrases the source or uses a synonym "
           "instead of the exact term. The baseline is useful for measuring how much a more elaborate "
           "hybrid pipeline actually contributes.")
    w.heading("Keywords", 11, before=6, after=2)
    w.para("BM25; sparse retrieval; retrieval-augmented generation; baseline; academic QA", 10)
    w.number(1, total)
    w.check("bm25 page 1")

    w = Writer(doc)
    w.heading("1. Introduction", 13.5, before=0)
    w.para("Retrieval-augmented generation is often evaluated with a dense retriever. A sparse "
           "baseline is equally important: it is cheap, transparent, and surprisingly strong on "
           "questions that contain exact terminology. This report documents such a baseline so that "
           "the contribution of hybrid retrieval and reranking can be measured against something "
           "simple rather than against nothing.")
    w.heading("2. Method", 13.5, before=4)
    w.para("Documents are split into fixed 512-token chunks with no page-aware boundaries. The "
           "retriever scores every chunk with BM25:")
    w.box("BM25(q, d) = sum_t IDF(t) * (tf * (k1 + 1)) / "
          "(tf + k1 * (1 - b + b * |d| / avgdl))", size=10.2, center=True)
    w.para("We use k1 = 1.5 and b = 0.75, the standard defaults. The top six chunks are placed in the "
           "prompt with their page numbers, and the generator is asked to cite a page for every factual "
           "claim. No reranker and no dense retriever are used. The design deliberately favors "
           "simplicity over accuracy so that its failure modes are easy to interpret.")
    w.number(2, total)
    w.check("bm25 page 2")

    w = Writer(doc)
    w.heading("3. Experiments", 13.5, before=0)
    w.para("We evaluate on SciQA-Demo and PaperQA-Demo, the same synthetic benchmarks used in the "
           "LightRAG report. Metrics are token-level F1, citation accuracy (CA), and average latency. "
           "All systems use the same six-passage context budget and the same generator.")
    w.heading("Table 1: Sparse baseline versus hybrid system", 10.5, before=4, after=3)
    w.table([
        ["Method", "F1", "CA", "Latency"],
        ["BM25-RAG", "0.69", "0.61", "0.9 s"],
        ["Dense-only baseline", "0.74", "0.66", "1.3 s"],
        ["LightRAG (hybrid + rerank)", "0.82", "0.79", "1.8 s"],
    ], [205, 80, 80, 90])
    w.para("BM25-RAG is 0.9 seconds faster than LightRAG but loses 0.13 F1 and 0.18 citation accuracy. "
           "The gap is concentrated in questions that use synonyms or paraphrase the source. On "
           "questions containing an exact model name or dataset name, the gap narrows to 0.03 F1.")
    w.heading("3.1 Where the baseline wins", 11.5, before=4)
    w.para("BM25-RAG is competitive on exact-term queries and on documents with highly distinctive "
           "vocabulary. It is also the easiest system to debug: a developer can inspect the matched "
           "terms and understand why a chunk was retrieved. For a small corpus with stable terminology, "
           "this baseline may be sufficient.")
    w.number(3, total)
    w.check("bm25 page 3")

    w = Writer(doc)
    w.heading("4. Discussion", 13.5, before=0)
    w.para("The baseline has three strengths. It runs on CPU, its retrieval scores are interpretable, "
           "and it handles rare exact terms well. It also has three weaknesses. First, vocabulary "
           "mismatch is fatal: if the question says \"retrieval quality\" and the paper says "
           "\"ranking effectiveness\", BM25 may miss the passage. Second, fixed-size chunking breaks "
           "tables and formulas across boundaries. Third, the lack of a reranker leaves the generator "
           "with no second chance to reject a noisy candidate.")
    w.para("These tradeoffs suggest a simple deployment rule. If latency and hardware are the dominant "
           "constraints, start with BM25-RAG and add a reranker only if evaluation shows a clear gain. "
           "If answers must be citation-accurate across paraphrased questions, a hybrid pipeline is "
           "worth the additional 0.9 seconds.")
    w.heading("4.1 Threats to Validity", 11.5, before=4)
    w.para("The benchmarks are synthetic and English-only. The reported numbers illustrate the behavior "
           "of the pipelines rather than the performance of a production system. A real evaluation "
           "should include domain experts, naturally occurring questions, and documents with noisy OCR.")
    w.number(4, total)
    w.check("bm25 page 4")

    w = Writer(doc)
    w.heading("5. Conclusion", 13.5, before=0)
    w.para("BM25-RAG is a useful lower bound for academic question answering. It is fast and "
           "transparent, and it establishes how much a hybrid retrieval and reranking pipeline must "
           "improve before its extra complexity is justified. On the synthetic benchmarks, that "
           "improvement is substantial for paraphrased questions and citation accuracy.")
    w.heading("Limitations", 11.5, before=4)
    w.bullet("English-only synthetic benchmark; no human evaluation.")
    w.bullet("Fixed-size chunking can split tables and formulas.")
    w.bullet("No semantic matching; synonym and paraphrase queries are fragile.")
    w.bullet("Citation accuracy depends on the quality of page metadata.")
    w.heading("References (fictional)", 11.5, before=4)
    w.para("[1] Demo Authors. LightRAG: A Lightweight Retrieval-Augmented Framework. Demo Technical "
           "Report, 2026.", 9.5, after=2)
    w.para("[2] Demo Authors. A Synthetic Benchmark for Academic Question Answering. Demo Technical "
           "Report, 2026.", 9.5, after=2)
    w.para("[3] Demo Authors. Sparse Retrieval Baselines Revisited. Demo Technical Report, 2026.",
           9.5, after=2)
    w.number(5, total)
    w.check("bm25 page 5")

    doc.set_metadata({
        "title": "BM25-RAG: A Sparse-Retrieval Baseline for Academic Question Answering",
        "author": "Demo Authors, Synthetic Systems Lab",
        "subject": "Synthetic demo document for Zhidu Xiaozhan",
        "keywords": "BM25, sparse retrieval, RAG, baseline",
        "creator": "Zhidu Xiaozhan demo generator",
    })
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    print(f"wrote {path} ({path.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    build_lightrag(ROOT / "lightrag_demo.pdf")
    build_bm25(ROOT / "bm25_rag_demo.pdf")
