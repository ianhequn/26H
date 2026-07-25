param(
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$Yolov5Root,
    [Parameter(Mandatory = $true)]
    [string]$TeacherWeights,
    [Parameter(Mandatory = $true)]
    [string]$StudentWeights,
    [Parameter(Mandatory = $true)]
    [string]$DataYaml,
    [string]$OutputDir,
    [int]$Epochs = 40,
    [int]$BatchSize = 16,
    [string]$Device = "cpu"
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$trainer = Join-Path $project "tools\train_distill.py"
$hyp = Join-Path $project "configs\hyp_distill.yaml"
if (-not $OutputDir) {
    $OutputDir = Join-Path $project "runs"
}

foreach ($required in @($Python, $Yolov5Root, $TeacherWeights, $StudentWeights, $DataYaml, $trainer, $hyp)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

$env:PYTHONPATH = "$Yolov5Root;$env:PYTHONPATH"
Push-Location $Yolov5Root
try {
    & $Python $trainer `
        --teacher-weights $TeacherWeights `
        --weights $StudentWeights `
        --data $DataYaml `
        --hyp $hyp `
        --epochs $Epochs `
        --batch-size $BatchSize `
        --imgsz 224 `
        --device $Device `
        --workers 0 `
        --patience 12 `
        --project $OutputDir `
        --name "yolov5n_224_distilled" `
        --exist-ok `
        --kd-weight 0.04 `
        --kd-temperature 2.0 `
        --kd-object-threshold 0.02 `
        --kd-warmup-epochs 5
    if ($LASTEXITCODE -ne 0) {
        throw "Distillation training failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

