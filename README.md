# TrustedRAG

TrustedRAG 是面向可信文档问答场景的前后端一体 RAG 系统。它在 RAG-Anything 与 LightRAG 的基础上增加了文档标题优先路由、反向证据检索、引用对齐、PDF 原文侧栏、登录与知识库权限、多用户模型配置隔离，以及严格串行的多文件建库队列。

> 本仓库只包含源码、测试、配置模板和部署资料，不包含任何真实知识库、用户数据库或 API Key。

## 主要能力

- 自动区分普通对话与需要检索的问题。
- 优先按文档标题定位，并支持从数值或证据反向定位文档。
- LightRAG 图谱与向量混合检索。
- 回答下方显示去重后的引用，点击后弹出 PDF/原文证据。
- 登录后查看受保护知识库与文档。
- 每个用户独立保存 OpenAI-compatible 生成模型配置。
- 多文件上传、单 worker 严格串行构图、独立子进程、进度和失败重试。
- 支持 PDF、DOCX、XLS/XLSX、PPTX、TXT 和 Markdown。

## 目录

```text
raganything/   RAG-Anything 核心源码
webapp/        FastAPI 后端、检索与建库逻辑
frontend/      React + TypeScript + Vite 前端
examples/      构图与检索示例
data/          空的运行数据目录模板
deploy/        Nginx、systemd 和安装脚本
scripts/       环境检查、初始化与开发启动工具
docs/          架构、配置、建库、部署和安全文档
```

## 系统要求

- Linux/macOS/Windows（生产部署推荐 Ubuntu 22.04+）
- Python 3.11 或 3.12
- Node.js 20+
- npm 10+
- OpenAI-compatible 生成模型接口
- OpenAI-compatible Embedding 接口
- 可选：LibreOffice，用于 Office 文档转换
- 可选：MinerU 完整依赖，用于复杂 PDF/OCR 解析

## 快速开始

### 1. 创建 Python 环境

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如需 MinerU/OCR 完整环境：

```bash
python -m pip install -r requirements-full.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

至少填写：

- `TRUSTEDRAG_ADMIN_PASSWORD`
- `RAG_LLM_BASE_URL`
- `RAG_LLM_API_KEY`
- `RAG_LLM_MODEL`
- `RAG_EMBEDDING_BASE_URL`
- `RAG_EMBEDDING_API_KEY`
- `RAG_EMBEDDING_MODEL`
- `RAG_EMBEDDING_DIM`

生成模型和 Embedding 模型是两套独立配置。Embedding 维度必须与模型实际输出一致。

### 3. 初始化运行目录

```bash
bash scripts/init-data-layout.sh
python scripts/validate-config.py
```

### 4. 启动后端

```bash
bash scripts/run-dev.sh
```

后端默认监听 `127.0.0.1:5051`。

### 5. 启动前端

```bash
cd frontend
npm ci
npm run dev
```

Vite 会将 `/api` 代理到 `127.0.0.1:5051`。

## Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python scripts\validate-config.py
python -m uvicorn webapp.app:app --host 127.0.0.1 --port 5051 --reload
```

前端在另一个终端运行：

```powershell
Set-Location frontend
npm ci
npm run dev
```

## 测试

```bash
source .venv/bin/activate
python -m compileall -q raganything webapp
python -m pytest webapp/tests -q

cd frontend
npm ci
npm run build
```

部分原始文档查看测试需要准备兼容的样例知识库。详见 `webapp/tests` 和 `docs/knowledge-base-build.md`。

## 生产部署

准备 `.env` 后运行：

```bash
sudo TRUSTEDRAG_PROJECT_ROOT=/opt/trustedrag bash deploy/install.sh
```

安装脚本会创建 Python 环境、构建前端、安装 systemd 服务与 Nginx 配置。部署前请阅读 [部署文档](docs/deployment.md)。

## 数据迁移

真实知识库不在 Git 仓库中。迁移现有系统时需单独复制：

```text
data/corpus/
data/output/
data/rag_storage/
data/name2id.json
```

用户数据库和模型配置包含敏感信息，应通过安全通道迁移，不能提交 Git。

## 常见问题

- `INDEX_INCOMPLETE`：通常表示 LightRAG 没有生成完整索引；先检查 `kv_store_doc_status.json` 中的 `error_msg`。模型接口返回 402 时表示账户余额不足。
- Embedding 维度错误：修改 `RAG_EMBEDDING_DIM` 使其与模型实际输出一致。
- PDF 只有扫描图片：安装并配置 MinerU/OCR，或先进行 OCR。
- 无法首次登录：确认首次启动前已设置 `TRUSTEDRAG_ADMIN_PASSWORD`。

## 安全

不要提交 `.env`、SQLite 数据库、语料、索引、模型设置或日志。完整要求见 [安全文档](docs/security.md)。

## 来源与授权

项目包含基于 RAG-Anything、LightRAG 与 UltraRAG 前端思想的修改。请阅读 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与现有 [LICENSE](LICENSE)。在确认业务数据和自定义代码的授权边界前，建议将仓库设为私有。
