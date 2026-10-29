# LLM Data Port

LLM Data Port 是一个本地运行的问卷 PDF 处理工具：上传扫描件，提取结构化 JSON，并按指定 Excel 模板汇总。网页默认只监听本机 `127.0.0.1`。

## 功能

- 批量上传多个 PDF，后台按队列逐份解析并显示进度。
- 将每份问卷保存为 JSON，并在批次完成后统一更新 Excel。
- 使用 OpenCV 可选遮盖第一页指定位置的姓名和假名；旁边的 ID 与其他页面保持不变。
- 在网页中浏览解析记录、按 ID 或年度查找，并下载指定格式的 Excel。
- 无法对应到 Excel 字段的答案保存在“未対応データ”工作表中，便于人工核对。

Excel 转换由本地 Python 代码完成，不会把 JSON 交给大模型转换。项目目前没有接入 Baserow 或其他数据库。

## 环境

- Python 3.12
- Poppler（`pdftoppm`、`pdftotext`、`pdfinfo`）
- `requirements.txt` 中的 Python 依赖
- 使用云端模型时，需要兼容 OpenAI Chat Completions 接口的服务及 API Key；也可配置本地 Ollama

### macOS 安装

在项目目录运行：

```sh
brew install python@3.12 poppler
python3.12 -m venv .venv312
.venv312/bin/python -m pip install --upgrade pip
.venv312/bin/python -m pip install -r requirements.txt
```

### Windows 安装

在项目目录的 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv312
.\.venv312\Scripts\python.exe -m pip install --upgrade pip
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
```

Windows 还需要安装 Poppler。完整的 Windows/macOS 安装、启动和停止说明见 [WEB_STARTUP.md](WEB_STARTUP.md)。

## 配置模型 API

仓库不包含可用的 API Key。首次使用时，将本地配置示例复制为私有配置文件，然后只在本机填写密钥：

macOS：

```sh
cp src/base_llm/provider_config_local.example.py src/base_llm/provider_config_local.py
```

Windows PowerShell：

```powershell
Copy-Item src/base_llm/provider_config_local.example.py src/base_llm/provider_config_local.py
```

`provider_config_local.py` 和 `.env` 已加入 `.gitignore`。也可以通过环境变量设置 `BASE_LLM_PROVIDER`、`BASE_LLM_API_URL`、`BASE_LLM_API_KEY` 和 `BASE_LLM_API_MODEL`。不要把真实密钥放进网页代码、README 或提交记录；如果密钥曾进入公开仓库，应在模型服务控制台撤销并重新生成。

## 启动

macOS：

```sh
.venv312/bin/python scripts/serve_frontend_demo.py --host 127.0.0.1 --port 8765
```

Windows PowerShell：

```powershell
.\.venv312\Scripts\python.exe .\scripts\serve_frontend_demo.py --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765/>。服务运行期间保持终端开启；按 `Ctrl+C` 停止。

网页中的“阅览者/管理员”只是界面切换，不提供账号认证。不要把本地演示服务直接开放到公网。

## Excel 汇总方式

程序以 [Excel 模板](templates/excel_output_template.xlsx) 为格式来源，读取已保存的 JSON，并从第 4 行开始逐条写入。字段按模板代码、问题 ID 和配置的日文名称别名匹配；无法匹配的答案会进入“未対応データ”工作表。导出的工作簿保存在本机配置的输出目录中，管理者页面可下载当前全部记录。

批量处理时，PDF 逐份排队解析，待本批任务结束后再统一重建 Excel。解析结果 JSON 是导出的数据来源；修改 JSON 后重新导出即可刷新工作簿。

## 数据与隐私

PDF 和解析结果保存在本机输入目录及 `outputs/`。这些数据目录、虚拟环境、`.env` 和本地 API 配置不会提交到 Git。选择云端模型时，页面图像会发送给所配置的模型服务；只处理你有权发送的数据。

姓名遮盖只覆盖第一页指定姓名栏，不代表整份 PDF 已去标识化。未勾选时不会执行姓名遮盖。启用后仍建议抽查输出 PDF；如需处理其他个人信息，应另行确认和设计规则。

更多项目说明见 [数据库导出说明](DATABASE_EXPORT.md)、[进度估算说明](PROGRESS_ESTIMATE.md) 和 [启动指南](WEB_STARTUP.md)。
