# 容器验收：build → 健康检查 → 真跑一条流水线 → 跑 5 条黄金集
#
# 为什么要有这个脚本：`docker build` 成功只能说明镜像装好了，
# 说明不了「容器里的知识库能找到」「产物能落回宿主机」。
# 这里每一步都断言产物，最后才打印 PASS。

param(
    [string]$Image = "contentops-agent:0.4",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Fail($msg) {
    Write-Host "[X] $msg" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "没装 docker。先跑 tools\install-docker.cmd（要管理员 + 重启一次），再回来跑这个脚本。"
}

# docker 引擎没起来时 build 会报一堆看不懂的错，先探一下
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "docker 命令在，但引擎没起来。Docker Desktop 还没启动，或者装完没重启。"
}

if (-not $SkipBuild) {
    Write-Host "== 1/4 build ==" -ForegroundColor Cyan
    docker build -t $Image .
    if ($LASTEXITCODE -ne 0) { Fail "build 失败" }
}

Write-Host "== 2/4 健康检查（不碰模型，验代码和知识库）==" -ForegroundColor Cyan
$kb = docker run --rm $Image kb --query "周报"
if ($LASTEXITCODE -ne 0) { Fail "容器里 kb 子命令跑不起来" }
if ($kb -notmatch "知识库分块数") { Fail "kb 输出不对：$kb" }
Write-Host $kb

Write-Host "== 3/4 跑一条完整流水线（mock，不花 token）==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "$root\outputs" | Out-Null
docker run --rm -e LLM_PROVIDER=mock -v "${root}\outputs:/app/outputs" `
    $Image run --topic "用 AI 做周报自动化" --auto approve --out /app/outputs
if ($LASTEXITCODE -ne 0) { Fail "流水线跑失败" }

$md = Get-ChildItem "$root\outputs\*.md" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $md) { Fail "容器说发布了，但宿主机 outputs/ 里没有文件 —— 挂载没生效" }
Write-Host "产物：$($md.FullName)"

Write-Host "== 4/4 黄金集抽 5 条 ==" -ForegroundColor Cyan
docker run --rm $Image eval --dataset evals/golden --limit 5
if ($LASTEXITCODE -ne 0) { Fail "评测跑失败" }

Write-Host ""
Write-Host "[OK] 容器验收通过：$Image" -ForegroundColor Green
