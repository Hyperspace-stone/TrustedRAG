# Plan: TrustedRAG GitHub 可发布项目归档

## 1. Source-of-Truth Collection

### Production backend and core

从服务器生产项目根目录只读导出以下内容：

- `raganything/`
- `webapp/`
- 根目录 `pyproject.toml`、`setup.py`、`requirements.txt`
- `examples/construct_graph_xzl.py`
- `examples/retrieval_xzl.py`
- 当前有效的必要上游文档和许可证文件

导出时显式排除：

- `.git/`
- `.venv/`
- `data/`
- `__pycache__/`
- `.pytest_cache/`
- `*.egg-info/`
- 日志、临时文件和归档

### Frontend

以前端源码目录 `work/frontend-build/frontend` 为基线，保留：

- `src/`
- `public/`
- `index.html`
- `package.json`
- `package-lock.json`
- TypeScript、Vite、ESLint 配置

排除：

- `node_modules/`
- `dist/`
- 本地缓存
- 重复的 `pnpm-lock.yaml`，仓库统一使用 npm 与 `package-lock.json`

### Deployment configuration

以生产 systemd 服务和 Nginx 配置为参考，转换为不包含服务器 IP、用户名和绝对私密路径的模板。

## 2. Clean Repository Assembly

在本地工作区创建：

```text
TrustedRAG-GitHub/
```

先创建空目录结构，再复制经过允许清单筛选的源码。所有复制动作均从源目录进入新目录，不直接修改生产项目或现有工作目录。

创建完成后进行第一次清单核对：

- 后端模块完整
- 前端源码完整
- 测试完整
- 数据目录只有说明文件和 `.gitkeep`
- 不存在构建产物、缓存和数据库

## 3. Dependency Normalization

### Python

分为两份清单：

- `requirements.txt`：TrustedRAG 实际运行和文档解析所需依赖。
- `requirements-dev.txt`：pytest 等开发测试依赖。

不直接复制服务器完整 `pip freeze`，因为其中包含大量间接依赖、平台相关包和 Git editable 安装路径。依赖版本从以下信息交叉确认：

1. 生产 `pip freeze`
2. 上游 `pyproject.toml` / `requirements.txt`
3. `webapp` 实际 import
4. 生产测试结果

对于 MinerU、Torch 等重型可选依赖，在文档中分为“基础安装”和“完整文档解析安装”，避免所有用户默认下载大型模型依赖。

### Node.js

保留现有 `package.json` 与 `package-lock.json`，统一命令为 `npm ci`。执行 `npm run build` 验证锁文件可用。

## 4. Configuration and Secret Isolation

创建完整 `.env.example`，并将配置划分为：

- 路径
- 生成模型
- Embedding 模型
- 检索参数
- 建库队列
- Web/API

检查 Python 代码中所有 `os.getenv` / `_env` 调用，确保每个实际配置项均出现在 `.env.example` 或配置文档中。

创建 `.gitignore`，至少排除：

```text
.env
.venv/
node_modules/
dist/
__pycache__/
.pytest_cache/
*.sqlite3
*.db
*.log
data/corpus/*
data/output/*
data/rag_storage/*
data/.trustedrag-builds/*
data/webapp_model_settings.json
```

## 5. Documentation

### Root README

README 覆盖：

1. 项目定位与功能
2. 架构概览
3. 系统要求
4. 快速安装
5. 模型与 Embedding 配置
6. 初始化数据目录
7. 启动后端和前端
8. 运行测试
9. Linux 生产部署
10. 常见错误排查
11. 安全注意事项
12. 第三方来源说明

### Detailed docs

- `docs/architecture.md`：请求、路由、检索、生成、来源展示和建库队列流程。
- `docs/configuration.md`：所有环境变量及默认值。
- `docs/knowledge-base-build.md`：单文件/多文件建库、进度和失败恢复。
- `docs/deployment.md`：systemd、Nginx、目录权限和升级方法。
- `docs/security.md`：密钥隔离、登录、用户 API 配置隔离和知识库访问控制。

## 6. Deployment Templates and Scripts

创建：

- `deploy/systemd/trustedrag-api.service`
- `deploy/nginx/trustedrag.conf`
- `deploy/install.sh`
- `scripts/init-data-layout.sh`
- `scripts/check-environment.sh`
- `scripts/run-dev.sh`

脚本必须：

- 使用可配置项目根目录，不硬编码生产服务器上的绝对路径
- 不写入真实密钥
- 在修改系统服务前明确输出目标路径
- 对关键命令使用 `set -euo pipefail`
- 不自动覆盖现有生产数据目录

## 7. Security and Size Audit

执行多层扫描：

1. 文件名扫描：`.env`、数据库、日志、归档、密钥文件。
2. 内容扫描：`sk-`、密码、Bearer Token、SSH 私钥头、服务器密码和已知 API Key 片段。
3. 大文件扫描：列出超过 5 MB 的文件并人工确认。
4. 路径扫描：查找生产绝对路径、开发者本机目录和生产服务器地址。
5. Git 忽略验证：确认数据目录中的真实文件会被忽略。

任何命中必须在生成归档前清理或替换为占位符。

## 8. Verification

### Static verification

- Python `compileall`
- 环境变量清单与代码读取项对照
- JSON、YAML、service 和 Nginx 模板基本语法检查

### Backend verification

优先使用服务器已有 Python 环境，在临时目录对归档源码运行：

```bash
python -m pytest webapp/tests -q
python -m compileall -q webapp raganything
```

测试只使用临时数据，不触碰生产知识库。

### Frontend verification

在本地归档目录执行：

```bash
cd frontend
npm ci
npm run build
```

### Package verification

- 从最终目录生成 ZIP。
- 解压到一个新的临时目录。
- 再次检查关键文件存在。
- 计算 SHA-256。
- 输出文件数量、源码大小、压缩包大小和扫描结论。

## 9. Implementation Order

1. 从生产服务器创建只读源码快照并下载到临时目录。
2. 建立 `TrustedRAG-GitHub/` 骨架。
3. 合并生产后端、RAG-Anything 核心和本地最新前端。
4. 清理缓存、产物、历史文件和绝对路径。
5. 生成依赖清单、环境模板和 `.gitignore`。
6. 编写 README、详细文档和部署模板。
7. 执行安全扫描与大文件检查。
8. 运行后端、前端和配置验证。
9. 修复验证问题并重新验证。
10. 生成 ZIP、SHA-256 和最终清单。

## 10. Risks and Mitigations

### Risk: production and local code drift

Mitigation: 后端以服务器文件为准；前端以最后一次成功构建并部署的本地源码为准；对关键文件计算哈希并记录来源。

### Risk: accidental secret inclusion

Mitigation: 使用允许清单复制、禁止复制 `data/`，并进行文件名、内容和已知密钥片段三层扫描。

### Risk: oversized repository

Mitigation: 不包含知识库、模型、构建产物和虚拟环境；可选重型依赖只写入文档或单独 extras 清单。

### Risk: upstream license conflict

Mitigation: 保留上游 LICENSE 和版权说明；不擅自重新授权；默认建议私有仓库。

### Risk: dependency list too broad or too narrow

Mitigation: 使用 import 扫描、生产依赖、上游清单和测试四方校验；基础依赖与完整解析依赖分离。

### Risk: verification touches production data

Mitigation: 测试在临时目录和临时数据配置下运行；不启动第二个生产 worker，不调用删除接口，不重试现有建库任务。

## 11. Verification Checkpoints

1. **Snapshot checkpoint**：生产快照不含 `data/`、`.venv/`、`.git/`。
2. **Assembly checkpoint**：目录结构和文件允许清单一致。
3. **Security checkpoint**：密钥与大文件扫描零违规。
4. **Build checkpoint**：后端测试和前端构建通过。
5. **Archive checkpoint**：新目录解压验证成功，SHA-256 已生成。

## 12. Parallelism

源码快照与本地前端清单检查可以并行；文档和部署模板依赖最终配置清单，应在代码合并后进行；安全扫描、后端测试和前端构建可在目录组装完成后并行执行；最终归档必须等待所有验证通过。
