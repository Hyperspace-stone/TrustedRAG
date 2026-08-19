# Spec: TrustedRAG GitHub 可发布项目归档

## Objective

将服务器当前运行的 TrustedRAG 整理为一个独立、干净、可复现、可直接上传 GitHub 的项目目录，并生成对应压缩包。

目标用户是需要继续开发、部署或交付 TrustedRAG 的工程人员。完成后，新环境应能仅依赖仓库源码、公开依赖和自行填写的环境变量完成安装与启动，不依赖当前服务器的绝对路径或任何私密文件。

## Assumptions

1. 以服务器当前生产版本为最终基线，本地历史工作目录仅用于补齐前端源码和部署资料。
2. 仓库包含 RAG-Anything 核心代码、TrustedRAG 自定义后端、React 前端、必要示例、测试和部署模板。
3. 不包含真实知识库、用户数据、会话、API Key、SSH 密码、日志、缓存、构建产物或历史备份。
4. 生成本地 GitHub-ready 文件夹和 `.zip`/`.tar.gz` 归档，但不自动创建远程 GitHub 仓库或执行 `git push`。
5. 暂不替用户决定开源许可证；保留现有上游许可证，并在 README 中说明第三方来源与待确认的项目授权方式。

## Tech Stack

- Python 3.12
- FastAPI 0.141.x + Uvicorn 0.52.x
- LightRAG `lightrag-hku==1.4.16`
- RAG-Anything（当前服务器检出版本）
- OpenAI-compatible generation and embedding APIs
- React + TypeScript + Vite
- Node.js 20+，npm 10+
- SQLite（用户、会话配置和知识库权限）
- Nginx + systemd（Linux 生产部署）

## Commands

预期仓库提供以下完整命令：

```bash
# 后端安装
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 后端开发
uvicorn webapp.app:app --host 127.0.0.1 --port 5051 --reload

# 后端测试
python -m pytest webapp/tests -q

# 前端安装、开发、检查和构建
cd frontend
npm ci
npm run dev
npm run build

# Linux 环境检查
bash scripts/check-environment.sh

# 生产安装示例
sudo bash deploy/install.sh
```

## Project Structure

```text
TrustedRAG/
├── README.md                    # 中文主文档：安装、配置、运行、部署
├── README_EN.md                 # 简要英文说明
├── LICENSE                      # 保留兼容的上游许可证/授权说明
├── THIRD_PARTY_NOTICES.md       # RAG-Anything、UltraRAG 等来源说明
├── .gitignore                   # 排除数据、密钥、缓存、构建产物
├── .env.example                 # 无密钥的完整环境变量模板
├── pyproject.toml               # Python 项目信息与工具配置
├── requirements.txt             # 最小运行依赖，使用明确版本范围
├── requirements-dev.txt         # pytest 等开发依赖
├── raganything/                 # 必要的 RAG-Anything 核心源码
├── webapp/                      # TrustedRAG FastAPI 后端及测试
├── frontend/                    # React/TypeScript/Vite 前端源码
├── examples/
│   ├── retrieval_xzl.py         # 检索示例
│   └── construct_graph_xzl.py   # 构图示例
├── data/
│   ├── README.md                # 数据目录约定
│   ├── name2id.example.json     # 映射格式样例
│   ├── corpus/.gitkeep
│   ├── output/.gitkeep
│   └── rag_storage/.gitkeep
├── deploy/
│   ├── nginx/trustedrag.conf
│   ├── systemd/trustedrag-api.service
│   └── install.sh
├── scripts/
│   ├── check-environment.sh
│   ├── init-data-layout.sh
│   └── run-dev.sh
├── docs/
│   ├── architecture.md
│   ├── configuration.md
│   ├── knowledge-base-build.md
│   ├── deployment.md
│   └── security.md
└── specs/
    └── github-project-packaging.md
```

## Environment Configuration

`.env.example` 至少覆盖以下分组，所有值均为占位符或安全默认值：

```dotenv
# Paths
TRUSTEDRAG_PROJECT_ROOT=/opt/trustedrag
TRUSTEDRAG_AUTH_DB_PATH=/opt/trustedrag/data/trustedrag_auth.sqlite3

# Generation model (OpenAI-compatible)
RAG_LLM_BASE_URL=https://api.example.com/v1
RAG_LLM_API_KEY=replace-me
RAG_LLM_MODEL=replace-me

# Embedding model (OpenAI-compatible)
RAG_EMBEDDING_BASE_URL=https://api.example.com/v1
RAG_EMBEDDING_API_KEY=replace-me
RAG_EMBEDDING_MODEL=text-embedding-3-large
RAG_EMBEDDING_DIM=3072

# Retrieval
RAG_QUERY_MODE=hybrid
RAG_TOP_K=40
RAG_CHUNK_TOP_K=20
RAG_MAX_SOURCES=8

# Knowledge-base build queue
TRUSTEDRAG_KB_BUILD_ENABLED=true
TRUSTEDRAG_BUILD_BATCH_LIMIT=20
TRUSTEDRAG_BUILD_MAX_UPLOAD_BYTES=104857600
TRUSTEDRAG_BUILD_TIMEOUT_SECONDS=2700
TRUSTEDRAG_BUILD_EMBEDDING_MAX_ASYNC=2
TRUSTEDRAG_BUILD_EMBEDDING_TIMEOUT_SECONDS=120

# Web/API
TRUSTEDRAG_API_HOST=127.0.0.1
TRUSTEDRAG_API_PORT=5051
```

真实生产值不得复制到仓库。

## Code Style

Python 使用类型标注、`pathlib.Path`、明确异常类型和边界校验：

```python
def normalize_collection_id(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    if not value.isdigit() or len(value) > 3:
        raise ValueError("invalid knowledge-base identifier")
    return value.zfill(3)
```

TypeScript 使用显式类型、`async/await` 和集中 API 层：

```ts
export async function fetchKnowledgeBases(): Promise<KbFilesResponse> {
  return httpRequest<KbFilesResponse>("/api/kb/files");
}
```

命名约定：Python 使用 `snake_case`，TypeScript 变量与函数使用 `camelCase`，React 组件使用 `PascalCase`。

## Testing Strategy

- 后端：保留并运行 `webapp/tests` 全部 pytest 测试。
- 前端：必须通过 TypeScript 检查和 Vite 生产构建。
- 配置：脚本验证 `.env.example` 包含代码实际读取的所有环境变量。
- 安全：递归扫描仓库，确认没有真实 API Key、密码、SQLite 数据库、`.env`、日志和语料文件。
- 体积：归档不包含 `node_modules`、`.venv`、`dist`、模型或知识库数据；目标压缩包控制在合理源码体积内。
- 可复现性：在空目录执行依赖安装、后端导入检查和前端构建。

## Boundaries

### Always

- 以生产后端和最新前端源码为基线。
- 保留必要版权和第三方来源说明。
- 使用 `.env.example` 替代真实配置。
- 在归档前执行密钥与大文件扫描。
- 保留测试并记录实际验证结果。

### Ask First

- 创建或推送远程 GitHub 仓库。
- 选择新的开源许可证。
- 把任何真实知识库样本或业务文档加入仓库。
- 修改生产服务器上的运行代码或配置。

### Never

- 提交 API Key、SSH 密码、Cookie 或账号密码。
- 提交 `trustedrag_auth.sqlite3`、模型设置 JSON 或会话数据。
- 提交 `data/corpus`、`data/output`、`data/rag_storage` 的真实内容。
- 提交 `.venv`、`node_modules`、`dist`、`__pycache__`、日志和历史备份。
- 删除或改动服务器现有生产项目来完成归档。

## Success Criteria

1. 生成独立目录 `TrustedRAG-GitHub/`，不依赖当前杂乱的 `work/` 历史目录。
2. 目录包含后端、前端、必要核心代码、测试、示例、部署模板和完整文档。
3. `.env.example` 与配置文档覆盖所有运行必需字段，且不含真实密钥。
4. `requirements.txt`、`requirements-dev.txt`、`package-lock.json` 可用于重建环境。
5. 后端测试通过，前端 `npm run build` 通过。
6. 密钥扫描和大文件扫描无违规结果。
7. 生成 `TrustedRAG-GitHub.zip` 和 SHA-256 校验文件。
8. README 给出从克隆仓库到启动服务的完整命令。
9. 不修改、不停止服务器上的生产服务，不复制真实知识库数据。

## Open Questions

1. 最终仓库许可证采用现有 RAG-Anything 的许可证，还是作为内部私有仓库不新增许可证？
2. 是否需要在后续阶段由我创建 GitHub 仓库并推送代码？本次默认不推送。
