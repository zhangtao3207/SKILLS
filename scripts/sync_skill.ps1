param(
    [string]$ProjectSkill = "C:/Users/zhangtao/Desktop/PQM/.codex/skills/hardware-pcb-detect",
    [string]$GlobalSkill = "C:/Users/zhangtao/.codex/skills/hardware-pcb-detect"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ProjectSkill)) {
    throw "Project skill path not found: $ProjectSkill"
}

New-Item -ItemType Directory -Force -Path $GlobalSkill | Out-Null

Copy-Item "$ProjectSkill/SKILL.md" "$GlobalSkill/SKILL.md" -Force
Copy-Item "$ProjectSkill/agents" "$GlobalSkill" -Recurse -Force
Copy-Item "$ProjectSkill/scripts" "$GlobalSkill" -Recurse -Force
Copy-Item "$ProjectSkill/references" "$GlobalSkill" -Recurse -Force

Write-Host "Synced skill content to global path"
Write-Host "Project log folder is intentionally not copied"
