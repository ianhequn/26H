param(
    [Parameter(Mandatory = $true)]
    [string]$NncasePython,
    [Parameter(Mandatory = $true)]
    [string]$NncaseTools,
    [Parameter(Mandatory = $true)]
    [string]$Onnx,
    [Parameter(Mandatory = $true)]
    [string]$CalibrationDir,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [int]$PtqOption = 2,
    [int]$CalibrationSamples = 100
)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
$converter = Join-Path $project "tools\convert_onnx_to_kmodel.py"
$sitePackages = Join-Path $NncaseTools "nncase_env\Lib\site-packages"

foreach ($required in @($NncasePython, $NncaseTools, $Onnx, $CalibrationDir, $converter)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required path not found: $required"
    }
}

$env:DOTNET_ROOT = Join-Path $NncaseTools "dotnet7"
$env:NNCASE_PLUGIN_PATH = Join-Path $sitePackages "nncase\modules\kpu"
$env:PATH = "$sitePackages;$env:DOTNET_ROOT;$env:PATH"

& $NncasePython $converter `
    --model $Onnx `
    --dataset $CalibrationDir `
    --output $Output `
    --input-width 224 `
    --input-height 224 `
    --ptq-option $PtqOption `
    --calibration-samples $CalibrationSamples

if ($LASTEXITCODE -ne 0) {
    throw "KModel conversion failed with exit code $LASTEXITCODE"
}

