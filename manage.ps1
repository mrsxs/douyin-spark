# 续火花 SaaS Windows 一键运维脚本
#
# 用法：
#   1. 装 Docker Desktop for Windows: https://www.docker.com/products/docker-desktop
#   2. 下载本脚本：
#      iwr https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/manage.ps1 -OutFile manage.ps1
#   3. 右键 → "用 PowerShell 运行"
#      或 PowerShell 里：powershell -ExecutionPolicy Bypass -File .\manage.ps1
#
# 功能：
#   [1] 安装/启动/升级
#   [2] 重置管理员密码
#   [3] 续期 License
#   [4] 查看状态 & 日志
#   [5] 卸载

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

# ─── 配置 ────────────────────────────────
$COMPOSE_URL = "https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/docker-compose.yml"
$IMAGE       = "mrsxs/douyin-spark:latest"
$INSTALL_DIR = if ($env:INSTALL_DIR) { $env:INSTALL_DIR } else { "$env:USERPROFILE\douyin-spark" }
$CONTAINER   = "douyin-spark"

# ─── 工具函数 ────────────────────────────
function Hr   { Write-Host ("━" * 60) -ForegroundColor White }
function Title($t) { Hr; Write-Host "  $t" -ForegroundColor White; Hr }
function Info($m)  { Write-Host "✓ $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "⚠ $m" -ForegroundColor Yellow }
function Err($m)   { Write-Host "❌ $m" -ForegroundColor Red }
function Die($m)   { Err $m; exit 1 }

function Read-Password($prompt) {
    $sec = Read-Host -Prompt $prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        return [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Test-Container { (docker ps --format '{{.Names}}' 2>$null) -match "^$CONTAINER$" }

function Wait-Ready {
    Write-Host "等待容器就绪（最多 60 秒）..."
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:8765/healthz" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($r.StatusCode -eq 200) {
                Info "服务已就绪"
                return $true
            }
        } catch { Start-Sleep -Seconds 1 }
    }
    Warn "60 秒内未就绪，看 [4] 状态 & 日志"
    return $false
}

function Check-Deps {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Err "未安装 Docker Desktop"
        Write-Host "  下载: https://www.docker.com/products/docker-desktop" -ForegroundColor Cyan
        Die "请先安装 Docker Desktop 并启动它"
    }
    try {
        docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw }
    } catch {
        Die "Docker Desktop 未启动。请打开 Docker Desktop 应用后再试。"
    }
    try {
        docker compose version 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw }
    } catch {
        Die "Docker Compose v2 不可用。Docker Desktop 4.0+ 才内置。"
    }
}

# ─── [1] 安装/启动 ─────────────────────────────
function Do-Install {
    Title "[1] 安装 / 启动 / 升级"
    Set-Location $INSTALL_DIR

    if (-not (Test-Path "docker-compose.yml")) {
        Write-Host "下载 docker-compose.yml..."
        Invoke-WebRequest $COMPOSE_URL -OutFile "docker-compose.yml" -UseBasicParsing
        Info "docker-compose.yml 已下载"
    } else {
        Info "docker-compose.yml 已存在"
    }

    # 检查已有 .env
    if ((Test-Path ".env") -and ((Get-Content ".env" -Raw) -match "LICENSE_KEY=.+")) {
        Warn "已有 .env 配置"
        $reconfig = Read-Host "重新配置？[y/N]"
        if ($reconfig -notmatch "^[Yy]$") {
            Write-Host "`n拉取最新镜像并重启..."
            docker compose pull
            docker compose up -d
            Wait-Ready | Out-Null
            return
        }
    }

    # 配置向导
    Write-Host "`n━━━ 配置向导（4 步）━━━" -ForegroundColor Cyan

    # [1/4] 数据存储路径
    Write-Host "`n[1/4] 数据存储路径（cookies / DB / 日志 / 加密密钥全存这里）" -ForegroundColor White
    Write-Host "  默认: $INSTALL_DIR\data" -ForegroundColor Gray
    Write-Host "  也可填绝对路径如 D:\MyData\spark" -ForegroundColor Gray
    $DATA_PATH = (Read-Host "DATA_PATH (回车用默认)").Trim()
    if (-not $DATA_PATH) { $DATA_PATH = ".\data" }
    # 算真实路径
    if ([System.IO.Path]::IsPathRooted($DATA_PATH)) {
        $REAL_DATA_PATH = $DATA_PATH
    } else {
        $clean = $DATA_PATH -replace '^\.\\|^\./', ''
        $REAL_DATA_PATH = Join-Path $INSTALL_DIR $clean
    }
    try {
        New-Item -ItemType Directory -Force -Path $REAL_DATA_PATH | Out-Null
    } catch {
        Die "无法创建 $REAL_DATA_PATH（权限？）"
    }
    Info "数据目录：$REAL_DATA_PATH（删除容器不丢）"

    # [2/4] License Key
    Write-Host "`n[2/4] License Key" -ForegroundColor White
    while ($true) {
        $LICENSE_KEY = (Read-Host "LICENSE_KEY (粘贴卖家给的长字符串)").Trim()
        if (-not $LICENSE_KEY) { Warn "不能为空"; continue }
        if ($LICENSE_KEY -notmatch "\.") {
            Warn "格式异常（无 . 分隔签名）"
            $ok = Read-Host "继续？[y/N]"
            if ($ok -notmatch "^[Yy]$") { continue }
        }
        break
    }
    Info "License 已接收（$($LICENSE_KEY.Length) 字符）"

    # [3/4] 验证码 Key
    Write-Host "`n[3/4] 验证码 Key（DDDocr 滑块服务，可回车跳过）" -ForegroundColor White
    $CAPTCHA_KEY = (Read-Host "DOUYIN_CAPTCHA_KEY").Trim()
    if ($CAPTCHA_KEY) { Info "验证码 Key 已接收" } else { Warn "跳过" }

    # [4/4] 管理员密码
    Write-Host "`n[4/4] 管理员密码（自定义或回车随机）" -ForegroundColor White
    $ADMIN_PASS = Read-Password "ADMIN_PASSWORD"
    $useCustom = $false
    if ($ADMIN_PASS) {
        $ADMIN_PASS2 = Read-Password "再次确认"
        if ($ADMIN_PASS -ne $ADMIN_PASS2) { Die "两次输入不一致" }
        if ($ADMIN_PASS.Length -lt 6) { Die "密码至少 6 位" }
        $useCustom = $true
        Info "密码已接收（$($ADMIN_PASS.Length) 位）"
    } else {
        Info "跳过，将由容器自动生成强随机密码"
    }

    # 拉镜像
    Write-Host "`n拉取镜像 $IMAGE ..."
    docker pull $IMAGE
    if ($LASTEXITCODE -ne 0) { Die "镜像拉取失败" }

    # 算 bcrypt
    $ADMIN_HASH = ""
    if ($useCustom) {
        Write-Host "`n生成 bcrypt 哈希..."
        $env:_TMP_ADMIN_PASS = $ADMIN_PASS
        $ADMIN_HASH = (docker run --rm -e ADMIN_PASS=$ADMIN_PASS $IMAGE python -c "import os,bcrypt; print(bcrypt.hashpw(os.environ['ADMIN_PASS'].encode()[:72], bcrypt.gensalt(12)).decode())").Trim()
        Remove-Item env:_TMP_ADMIN_PASS -ErrorAction SilentlyContinue
        if (-not $ADMIN_HASH) { Die "哈希生成失败" }
        Info "哈希已生成"
    }

    # 写 .env
    $envContent = @(
        "# 由 manage.ps1 生成 - $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')"
        "DATA_VOLUME_PATH=$DATA_PATH"
        "LICENSE_KEY=$LICENSE_KEY"
    )
    if ($CAPTCHA_KEY) { $envContent += "DOUYIN_CAPTCHA_KEY=$CAPTCHA_KEY" }
    $envContent += "ADMIN_USERNAME=admin"
    if ($ADMIN_HASH) {
        # ⚠️ docker compose 把 $2b$12$xxx 当变量引用 → 必须 $ → $$ 转义
        $escapedHash = $ADMIN_HASH -replace '\$', '$$'
        $envContent += "ADMIN_PASSWORD_HASH=$escapedHash"
    }
    # 用 .NET WriteAllLines 避免 PowerShell 5.1 Out-File 写 UTF-16 BOM
    [System.IO.File]::WriteAllLines("$INSTALL_DIR\.env", $envContent, [System.Text.UTF8Encoding]::new($false))
    Info ".env 已生成"

    # 启动
    docker compose up -d
    Wait-Ready | Out-Null

    # 输出访问信息
    Write-Host ""
    $localIP = (Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Manual,Dhcp -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -ne "127.0.0.1" } | Select-Object -First 1).IPAddress
    if (-not $localIP) { $localIP = "127.0.0.1" }
    Write-Host "  🌐 Web 地址: http://${localIP}:8765" -ForegroundColor White
    Write-Host "  👤 用户名  : admin" -ForegroundColor White
    if ($useCustom) {
        Write-Host "  🔐 密码    : (你刚输入的)" -ForegroundColor White
    } else {
        Write-Host "`n  📋 系统自动生成的 admin 密码（只显示一次）："
        Start-Sleep -Seconds 2
        docker compose logs 2>&1 | Select-String -Pattern "管理员账户" -Context 0,8
    }

    # 自动开浏览器
    Write-Host ""
    $open = Read-Host "现在打开浏览器访问？[Y/n]"
    if ($open -notmatch "^[Nn]$") {
        Start-Process "http://${localIP}:8765/login"
    }
}

# ─── [2] 重置管理员密码 ────────────────────────
function Do-ResetPassword {
    Title "[2] 重置管理员密码"
    Set-Location $INSTALL_DIR
    if (-not (Test-Container)) { Die "容器 $CONTAINER 未运行，请先选 [1]" }

    $username = Read-Host "用户名（默认 admin）"
    if (-not $username) { $username = "admin" }

    $newPwd = $null
    while ($true) {
        $newPwd = Read-Password "新密码（隐藏）"
        if (-not $newPwd) { Warn "不能为空"; continue }
        if ($newPwd.Length -lt 6) { Warn "至少 6 位"; continue }
        $newPwd2 = Read-Password "再次确认"
        if ($newPwd -eq $newPwd2) { break }
        Warn "两次不一致，重新输入"
    }

    # 准备 inline python
    $py = @'
import os, bcrypt
from datetime import datetime
from app.db import SessionLocal, init_db
from app.models import User
init_db()
username = os.environ.get('UNAME', 'admin')
pwd = os.environ['NEW']
h = bcrypt.hashpw(pwd.encode()[:72], bcrypt.gensalt(12)).decode()
with SessionLocal() as db:
    u = db.query(User).filter(User.username == username).first()
    if not u:
        u = User(username=username, password_hash=h, is_admin=True, is_active=True,
                 expires_at=datetime(2099, 12, 31), max_accounts=100)
        db.add(u)
        msg = f'✓ 已创建管理员 {username}'
    else:
        u.password_hash = h; u.is_active = True; u.is_admin = True
        msg = f'✓ 已重置 {username} 密码'
    db.commit()
    print(msg)
    print('hash 前缀:', h[:30] + '...')
'@
    $tmp = "$env:TEMP\_reset_admin.py"
    $py | Out-File -FilePath $tmp -Encoding UTF8

    docker cp $tmp "${CONTAINER}:/app/_reset_admin.py" | Out-Null
    docker exec -e NEW=$newPwd -e UNAME=$username --workdir /app $CONTAINER python /app/_reset_admin.py
    docker exec $CONTAINER rm -f /app/_reset_admin.py | Out-Null
    Remove-Item $tmp -ErrorAction SilentlyContinue

    Info "密码已生效，立即可登录"
    Write-Host "  🌐 http://127.0.0.1:8765/login" -ForegroundColor White
    Write-Host "  👤 $username" -ForegroundColor White
}

# ─── [3] 续期 License ──────────────────────────
function Do-Renew {
    Title "[3] 续期 License"
    Set-Location $INSTALL_DIR
    if (-not (Test-Path ".env")) { Die ".env 不存在，请先选 [1]" }

    Write-Host "当前 License："
    docker compose logs --tail 200 2>&1 | Select-String -Pattern "License 已验证" -Context 0,5
    Write-Host ""

    while ($true) {
        $newLic = (Read-Host "粘贴卖家新签的 License Key").Trim()
        if (-not $newLic) { Warn "不能为空"; continue }
        if ($newLic -notmatch "\.") {
            Warn "格式异常"
            $ok = Read-Host "继续？[y/N]"
            if ($ok -notmatch "^[Yy]$") { continue }
        }
        break
    }

    $bak = ".env.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Copy-Item ".env" $bak
    Info "旧 .env 备份到 $bak"

    $envText = Get-Content ".env" -Raw
    if ($envText -match "(?m)^LICENSE_KEY=") {
        $envText = $envText -replace "(?m)^LICENSE_KEY=.*$", "LICENSE_KEY=$newLic"
        $envText | Out-File -FilePath ".env" -Encoding UTF8 -NoNewline
    } else {
        Add-Content ".env" "`nLICENSE_KEY=$newLic"
    }
    Info ".env 已更新"

    Write-Host "重启容器..."
    docker compose restart
    Start-Sleep -Seconds 3

    Write-Host ""
    $verified = docker compose logs --tail 30 2>&1 | Select-String -Pattern "License 已验证" -Context 0,5
    if ($verified) {
        $verified
        Info "续期成功"
    } else {
        Warn "未找到验证标志，看完整日志："
        docker compose logs --tail 20
        Warn "可恢复旧 license: copy $bak .env; docker compose restart"
    }
}

# ─── [4] 状态 & 日志 ───────────────────────────
function Do-Status {
    Title "[4] 状态 & 日志"
    Set-Location $INSTALL_DIR
    Write-Host "`n── 容器状态 ──"
    docker compose ps
    Write-Host "`n── 最近 30 行日志 ──"
    docker compose logs --tail 30
}

# ─── [5] 卸载 ──────────────────────────────────
function Do-Uninstall {
    Title "[5] 卸载"
    Set-Location $INSTALL_DIR
    Warn "此操作将停止并删除容器"
    Write-Host "  数据目录 .\data (cookies/DB/日志) 不会自动删除，可手动清理。" -ForegroundColor Yellow
    $confirm = Read-Host "确定删除容器？[y/N]"
    if ($confirm -notmatch "^[Yy]$") { Write-Host "已取消"; return }

    docker compose down
    Info "容器已停止删除"

    Write-Host ""
    $delData = Read-Host "也删除 .\data 目录（cookies/DB/日志全删，不可恢复）？[y/N]"
    if ($delData -match "^[Yy]$") {
        Remove-Item -Recurse -Force ".\data"
        Info "数据目录已删除"
    } else {
        Info "数据目录保留在：$INSTALL_DIR\data"
    }
}

# ─── 主菜单 ────────────────────────────────────
Check-Deps
New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

while ($true) {
    Clear-Host
    Title "续火花 SaaS Windows 部署 ($env:COMPUTERNAME)"
    Write-Host ""
    if (Test-Container) {
        Write-Host "  状态: ● 运行中" -ForegroundColor Green -NoNewline
        Write-Host "    数据目录: $INSTALL_DIR"
    } else {
        Write-Host "  状态: ● 未运行" -ForegroundColor Red -NoNewline
        Write-Host "    数据目录: $INSTALL_DIR"
    }
    Write-Host ""
    Write-Host "  [1] 安装 / 启动 / 升级服务"
    Write-Host "  [2] 重置管理员密码"
    Write-Host "  [3] 续期 License"
    Write-Host "  [4] 查看状态 & 日志"
    Write-Host "  [5] 卸载"
    Write-Host ""
    Write-Host "  [0] 退出"
    Write-Host ""
    $choice = Read-Host "选择 [0-5]"

    switch ($choice) {
        "1" { Do-Install }
        "2" { Do-ResetPassword }
        "3" { Do-Renew }
        "4" { Do-Status }
        "5" { Do-Uninstall }
        "0" { Write-Host "再见 👋"; exit 0 }
        default { Warn "无效选择" }
    }

    Write-Host ""
    Read-Host "按回车返回主菜单" | Out-Null
}
