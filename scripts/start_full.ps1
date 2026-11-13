param(
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8000,
    [ValidateRange(1, 65535)]
    [int]$FrontendPort = 3000,
    [switch]$SkipDocker
)

# 完整版启动脚本：PostgreSQL + etcd + MinIO + Milvus（Docker）+ FastAPI + Next.js。
# 与 scripts/start_rag.ps1 的区别：那个走 SQLite + 关闭 Milvus 的降级演示模式，
# 本脚本使用 .env 里的真实配置（PostgreSQL、Milvus、本地 Qwen Embedding）。

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDirectory = Join-Path $projectRoot "backend"
$frontendDirectory = Join-Path $projectRoot "frontend"
$pythonPath = Join-Path $backendDirectory ".venv\Scripts\python.exe"
$dataDirectory = Join-Path $projectRoot ".data"
$backendOutputLog = Join-Path $dataDirectory "devflow-backend.out.log"
$backendErrorLog = Join-Path $dataDirectory "devflow-backend.err.log"
$frontendOutputLog = Join-Path $dataDirectory "devflow-frontend.out.log"
$frontendErrorLog = Join-Path $dataDirectory "devflow-frontend.err.log"

# Docker Desktop 默认按用户安装，其 CLI 目录未必在当前进程的 PATH 中。
$dockerBin = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin"
if (Test-Path -LiteralPath $dockerBin -PathType Container) {
    $env:PATH = "$dockerBin;$env:PATH"
}

if (-not (Test-Path -LiteralPath (Join-Path $projectRoot ".env") -PathType Leaf)) {
    throw "未找到 .env。请先复制 .env.example 并填写 LLM_API_KEY / GITHUB_TOKEN。"
}
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "未找到 backend/.venv。请先创建虚拟环境并安装 backend/requirements.txt。"
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory "node_modules") -PathType Container)) {
    throw "未找到 frontend/node_modules。请先在 frontend 目录执行 npm install。"
}

New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null

# 本地 Qwen Embedding 的模型缓存；不设置的话会用用户主目录下的默认缓存。
$env:HF_HOME = Join-Path $dataDirectory "hf-cache"

$backendProcess = $null
$frontendProcess = $null

try {
    if (-not $SkipDocker) {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            throw "未找到 docker 命令。请先安装 Docker Desktop，或改用 .\scripts\start_rag.ps1（SQLite 降级演示模式，不需要 Docker）。"
        }

        # Docker Desktop 按用户安装时不会注册成 Windows 服务：Daemon 没跑的话，
        # docker 只会报 “找不到 npipe”，而后端会因为连不上 5432 直接启动失败。
        # 所以这里先探测引擎，必要时拉起 Docker Desktop 再等它就绪。
        $dockerReady = $false
        try {
            docker info --format "{{.ServerVersion}}" *> $null
            $dockerReady = ($LASTEXITCODE -eq 0)
        }
        catch {
            # 引擎没运行时会走到这里，属于预期情况。
            $dockerReady = $false
        }

        if (-not $dockerReady) {
            $dockerDesktop = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\Docker Desktop.exe"
            if (-not (Test-Path -LiteralPath $dockerDesktop -PathType Leaf)) {
                $dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
            }
            if (-not (Test-Path -LiteralPath $dockerDesktop -PathType Leaf)) {
                throw "Docker 引擎未运行，且没有找到 Docker Desktop.exe。请手动启动 Docker Desktop 后重试。"
            }
            Write-Host "Docker 引擎未运行，正在启动 Docker Desktop..."
            Start-Process -FilePath $dockerDesktop
            for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
                try {
                    docker info --format "{{.ServerVersion}}" *> $null
                    if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
                }
                catch {
                    # 还在启动中，继续等。
                }
                Start-Sleep -Seconds 5
            }
        }
        if (-not $dockerReady) {
            throw "Docker 引擎在 300 秒内没有就绪。请打开 Docker Desktop 确认状态为 Running 后重试。"
        }

        Write-Host "启动 Docker 依赖（postgres / etcd / minio / milvus）..."
        Push-Location $projectRoot
        try {
            docker compose up -d postgres etcd minio milvus
            $postgresReady = $false
            for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
                try {
                    # psycopg 连不上就绪前的 5432，所以这里必须能容忍失败后重试，
                    # 不能让 $ErrorActionPreference='Stop' 把脚本直接终止掉。
                    docker exec devflow-ai-postgres pg_isready -U devflow -d devflow *> $null
                    if ($LASTEXITCODE -eq 0) { $postgresReady = $true; break }
                }
                catch {
                    # 容器还在初始化，继续等。
                }
                Start-Sleep -Seconds 2
            }
            if (-not $postgresReady) {
                throw "PostgreSQL 在 120 秒内没有就绪，请检查 docker compose logs postgres。"
            }
            $milvusReady = $false
            for ($attempt = 0; $attempt -lt 60; $attempt += 1) {
                try {
                    Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:9091/healthz" -TimeoutSec 3 | Out-Null
                    $milvusReady = $true
                    break
                }
                catch {
                    Start-Sleep -Seconds 2
                }
            }
            if (-not $milvusReady) {
                throw "Milvus 在 120 秒内没有就绪，请检查 docker compose logs milvus。"
            }
        }
        finally {
            Pop-Location
        }
    }

    $env:PYTHONPATH = $backendDirectory
    $backendProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $backendDirectory `
        -RedirectStandardOutput $backendOutputLog `
        -RedirectStandardError $backendErrorLog `
        -WindowStyle Hidden `
        -PassThru

    $backendReady = $false
    for ($attempt = 0; $attempt -lt 90; $attempt += 1) {
        if ($backendProcess.HasExited) {
            throw "后端进程提前退出，请检查 $backendErrorLog。"
        }
        try {
            Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/health" -TimeoutSec 3 | Out-Null
            $backendReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $backendReady) {
        throw "后端在 45 秒内没有就绪，请检查 $backendErrorLog。"
    }

    # 子进程继承当前进程的环境变量（-Environment 需要 PowerShell 7，本机是 5.1）。
    $env:NEXT_PUBLIC_API_BASE_URL = "http://127.0.0.1:$BackendPort"
    $frontendProcess = Start-Process `
        -FilePath "npm.cmd" `
        -ArgumentList @("run", "dev", "--", "-p", "$FrontendPort") `
        -WorkingDirectory $frontendDirectory `
        -RedirectStandardOutput $frontendOutputLog `
        -RedirectStandardError $frontendErrorLog `
        -WindowStyle Hidden `
        -PassThru

    $frontendReady = $false
    for ($attempt = 0; $attempt -lt 180; $attempt += 1) {
        if ($frontendProcess.HasExited) {
            throw "前端进程提前退出，请检查 $frontendErrorLog。"
        }
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$FrontendPort" -TimeoutSec 3 | Out-Null
            $frontendReady = $true
            break
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $frontendReady) {
        throw "前端在 90 秒内没有就绪，请检查 $frontendErrorLog。"
    }

    Write-Host "DevFlow AI 完整版已启动：" -ForegroundColor Green
    Write-Host "  前端：http://127.0.0.1:$FrontendPort"
    Write-Host "  后端：http://127.0.0.1:$BackendPort"
    Write-Host "  API 文档：http://127.0.0.1:$BackendPort/docs"
    Write-Host "  后端 PID：$($backendProcess.Id)"
    Write-Host "  前端 PID：$($frontendProcess.Id)"
    Write-Host "  停止命令：Stop-Process -Id $($backendProcess.Id),$($frontendProcess.Id)"
    Write-Host "  停止依赖：docker compose down"
}
catch {
    if ($frontendProcess -and -not $frontendProcess.HasExited) {
        Stop-Process -Id $frontendProcess.Id
    }
    if ($backendProcess -and -not $backendProcess.HasExited) {
        Stop-Process -Id $backendProcess.Id
    }
    throw
}
