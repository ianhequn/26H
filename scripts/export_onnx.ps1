param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$Yolov5Root,
    [Parameter(Mandatory = $true)]
    [string]$Weights,
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$compat = Join-Path $project "tools\export_yolov5_compat.py"

foreach ($required in @($Python, $Yolov5Root, $Weights, $compat)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

Push-Location $Yolov5Root
try {
    & $Python $compat $Yolov5Root `
        --weights $Weights `
        --imgsz 224 `
        --batch-size 1 `
        --opset 12 `
        --include onnx
    if ($LASTEXITCODE -ne 0) {
        throw "ONNX export failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$exported = [IO.Path]::ChangeExtension($Weights, ".onnx")
$outputPath = [IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($outputPath)) | Out-Null
Copy-Item -LiteralPath $exported -Destination $outputPath -Force
Write-Host "ONNX: $outputPath"

