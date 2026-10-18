# -*- coding: utf-8 -*-
"""集中配置：路径、模型、检索参数。

设计原则（本项目硬性要求）：
1. 向量模型只用本机已缓存的副本，绝不联网下载 —— 通过 HF_HUB_OFFLINE 强制离线。
2. 模型加载失败必须显式暴露，不允许静默降级。
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

# 必须在读取任何 os.getenv 之前加载 .env，
# 否则 OPENAI_API_KEY / OPENAI_MODEL 会全部读成 None（这是个真实踩过的坑）。
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass

DATA_DIR = BASE_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
SECRET_FILE = DATA_DIR / "secret.key"
UPLOAD_ROOT = BASE_DIR / "uploads"

# ---------------------------------------------------------------- 离线开关
# 默认强制离线：只用手上已有的模型副本，绝不偷偷联网下载。
# 新机器第一次部署时，在 .env 里加一行 EMBEDDING_ALLOW_DOWNLOAD=1，
# 程序就会在本地找不到模型时，从 HF_ENDPOINT（默认国内镜像 hf-mirror.com）下一次；
# 下完把那行删掉即可恢复"绝不联网"的行为。
_allow_download = os.getenv("EMBEDDING_ALLOW_DOWNLOAD", "").strip().lower() in (
    "1", "true", "yes", "on")
if _allow_download:
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
else:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_ENDPOINT"] = os.getenv("HF_ENDPOINT") or "https://hf-mirror.com"

# ---------------------------------------------------------------- 向量模型
EMBEDDING_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
# 学术片段远长于 512 token 的场景很少，限制长度能显著减少 padding 开销
EMBEDDING_MAX_SEQ_LENGTH = 512
EMBEDDING_BATCH_SIZE = 32
# 升配到 GPU 时把这里改成 "cuda"（需要装 CUDA 版 torch）
EMBEDDING_DEVICE = os.getenv("EMBEDDING_DEVICE", "auto")

# ---------------------------------------------------------------- 分块
CHUNK_TARGET_CHARS = 700
CHUNK_MAX_CHARS = 950
CHUNK_OVERLAP_SENTENCES = 1

# ---------------------------------------------------------------- 检索
RETRIEVE_TOP_K = 6
RETRIEVE_CANDIDATES = 24
HYBRID_VECTOR_WEIGHT = 0.6
HYBRID_BM25_WEIGHT = 0.4
RERANK_WEIGHT = 0.25

# ---------------------------------------------------------------- 神经重排
# cross-encoder 重排：把混合检索的候选片段和问题一起送进模型打分，
# 比纯向量/词面匹配更能识别"这段话是否真的回答了问题"。
# 模型缺失或加载失败时自动退回原来的词面重排，不影响提问。
RERANKER_MODEL_NAME = "BAAI/bge-reranker-base"
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
RERANKER_DEVICE = os.getenv("RERANKER_DEVICE", "auto")
RERANK_CANDIDATES = 18       # 送进 cross-encoder 的候选数
RERANK_MAX_LENGTH = 512
RERANK_BATCH_SIZE = 8
RERANK_NEURAL_WEIGHT = 0.7   # 最终排序 = 0.7 神经分 + 0.3 混合检索分

# ---------------------------------------------------------------- 账号 / 会话
SESSION_COOKIE = "zd_session"
SESSION_MAX_AGE = 7 * 24 * 3600
SESSION_SALT = "zhidu-xiaozhan-session-v1"
MIN_PASSWORD_LEN = 6
MAX_USERNAME_LEN = 32

# ---------------------------------------------------------------- 上传
# 支持的文档格式：PDF 有真实页码；txt / md / docx 没有"页"，
# 按段落合成编号单元，出处标记为"第 N 段"，跳转功能照旧保留。
ALLOWED_EXTENSIONS = {"pdf", "txt", "md", "markdown", "docx"}
TEXT_EXTENSIONS = {"txt", "markdown", "md"}
DOCX_EXTENSIONS = {"docx"}
# 文本类文件合成单元的参考长度（字符）
TEXT_UNIT_CHARS = 1600
MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB

# ---------------------------------------------------------------- OCR
# 扫描件/图片型 PDF：某页提取到的文字少于阈值、且页面上有图片时，
# 先用 RapidOCR 识别该页，再进入正常分块与索引流程。
# 普通文字 PDF 不会走 OCR；混合型 PDF 只 OCR 文字过少的页。
OCR_ENABLED = os.getenv("OCR_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
OCR_MIN_PAGE_CHARS = int(os.getenv("OCR_MIN_PAGE_CHARS", "40"))
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
OCR_SCORE_THRESHOLD = float(os.getenv("OCR_SCORE_THRESHOLD", "0.5"))
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "0"))  # 0 表示不限制

# ---------------------------------------------------------------- 管理后台
# 管理员默认只能看文献元数据；处理举报时按单篇签发短期查看令牌，
# 每次授权和实际访问都会写入审计日志。
ADMIN_VIEW_SALT = "zhidu-xiaozhan-admin-view-v1"
ADMIN_VIEW_MAX_AGE = int(os.getenv("ADMIN_VIEW_MAX_AGE", "600"))  # 秒
AUDIT_LOG_FILE = DATA_DIR / "audit.log"
AUDIT_MAX_BYTES = 10 * 1024 * 1024  # 超过后轮转为 audit.log.1

# ---------------------------------------------------------------- 结构化提取
# 表格、公式、图注在解析阶段识别并加标记，供摘要/问答使用。
# 有完整线框的表格还原行列；只有横线的 booktabs 表格用横线定位区域后还原。
STRUCTURED_ENABLED = os.getenv("STRUCTURED_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")

# ---------------------------------------------------------------- LLM
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")


def resolve_local_model(name: str = EMBEDDING_MODEL_NAME) -> str:
    """在本机 HuggingFace 缓存里找出模型快照目录；找不到就退回模型名。

    这样即使断网，只要模型已经缓存过，就一定能加载成功。
    """
    explicit = os.getenv("EMBEDDING_MODEL_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    hub = os.getenv("HF_HUB_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub"
    )
    model_dir = Path(hub) / ("models--" + name.replace("/", "--"))
    snapshots = model_dir / "snapshots"
    if snapshots.is_dir():
        for snap in sorted(snapshots.iterdir(), reverse=True):
            if (snap / "config.json").exists():
                return str(snap)
    return name


def resolve_reranker_path() -> str:
    """找本机已下载的重排模型；找不到返回空串，由调用方降级。"""
    explicit = os.getenv("RERANKER_MODEL_PATH")
    if explicit and Path(explicit).exists():
        return explicit

    hub = os.getenv("HF_HUB_CACHE") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface", "hub"
    )
    model_dir = Path(hub) / ("models--" + RERANKER_MODEL_NAME.replace("/", "--"))
    snapshots = model_dir / "snapshots"
    if snapshots.is_dir():
        for snap in sorted(snapshots.iterdir(), reverse=True):
            if (snap / "config.json").exists():
                return str(snap)

    env_root = os.getenv("MODEL_DIR")
    if env_root:
        local = Path(env_root) / RERANKER_MODEL_NAME.split("/")[-1]
        if (local / "config.json").exists():
            return str(local)
    local = BASE_DIR / "models" / RERANKER_MODEL_NAME.split("/")[-1]
    if (local / "config.json").exists():
        return str(local)
    return ""


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
