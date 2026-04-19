# 续火花 SaaS · Windows 部署指南

适合在自己 Windows 电脑/Win 服务器跑，**避开云服务器 IP 被抖音风控**。

## 准备

### 1. 装 Docker Desktop for Windows

下载并安装：https://www.docker.com/products/docker-desktop

- Windows 10/11 64 位（家庭版需要 WSL 2，专业版可用 Hyper-V）
- 装完启动 Docker Desktop，等左下角图标变绿色（约 1-2 分钟）

### 2. 检查 Docker 已就绪

打开 PowerShell（开始菜单搜 PowerShell 右键管理员运行）：

```powershell
docker --version          # 应显示版本号
docker compose version    # 应显示 v2.x
docker pull hello-world   # 测试能拉镜像（如果失败说明 Docker 没启动）
```

## 一键部署

### Step 1：下载脚本

PowerShell 里跑：

```powershell
cd $env:USERPROFILE
iwr "https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/manage.ps1" -OutFile manage.ps1
```

或者直接用浏览器打开下面的 URL 另存为 `manage.ps1`：
https://gist.githubusercontent.com/mrsxs/eb80f17ecee1944c83deb5e0c33d2d78/raw/manage.ps1

### Step 2：运行（首次需要绕过执行策略）

PowerShell（管理员）：

```powershell
powershell -ExecutionPolicy Bypass -File .\manage.ps1
```

或者**右键** `manage.ps1` → 选 **"用 PowerShell 运行"**。

### Step 3：跟着主菜单走

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  续火花 SaaS Windows 部署 (你的电脑名)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  状态: ● 未运行    数据目录: C:\Users\xxx\douyin-spark

  [1] 安装 / 启动 / 升级服务
  [2] 重置管理员密码
  [3] 续期 License
  [4] 查看状态 & 日志
  [5] 卸载

  [0] 退出

选择 [0-5]: 1
```

选 **[1]** → 输入：

1. **License Key** - 卖家给的长字符串
2. **DOUYIN_CAPTCHA_KEY** - 滑块识别 key（可回车跳）
3. **管理员密码** - 自定义或回车随机

脚本自动：
- 拉镜像（约 200MB，第一次较慢）
- 写 `.env`
- 启动容器
- 健康检查
- 询问是否打开浏览器

### Step 4：登录

浏览器打开 `http://127.0.0.1:8765/login`，用你设的密码登录。

## 日常使用

### 重启 / 停止
PowerShell（cd 到 `C:\Users\你\douyin-spark`）：

```powershell
docker compose restart    # 重启
docker compose stop       # 停
docker compose start      # 启
docker compose logs -f    # 实时日志
```

### 改密码 / 续期 / 看日志

直接跑 `manage.ps1`，选对应菜单 [2]/[3]/[4]。

### 升级到新版本

```powershell
.\manage.ps1            # 选 [1] → 检测到已有配置 → 直接拉新镜像并重启
```

## 常见问题

### Q: PowerShell 报"无法加载 ... 因为在此系统上禁止运行脚本"

执行策略被禁用。临时绕过：

```powershell
powershell -ExecutionPolicy Bypass -File .\manage.ps1
```

或永久允许（需管理员）：
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Q: Docker Desktop 启动失败

- Windows 家庭版需要装 WSL 2：管理员 PowerShell 跑 `wsl --install`
- 专业版需要打开 Hyper-V：控制面板 → 程序 → 启用或关闭 Windows 功能
- 重启电脑后再启 Docker Desktop

### Q: 中文显示乱码

PowerShell（管理员）：
```powershell
chcp 65001               # 切 UTF-8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

### Q: `docker pull` 慢

国内可配置 Docker Hub 镜像加速：

Docker Desktop → Settings → Docker Engine，加：
```json
{
  "registry-mirrors": [
    "https://docker.1ms.run",
    "https://docker.m.daocloud.io"
  ]
}
```

应用并重启 Docker Desktop。

### Q: 端口 8765 被占用

改 `docker-compose.yml` 端口映射，比如 `9000:8765`：

```yaml
ports:
  - "9000:8765"
```

然后 `docker compose up -d`，访问 `http://127.0.0.1:9000`。

### Q: 我想让局域网其他设备访问

- 关 Windows 防火墙对 8765 的限制：控制面板 → Windows 安全 → 防火墙 → 高级设置 → 入站规则 → 新建规则 → 端口 8765
- 局域网其他设备访问 `http://你Windows的IP:8765`
- 查 IP：PowerShell `ipconfig`

## 卸载

```powershell
.\manage.ps1     # 选 [5]
```

会问是否同时删数据（cookies/DB），按需选。

## 数据备份

```powershell
# 找 volume 名
docker volume ls | findstr spark

# 备份到桌面
docker run --rm -v spark-data:/data -v $env:USERPROFILE\Desktop:/backup `
    alpine tar czf /backup/spark-backup.tar.gz -C /data .
```
