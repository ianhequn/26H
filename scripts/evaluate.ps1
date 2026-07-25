param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$Yolov5Root,
    [Parameter(Mandatory = $true)]
    [string]$DataYaml,
    [string]$Pt,
    [string]$Onnx,
    [string]$KModel,
    [string]$SaveDir,
    [string]$Device = "cpu",
    [switch]$SaveImages,
    [int]$Limit = 0
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$evaluator = Join-Path $project "tools\eval_pt_onnx_kmodel.py"
if (-not $SaveDir) {
    $SaveDir = Join-Path $project "eval_output"
}

foreach ($required in @($Python, $Yolov5Root, $DataYaml, $evaluator)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

$modelArgs = @()
foreach ($entry in @(
    @{ Name = "--pt"; Path = $Pt },
    @{ Name = "--onnx"; Path = $Onnx },
    @{ Name = "--kmodel"; Path = $KModel }
)) {
    if ($entry.Path) {
        if (-not (Test-Path -LiteralPath $entry.Path)) {
            throw "Model path not found: $($entry.Path)"
        }
        $modelArgs += $entry.Name
        $modelArgs += $entry.Path
    }
}

if ($modelArgs.Count -eq 0) {
    throw "Provide at least one model with -Pt, -Onnx, or -KModel."
}

$args = @(
    $evaluator
) + $modelArgs + @(
    "--data", $DataYaml,
    "--imgsz", "224",
    "--conf-thres", "0.001",
    "--iou-thres", "0.60",
    "--max-det", "5",
    "--save-dir", $SaveDir,
    "--device", $Device,
    "--yolov5-root", $Yolov5Root
)

if ($SaveImages) {
    $args += "--save-images"
}
if ($Limit -gt 0) {
    $args += @("--limit", $Limit.ToString())
}

& $Python @args
if ($LASTEXITCODE -ne 0) {
    throw "Evaluation failed with exit code $LASTEXITCODE"
}
