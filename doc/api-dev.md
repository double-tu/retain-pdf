# 本地启动与配置

## 1. 前置条件

本地开发至少需要：

- `cargo`
- `python3` 或 `python`
- `npm`
- `typst`

后端鉴权文件默认读取：

```text
backend/rust_api/auth.local.json
```

首次使用前先复制样例文件：

```bash
cp backend/rust_api/auth.local.example.json backend/rust_api/auth.local.json
```

然后把 `api_keys` 里的值替换成你自己的后端访问 key。

## 2. 一键启动

仓库根目录已经提供了本地开发脚本，会自动做这些事：

- 检查 `cargo`、Python、`npm`、`typst`
- 读取 `backend/rust_api/auth.local.json`
- 生成 `frontend/runtime-config.local.js`
- 如缺少前端依赖，自动执行 `npm install`
- 启动 Rust API 和前端静态服务
- 把日志写入 `.run/backend.log` 和 `.run/frontend.log`

### macOS / Linux

在仓库根目录执行：

```bash
./start-dev.sh
```

停止：

```bash
./stop-dev.sh
```

### Windows PowerShell

在仓库根目录执行：

```powershell
.\start-dev.ps1
```

停止：

```powershell
.\stop-dev.ps1
```

如果首次执行 PowerShell 脚本被策略拦截，可以只对当前终端放开：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

然后再执行：

```powershell
.\start-dev.ps1
```

## 3. 启动后的默认地址

默认启动后访问：

```text
前端: http://127.0.0.1:8080
后端健康检查: http://127.0.0.1:41000/health
简便同步接口: http://127.0.0.1:42000
```

## 4. 鉴权

除 `GET /health` 外，其余接口默认都需要：

```http
X-API-Key: your-rust-api-key
```

注意区分：

- `X-API-Key`：访问 Rust API 的后端凭证
- 请求体里的 `api_key`：下游模型服务的 API Key
- 请求体里的 `mineru_token`：MinerU Token

## 5. 本地 key 来源

本地后端 key 一般来自：

- `backend/rust_api/auth.local.json`
- 或环境变量 `RUST_API_KEYS`

## 6. 常用环境变量

一键启动脚本支持这些环境变量：

- `API_HOST`
- `API_PORT`
- `SIMPLE_PORT`
- `FRONTEND_HOST`
- `FRONTEND_PORT`
- `PYTHON_BIN`
- `TYPST_BIN`
- `UPLOAD_MAX_MB`
- `UPLOAD_MAX_PAGES`
- `RUST_API_UPLOAD_MAX_BYTES`
- `RUST_API_UPLOAD_MAX_PAGES`

Rust API 常用环境变量仍包括：

- `RUST_API_BIND_HOST`
- `DATA_ROOT`
- `RUST_API_SCRIPTS_DIR`
- `RUST_API_PORT`
- `RUST_API_SIMPLE_PORT`

## 7. 手动启动方式

如果你不想使用一键脚本，也可以手动启动。

### 启动后端

```bash
cd backend/rust_api
RUST_API_BIND_HOST=0.0.0.0 \
DATA_ROOT="$(pwd)/../../data" \
RUST_API_SCRIPTS_DIR="$(pwd)/../scripts" \
cargo run
```

### 启动前端

```bash
cd frontend
python3 -m http.server 8080 --bind 0.0.0.0
```

## 8. fork 仓库后的远程配置

如果你是从原仓库拉代码，但想把自己的修改推到自己的 GitHub fork，并且后续继续合入原仓库更新，建议保持下面的远程约定：

- `origin`：你自己的 fork
- `upstream`：原始仓库

查看当前远程：

```bash
git remote -v
```

如果还没配置好，可以执行：

```bash
git remote rename origin upstream
git remote add origin https://github.com/<your-name>/retain-pdf.git
git push -u origin main
```

## 9. 后续拉取原仓库更新并合并

保持本地 `main` 为主同步分支时，最常用的命令是：

```bash
git fetch upstream
git checkout main
git merge upstream/main
git push origin main
```

如果你平时在功能分支开发，建议流程是：

```bash
git checkout main
git fetch upstream
git merge upstream/main
git push origin main
git checkout <your-feature-branch>
git merge main
```
