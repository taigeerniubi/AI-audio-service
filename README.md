# AI audio service

AI audio service 是一个根据产品资料回答客户问题的实时语音销售顾问。应用在本机运行，使用阿里云百炼处理实时语音对话和文本向量化。

## 主要功能

- 在浏览器里用麦克风对话，支持实时字幕、插话打断和静音。
- 为不同产品配置知识库、开场白和沟通目标。
- 上传资料后试跑检索，核对命中的原文片段和来源。
- 查看会话转录、业务结果、响应耗时和模型用量，并导出 JSON 报告。

## 快速开始

需要 Python 3.11+、Node.js 20.19+ 或 22.12+、npm 和 uv。以下以 Windows PowerShell 为例，命令在仓库根目录执行。

### 安装依赖

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
npm --prefix frontend ci
```

### 配置 API Key

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

将具备实时语音和文本向量模型访问权限的百炼 API Key 填入 `.env` 的 `DASHSCOPE_API_KEY`。

### 启动应用

```powershell
.\.venv\Scripts\python.exe -m scripts.setup_rag
.\.venv\Scripts\python.exe start.py
```

首次运行 `scripts.setup_rag` 会下载文档切片所需的分词器。启动后打开 [语音工作台](http://127.0.0.1:5173)，并允许浏览器使用麦克风。

## 第一次语音会话

1. 在「产品与知识」创建产品并上传资料。
2. 在「检索试跑」输入一个客户可能会问的问题，检查知识库是否找到了对应资料。
3. 在「语音演示」选择产品和音色，开始对话。
4. 结束对话后打开「会话报告」，查看并导出本次结果。

支持 TXT、Markdown、文字版 PDF、DOCX 和 XLSX。每个产品最多上传 20 份资料，单份上限为 10 MB，PDF 最多 50 页。上传 Excel 前请将公式转换为值。

## 常用配置

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DASHSCOPE_WORKSPACE_ID` | 空 | 使用专属工作空间时填写。 |
| `QWEN_REGION` | `cn-beijing` | 模型服务地域。 |
| `QWEN_MODEL` | `qwen-audio-3.1-realtime-plus` | 实时语音模型。 |
| `QWEN_VOICE` | `longanhuan_v3.1` | 默认音色。 |
| `DATABASE_PATH` | `data/voice.db` | SQLite 数据库路径，相对于仓库根目录。 |

修改 `.env` 后重启服务。 [配置模板](.env.example)

## 架构

- React 和 AudioWorklet 负责麦克风采集、播放及字幕显示，通过 WebSocket 与 FastAPI 交换语音帧和会话事件。
- FastAPI 管理每次会话的产品资料快照、对话状态和业务工具调用，通过阿里云百炼的实时 WebSocket 接口收发语音。
- 知识库使用 Docling 切分文档，结合 BM25 和千问文本向量检索，通过 RRF 融合结果并附上来源。
- SQLite 保存产品配置、资料索引、会话转录与报告。

## 开发验证

运行后端与前端测试，并构建前端。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

构建后可由 FastAPI 同时提供页面和 API。

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

服务启动后可在本机打开 [语音工作台](http://127.0.0.1:8000) 或 [API 文档](http://127.0.0.1:8000/docs)。

## 开源许可

项目源码和自有文档采用 [MIT 许可证](LICENSE)。

随项目分发的思源黑体采用 [SIL Open Font License 1.1](frontend/public/fonts/OFL.txt)。`samples/` 中引用的第三方资料适用其各自的授权条款。
