# LLM 项目网页端启动指南

网页入口位于 frontend-html/，服务启动脚本为 scripts/serve_frontend_demo.py。Windows 和 macOS 都通过本机的 127.0.0.1:8765 访问。

## 首次运行前

- 项目使用 Python 3.12 和项目内的 .venv312 环境。不要用系统 Python 直接启动。
- 使用 MiniMax 时，确认 src/base_llm/provider_config_local.py 中已填写 OPENAI_COMPATIBLE_API_KEY。若文件不存在，可从 src/base_llm/provider_config_local.example.py 复制一份，再填写标准 API Key：
- API Key 仅填写在本机的私有配置文件或环境变量中，不要写进启动文档、网页前端或 Git 提交。

- Token Plan 订阅密钥不能代替标准 API Key。
- API Key 只保存在服务端本地配置中，不要写进网页前端。该本地配置文件已被 Git 忽略。
- 安装 `requirements.txt` 后会同时安装 Excel 导出依赖。解析完成后，项目会自动更新 本机配置的输出目录；也可以在管理者页面下载全部解析 JSON 的模板格式 Excel。未匹配到模板列的答案会放入 `未対応データ` 工作表。
- 网页中的“閲覧者/管理者”选项只切换界面，不提供账号认证；服务默认绑定 127.0.0.1，请不要将其作为带有登录保护的公网服务使用。
- 上传解析时可选择是否进行姓名打码，并会再次确认。只有选择打码时，程序才会在本机用 OpenCV 处理第一页的姓名及假名读音，保留 ID；其他页面不会打码。若选择打码但无法可靠定位姓名栏，任务会停止，不会把未打码的第一页发送给模型。原始 PDF 仍保留在本机。此功能不覆盖其他个人信息；在多份脱敏扫描件验证完成前，不要上传含真实个人信息的问卷。

## Windows 启动（PowerShell）

### 首次安装

打开 PowerShell，运行：

~~~powershell
Set-Location "$HOME\Desktop\LLMdataport"

py -3.12 --version
if (-not (Test-Path ".\.venv312\Scripts\python.exe")) {
    py -3.12 -m venv .venv312
}
.\.venv312\Scripts\python.exe -m pip install --upgrade pip
.\.venv312\Scripts\python.exe -m pip install -r requirements.txt
~~~

如果电脑尚未安装 Python 3.12，先安装 Python 3.12，再重新运行以上命令。PDF 处理还需要 Poppler。程序会自动检查常见的 WinGet 安装目录；如果 Poppler 安装在其他位置，请在启动前设置其 bin 目录：

~~~powershell
$env:POPPLER_BIN = "C:\path\to\poppler\Library\bin"
~~~

### 每次启动

~~~powershell
Set-Location "$HOME\Desktop\LLMdataport"
.\.venv312\Scripts\python.exe .\scripts\serve_frontend_demo.py --host 127.0.0.1 --port 8765
~~~

看到 “Serving LLM demo frontend at http://127.0.0.1:8765/” 后，在浏览器打开 http://127.0.0.1:8765/。保持这个 PowerShell 窗口开启；按 Ctrl+C 停止服务。

### 可选：后台启动

~~~powershell
Set-Location "$HOME\Desktop\LLMdataport"
$python = ".\.venv312\Scripts\python.exe"
$out = ".\frontend_demo_server.out.log"
$err = ".\frontend_demo_server.err.log"
Start-Process -FilePath $python -ArgumentList @("scripts\serve_frontend_demo.py","--host","127.0.0.1","--port","8765") -WorkingDirectory (Get-Location) -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
~~~

后台运行时仍通过 http://127.0.0.1:8765/ 访问。查看服务状态：

~~~powershell
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
~~~

停止后台服务时，先查看 OwningProcess 对应的 PID。例如 PID 为 12345 时，运行 Stop-Process -Id 12345（将数字替换为实际 PID）。日志保存在项目根目录的 frontend_demo_server.out.log 和 frontend_demo_server.err.log。

## macOS 启动（终端）

### 首次安装

打开“终端”，运行：

~~~bash
cd ~/Desktop/LLMdataport
brew install python@3.12 poppler

if [ ! -x ".venv312/bin/python" ]; then
  python3.12 -m venv .venv312
fi
.venv312/bin/python -m pip install --upgrade pip
.venv312/bin/python -m pip install -r requirements.txt
~~~

以上安装步骤只需首次运行时执行。之后如果更新了项目依赖，再运行 pip 安装命令即可。

### 每次启动

~~~bash
cd ~/Desktop/LLMdataport
.venv312/bin/python scripts/serve_frontend_demo.py --host 127.0.0.1 --port 8765
~~~

然后在浏览器打开 http://127.0.0.1:8765/。保持终端窗口开启；按 Ctrl+C 停止服务。

### 可选：后台启动

~~~bash
cd ~/Desktop/LLMdataport
nohup .venv312/bin/python scripts/serve_frontend_demo.py --host 127.0.0.1 --port 8765 > frontend_demo_server.out.log 2> frontend_demo_server.err.log < /dev/null &
~~~

后台运行时仍通过 http://127.0.0.1:8765/ 访问。查看占用端口的进程：

~~~bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
~~~

停止服务时，使用上一步输出的 PID：

~~~bash
kill 12345  # 将 12345 替换为上一步查到的 PID
~~~

后台日志保存在项目根目录的 frontend_demo_server.out.log 和 frontend_demo_server.err.log。

## 修改 API 配置后

保存 src/base_llm/provider_config_local.py 后，需要停止并重新启动网页服务，新的配置才会生效。
