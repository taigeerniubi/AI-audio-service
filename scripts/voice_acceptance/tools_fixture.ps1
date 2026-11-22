# 使用本机中文音色生成必须查询产品资料的语音问题，不使用用户麦克风。
param([switch]$Continuous)
$ErrorActionPreference = 'Stop'
$fixtureRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.cache/voice-acceptance/fixtures'))
New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
$phrase = '请查询产品资料，告诉我 Pro 套餐的月付价格是多少？'
$fixtureName = 'rag_pricing'
if ($Continuous) {
    $phrase = '请查询产品资料中Pro套餐的月付价格'
    $fixtureName = 'rag_pricing_continuous'
}
$voice = New-Object -ComObject SAPI.SpVoice
$token = New-Object -ComObject SAPI.SpObjectToken
$token.SetId('HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\MSTTS_V110_zhCN_HuihuiM')
$voice.Voice = $token
$voice.Rate = 0
$stream = New-Object -ComObject SAPI.SpFileStream
$stream.Format.Type = 18
try {
    $stream.Open((Join-Path $fixtureRoot ($fixtureName + '.wav')), 3, $false)
    $voice.AudioOutputStream = $stream
    $voice.Speak($phrase) | Out-Null
} finally {
    $stream.Close()
}
@{ $fixtureName = @{ text = $phrase; kind = 'offline_synthetic_speech' } } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $fixtureRoot ($fixtureName + '-manifest.json')) -Encoding utf8
if (-not $Continuous) {
    Copy-Item -LiteralPath (Join-Path $fixtureRoot ($fixtureName + '-manifest.json')) -Destination (Join-Path $fixtureRoot 'tools-manifest.json')
}
Write-Output 'Generated one offline tool-query fixture.'
