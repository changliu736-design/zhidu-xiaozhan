# 智读小站 - 后端 API 文档

---

## 📋 概述

- **框架**：Flask
- **PDF 解析**：PyMuPDF（逐页提取，保留页码）
- **扫描件 OCR**：RapidOCR（ONNX Runtime，CPU），文字过少的页面自动 OCR 后再建索引
- **结构化提取**：表格按行列还原，公式与图注/表注单独标记，一并写入检索索引
- **语义检索**：Qwen3-Embedding-0.6B（1024 维）+ faiss 余弦索引 + 自实现 BM25
- **神经重排**：bge-reranker-base cross-encoder 精排（模型缺失时自动退回词面重排）
- **查询改写**：每次提问先改写成完整检索问题（追问会结合上文补全指代）
- **引用校验**：答案里的页码必须真实出现在本次检索片段中，越界标记会被移除
- **AI 能力**：OpenAI 兼容接口（当前配置为 DeepSeek）
- **鉴权**：bcrypt 密码 + itsdangerous 签名令牌（Cookie 与 Bearer 双通道）
- **管理后台**：用户列表、封禁 / 解封、删除违规文献、审计日志；
  默认只看文献元数据，处理举报时按单篇签发短期授权才能查看原文

无 GPU 时自动退回 CPU 运行。

---

## 🔧 配置说明

在 `backend/.env` 中配置：

```
OPENAI_API_KEY=你的Key
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `EMBEDDING_DEVICE` | `auto` | `auto` / `cuda` / `cpu` |
| `EMBEDDING_MODEL_PATH` | 自动查找本机缓存 | 手动指定模型目录 |
| `RERANKER_ENABLED` | `1` | 设为 `0` 关闭神经重排，直接用混合检索排序 |
| `RERANKER_DEVICE` | `auto` | 重排模型推理设备 |
| `RERANKER_MODEL_PATH` | `D:\model\bge-reranker-base` | 手动指定重排模型目录 |
| `OCR_ENABLED` | `1` | 设为 `0` 关闭扫描件 OCR |
| `OCR_MIN_PAGE_CHARS` | `40` | 页面文字少于该值时视为扫描页 |
| `OCR_DPI` | `200` | OCR 前渲染 PDF 页的分辨率 |
| `OCR_SCORE_THRESHOLD` | `0.5` | 低于该置信度的识别行会被丢弃 |
| `OCR_MAX_PAGES` | `0` | 单篇最多 OCR 多少页，`0` 表示不限制 |
| `STRUCTURED_ENABLED` | `1` | 设为 `0` 关闭表格 / 公式 / 图注识别 |
| `ADMIN_VIEW_MAX_AGE` | `600` | 举报核查查看授权的有效秒数 |

重排模型只需本机存在即可，运行时不联网下载。缺失、显存不足或推理失败时
自动退回原来的词面重排，不影响问答；`/api/health` 的 `reranker` 字段会显示实际状态。

OCR 模型随 `rapidocr` 包一起安装，也不需要运行时下载；`/api/health` 的 `ocr`
字段会显示是否启用、是否已加载以及错误信息。

---

## 🔐 鉴权

除下列接口外，所有 `/api/` 接口都需要登录：

- `GET /api/health`
- `POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`
- `GET /api/documents/<id>/file`（自行校验 `?token=`）
- `GET /api/admin/view`（自行校验单篇短期查看授权）

被封禁的账号登录会返回 `403 {"error": "账号已被封禁，请联系管理员", "code": "banned"}`；
已经登录的会话在下一次请求时也会收到同样的 403。

两种带令牌方式，任选其一：

```
Authorization: Bearer <token>
Cookie: zd_session=<token>
```

未登录返回 `401 {"error": "请先登录", "code": "unauthorized"}`。

### 修改密码

**POST** `/api/auth/password`（需登录）

```json
{ "old_password": "旧密码", "new_password": "新密码（至少 6 位）" }
```

校验规则：原密码必须正确、新密码至少 6 位、不能与原密码相同。

成功后返回新令牌（当前设备用这个继续，不会掉线）；**所有旧令牌立即失效**——
实现方式是在用户记录里记一个 `pwd_changed_at`，令牌里带签发时间 `iat`，
校验时丢弃签发时间早于改密时间的令牌，因此不需要额外的服务端会话表。

---

## 🛡 管理员账号与管理后台

### 创建管理员

在 `backend` 目录执行（密码交互输入，不写进命令历史）：

```bash
python manage.py create-admin
```

用户名不存在时会新建管理员；**已存在则把该账号提升为管理员，密码不变**。
也可以用 `python manage.py list-users` 查看所有账号的角色和封禁状态。

登录该账号后，主页面顶部会出现「管理后台」入口，也可以直接访问
`http://127.0.0.1:5000/admin`（从文件夹直接打开页面时访问 `admin.html`）。

### 能做什么

- **用户列表**：角色、封禁状态、文献数、占用空间、最近上传时间
- **封禁 / 解封**：封禁必须填写原因；封禁后该账号立即无法登录，
  已登录的会话在下一次请求时失效。不能封禁自己，也不能封禁其他管理员
- **删除违规文献**：必须填写原因，同时清理原始文件、索引和标记
- **审计日志**：最新的在前，可按操作类型过滤

### 隐私模型

管理后台**默认只返回文献元数据**（标题、页数、块数、索引状态、大小、所属用户），
任何管理接口都不会返回正文、分块或文件路径，也不提供全量 PDF 浏览。

处理举报需要看原文时：

1. 在「文献」页点「核查查看」，填写核查事由；举报编号 / 工单号可选
2. 后端签发**只对这一篇有效**的短期令牌（默认 10 分钟，`ADMIN_VIEW_MAX_AGE` 可调）
3. 打开授权链接时才真正读取原始文件；链接每次被打开都会新增一条审计记录
4. 签发人若被停用（封禁 / 取消管理员），已签发的授权立即失效

审计日志写在 `backend/data/audit.log`（JSONL，超过 10MB 轮转为 `audit.log.1`），
记录时间、操作、操作者、对象、事由、IP、User-Agent 和附加详情。

| 操作 | 含义 |
|---|---|
| `admin.user.ban` / `admin.user.unban` | 封禁 / 解封账号 |
| `admin.document.delete` | 删除违规文献 |
| `admin.document.view_grant` | 签发单篇查看授权 |
| `admin.document.view` | 实际打开原始文件 |
| `admin.access.denied` | 非管理员访问管理接口被拒 |

---

## 📡 API 端点

| 端点 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/` | GET | — | 前端页面 |
| `/vendor/<path>` | GET | — | 前端静态资源 |
| `/api/health` | GET | — | 健康检查（模型、设备、大模型可用性） |
| `/api/auth/register` | POST | — | 注册（第一个用户继承升级前的老文献） |
| `/api/auth/login` | POST | — | 登录 |
| `/api/auth/logout` | POST | — | 退出 |
| `/api/auth/me` | GET | ✓ | 当前登录用户 |
| `/api/auth/password` | POST | ✓ | 修改密码（会作废此前所有令牌，并给当前设备换发新令牌） |
| `/api/documents` | GET | ✓ | 我的文献列表（含分类、标签、阅读进度） |
| `/api/documents/<id>` | PATCH | ✓ | 更新标题 / 分类 / 标签 / 阅读进度 |
| `/api/documents/<id>/status` | GET | ✓ | 单篇索引状态与进度 |
| `/api/documents/batch-delete` | POST | ✓ | 批量删除文献 |
| `/api/documents/<id>/blocks` | GET | ✓ | 带出处编号的正文块（txt/md/docx 的预览与跳转用） |
| `/api/upload` | POST | ✓ | 上传文件（PDF / docx / md / txt），立即返回，索引后台进行 |
| `/api/documents/<id>/summary/stream` | GET | ✓ | **流式**生成摘要（SSE） |
| `/api/documents/<id>/summary` | GET | ✓ | 生成摘要（非流式，便于脚本调用） |
| `/api/documents/<id>/ask/stream?q=` | GET | ✓ | **流式**问答（SSE） |
| `/api/documents/<id>/ask` | POST | ✓ | 问答（非流式） |
| `/api/documents/<id>/export` | POST | ✓ | 导出当前摘要 / 问答记录为 Word 或 Markdown |
| `/api/documents/<id>/file` | GET | token | 原始 PDF（供 iframe 预览，支持 `#page=N`） |
| `/admin` | GET | — | 管理后台页面（页面内自行校验管理员身份） |
| `/api/admin/stats` | GET | 管理员 | 用户 / 文献 / 存储 / 审计计数 |
| `/api/admin/users` | GET | 管理员 | 用户列表（含文献数与占用空间，不含密码） |
| `/api/admin/users/<id>/ban` | POST | 管理员 | 封禁账号（必填 `reason`） |
| `/api/admin/users/<id>/unban` | POST | 管理员 | 解封账号 |
| `/api/admin/documents` | GET | 管理员 | 全站文献元数据列表（可按 `user_id`、`q` 过滤） |
| `/api/admin/documents/<uid>/<id>` | DELETE | 管理员 | 删除违规文献（必填 `reason`） |
| `/api/admin/documents/<uid>/<id>/view-grant` | POST | 管理员 | 为单篇文献签发短期查看授权 |
| `/api/admin/view?token=` | GET | 授权令牌 | 凭短期授权读取单篇原始文件（每次访问写审计） |
| `/api/admin/audit` | GET | 管理员 | 审计日志（可按 `action` 前缀过滤） |
| `/api/library/ask/stream` | GET/POST | ✓ | **流式**全库问答（SSE，跨文献检索并标注来源） |
| `/api/library/ask` | POST | ✓ | 全库问答（非流式） |
| `/api/library/review/stream` | GET/POST | ✓ | **流式**生成多篇对比 / 综述（SSE） |
| `/api/translate/stream` | GET | ✓ | **流式**翻译（SSE） |
| `/api/translate` | POST | ✓ | 翻译（非流式） |
| `/api/documents/<id>/delete` | DELETE | ✓ | 删除文献及其索引 |

---

## 📤 上传

**POST** `/api/upload`，`multipart/form-data`，字段 `file`。

支持的格式：

| 格式 | 解析方式 | 出处单位 |
|---|---|---|
| `.pdf` | PyMuPDF 逐页提取 | **页**（真实页码） |
| `.docx` | python-docx 读段落（含标题样式、表格文字） | **段**（按段落合成的编号单元） |
| `.md` / `.markdown` | 按 `#` 标题与空行分段 | **段** |
| `.txt` | 自动识别编码（UTF-8 / UTF-16 / GB18030 含 GBK） | **段** |

> 老版 `.doc` 不支持（二进制格式），请另存为 `.docx`。
> 纯文本类没有"页"的概念，所以按约 1600 字合成编号单元，"点出处跳原文"改为滚动定位到对应段落。

```json
{
  "success": true,
  "message": "上传成功，正在后台建立语义索引",
  "document": {
    "id": "54ce5e195da86d23",
    "filename": "笔记.md",
    "chunk_count": 5,
    "page_count": 5,
    "source_type": "text",
    "unit": "段",
    "index_status": "pending",
    "ocr_pages": 0,
    "upload_time": "2026-10-11T13:12:00"
  }
}
```

上传后请轮询 `/api/documents` 或 `/api/documents/<id>/status`，直到 `index_status` 变为 `ready`。

### 扫描件 / 图片型 PDF

页面文字少于 `OCR_MIN_PAGE_CHARS` 且页面上有图片时，按扫描页处理：

| PDF 类型 | 处理方式 |
|---|---|
| 普通文字 PDF | 直接用文字层，不 OCR |
| 整本扫描件（无文字层） | 后台逐页 OCR 后建索引 |
| 混合型（部分页是扫描图） | 文字页用文字层，文字过少的页才 OCR |
| 含图片但文字层正常 | 不 OCR，避免图表里的零碎文字混入索引 |

上传接口只做一次快速文字提取就返回；需要 OCR 时由后台任务完成，
轮询状态可以看到 OCR 阶段：

```json
{
  "index_status": "running",
  "index_stage": "ocr",
  "index_done": 3,
  "index_total": 20,
  "ocr_pages": 0
}
```

`index_stage = "ocr"` 表示正在识别第 3/20 页；识别完成后切到 `"index"` 建语义索引，
最终 `index_status` 变为 `ready`，`ocr_pages` 为实际 OCR 的页数。
OCR 只识别文字，不解析图表/公式；扫描件的识别准确率低于原生文字层。

---

## 🧩 表格 / 公式 / 图注

解析阶段会尽量保留三类结构信息，写进分块文本，摘要和问答都能看到：

| 内容 | 标记 | 处理方式 |
|---|---|---|
| 表格 | `【表格】` | 还原成「列1 ｜ 列2 …」的逐行文本并单独成块 |
| 公式 | `【公式】` | 按数学符号、等号和行长度识别出的公式行 |
| 图注 / 表注 | `【图注】` / `【表注】` | 识别「图 1…」「Table 2.…」等题注并保留 |

表格识别依次尝试：完整线框（`lines` 策略）→ 只有横线的 booktabs 表格
（先用横线定位区域，再在区域内还原）→ 从「表 N」题注向下按空白列对齐还原 →
受限的整页 `text` 策略。整页正文被误判成表格时会因面积/填充率超限被丢弃；
表格块在生成摘要时优先选入，问答时也会注明「表格」出处。

限制：图片型 PDF 的表格/公式只保留 OCR 文字，不做单元格还原，也不把公式转成
LaTeX；没有题注、纯靠空格排版的内容可能识别不到。需要关闭该功能时设置
`STRUCTURED_ENABLED=0`。

---

## 🗂 文献库管理

**PATCH** `/api/documents/<id>`（需登录）

只修改元数据，不改原始文件。字段可任选其一或多个：

```json
{
  "display_name": "Transformer 综述",
  "category": "机器学习",
  "tags": ["Transformer", "注意力"],
  "progress": { "page": 4 }
}
```

- `display_name`：显示标题，留空则恢复原文件名
- `category`：单分类，最长 40 字
- `tags`：字符串（逗号/顿号/空格分隔）或数组，最多 10 个，每个最长 24 字
- `progress`：阅读进度，后端按 `page_count` 计算百分比并自动夹到合法范围

**POST** `/api/documents/batch-delete`

```json
{ "ids": ["doc_id_1", "doc_id_2"] }
```

一次最多 200 篇；返回 `{"deleted": 2, "missing": []}`。删除会同时清理原始文件、索引、标记和修改记录。

> 按标题 / 分类 / 标签的过滤在前端完成，不额外请求后端；阅读进度的百分比由后端计算。

---

## 📚 全库问答 / 对比综述

在用户整个文献库范围内检索并生成答案，对应前端「全库问答 / 对比综述」弹窗。
只使用已完成索引（`index_status = ready`）的文献。

### 全库问答

**GET** `/api/library/ask/stream?q=…&ids=id1,id2`（SSE）
**POST** `/api/library/ask`

```json
{
  "question": "我的文献库里关于小样本学习有哪些方法？",
  "ids": ["doc_a", "doc_b"],
  "top_k": 8
}
```

- `ids` 省略或为空表示整个文献库；传入则限定在这些文献内
- 每篇先各召回若干片段，再用 cross-encoder 全局重排，最后按文献限流，
  避免某一篇占满所有出处名额

流式 `done` 事件在通用字段之外还会携带：

```json
{
  "mode": "library",
  "text": "…… [[s.1]][[s.3]]",
  "citations": [1, 3],
  "invalid_citations": [],
  "sources": [
    { "n": 1, "doc_id": "doc_a", "filename": "论文 A", "page": 3,
      "unit": "页", "source_type": "pdf", "section": "", "text": "……", "score": 0.91 }
  ],
  "documents": [
    { "id": "doc_a", "title": "论文 A", "unit": "页", "category": "机器学习", "tags": [] }
  ],
  "searched": 2,
  "rewritten_query": "小样本学习的主要方法"
}
```

> 跨文献场景页码不唯一，所以出处标记用来源编号 `[[s.N]]`，对应 `sources`
> 里的第 N 条；前端渲染为「论文标题 · 第 N 页」的可点击按钮。单篇问答仍用
> `[[p.N]]`。生成后会按本次来源白名单校验，越界标记会被移除并返回在
> `invalid_citations` 中。

### 对比 / 综述

**GET** `/api/library/review/stream?topic=…&mode=review&ids=id1,id2`（SSE）
**POST** `/api/library/review/stream`

```json
{
  "topic": "小样本学习",
  "mode": "compare",
  "ids": []
}
```

- `mode`：`review`（综述，按主题归纳研究概览 / 方法路线 / 关键发现 / 异同与分歧 / 局限与展望 / 结论）或 `compare`（逐篇对比维度、共同点、差异与选型建议）
- `topic` 可留空：此时直接取每篇文献的摘要性片段，适合「把我库里的文献整体综述一遍」
- 选材按文献分配名额（最多 12 篇、24 条材料），保证覆盖而不是让高分文献独占
- `done` 除 `text` / `citations` / `sources` / `documents` 外还带 `mode`（`review` / `compare`）

---

## 🌊 流式接口（SSE）

事件格式为 `data: {json}\n\n`，共四种：

| type | 含义 |
|---|---|
| `status` | 进度提示，如"正在检索相关片段…" |
| `delta` | 正文增量，`{"type":"delta","text":"…"}` |
| `done` | 结束，携带 `text`（校验后的完整正文）、`citations: [5,6,7]`、`invalid_citations`、`passages: [{page, section, text, score}]`、`model` |
| `error` | 出错，`{"type":"error","error":"原因"}` |

正文中的 `[[p.3]]` 是出处标记，前端渲染为可点击的跳页按钮；全库问答的
`[[s.N]]` 表示第 N 条来源，渲染为「文献标题 · 页码」按钮。

**引用校验**：生成前会把本次检索片段涉及的编号白名单写进提示词；生成后再次检查，
删掉不在白名单里的 `[[p.N]]` 标记，并在 `done.invalid_citations` 里返回被删编号。
前端用 `done.text` 重渲染最终答案，并提示"已移除 N 处无法在原文核实的出处"。

**查询改写**：每次提问都会先调用一次低 token 的改写请求，把口语化问题和追问
（如「还有呢」「它的缺点」）补全成可独立检索的问题；改写结果放在
`done.rewritten_query`（与原问题相同则为 `null`）。改写失败时自动退回原问题。

---

## 🔎 按页码 / 段号提问

上传时**每一页都会解析并建入索引**；但提问时不会把整本 PDF 交给大模型，
而是按需取出相关片段（受上下文长度和调用成本限制）。

问题里明确写出页码 / 段号时，后端会跳过查询改写和语义检索，
直接读取该页（该段）的全部片段作为上下文：

| 写法 | 效果 |
|---|---|
| `第244页讲的什么` / `244页` / `page 244` / `p.244` | 直接读取第 244 页 |
| `第244-246页` / `pages 244-246` | 读取范围内的每一页 |
| `第12段`（txt/md/docx） | 直接读取第 12 段 |

页码超过文献总页数时会返回「这篇文献共 N 页，没有第 X 页」；
空白页或纯图片页会提示该页没有可识别文字。没有写页码的问题仍走
混合检索 + 神经重排。

---

## 💬 非流式问答响应

```json
{
  "document_id": "54ce5e195da86d23",
  "question": "本文用了什么方法？",
  "answer": {
    "text": "所提方法为 RecLMIS，无需复杂的多层级交互 [[p.5]]。",
    "citations": [5],
    "invalid_citations": [],
    "source": "openai",
    "model": "deepseek-chat",
    "passages": [{"page": 5, "section": "Methods", "text": "…", "score": 0.83}]
  }
}
```

---

## 📄 导出摘要 / 问答记录

**POST** `/api/documents/<id>/export`

把前端当前的摘要或问答记录转成可编辑、可打印的文件，**不调用大模型**。

```json
{
  "kind": "summary",
  "format": "docx",
  "summary": { "text": "## 研究背景\n…… [[p.2]]", "model": "deepseek-chat" }
}
```

```json
{
  "kind": "qa",
  "format": "md",
  "turns": [
    { "q": "本文用了什么方法？", "a": "…… [[p.5]]", "citations": [5],
      "passages": [{ "page": 5, "section": "Methods", "text": "……" }] }
  ]
}
```

- `kind`：`summary` 或 `qa`；`format`：`docx` 或 `md`
- 正文中的 `[[p.N]]` 会转成「（第 N 页）」（txt/docx/md 文献为「第 N 段」）
- Word 版自动加标题、导出时间、参考出处和页码页脚

---

## ⚠️ 错误响应

```json
{ "error": "错误消息" }
```

| 状态码 | 含义 |
|---|---|
| 400 | 请求参数错误 / PDF 无法解析（如扫描件） |
| 401 | 未登录或令牌失效 |
| 403 | 账号已封禁、需要管理员权限，或查看授权失效；访问他人文档统一返回 404，避免暴露文档是否存在 |
| 404 | 资源未找到 |
| 503 | 语义模型或大模型不可用 |

---

## 📁 数据存储

```
backend/data/
  users.json          # 账号（密码为 bcrypt 哈希；含 role / banned 字段）
  secret.key          # 会话签名密钥（自动生成，勿泄露）
  audit.log           # 管理操作审计日志（JSONL，超 10MB 轮转为 audit.log.1）
backend/uploads/
  <用户ID>/
    <文档ID>_info.json     # 元数据 + 带页码的文本块
    <文档ID>_index.faiss   # 向量索引
    <文档ID>_<原文件名>.pdf # 原始 PDF
```
