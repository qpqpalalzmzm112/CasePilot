# 本地便捷脚本（已 gitignore）：一条命令配好 CasePilot 使用环境
# 做两件事：① 激活虚拟环境（casepilot 命令才在 PATH 里）② 把 .env 的 LLM 配置注入会话
# 用法（注意前面的点，dot-source 才作用于当前会话）：
#   cd c:\Users\123456\Desktop\CasePilot
#   . .\use_env.ps1
#   casepilot gen -i examples\prd_login.md

# ① 激活虚拟环境
$activate = Join-Path $PSScriptRoot '.venv\Scripts\Activate.ps1'
if (Test-Path $activate) { . $activate } else { Write-Host "警告：未找到 .venv，casepilot 命令将不可用" }

# ② 加载 LLM 环境变量
Get-Content "$PSScriptRoot\.env" -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^\s*([^#=]+)=(.*)$') {
        Set-Item -Path "env:$($Matches[1].Trim())" -Value $Matches[2].Trim()
    }
}
Write-Host "环境就绪：venv 已激活，BASE_URL=$env:CASEPILOT_LLM_BASE_URL MODEL=$env:CASEPILOT_LLM_MODEL（API key 已设置）"
Write-Host "现在可直接运行：casepilot gen -i <需求文档.md>"
