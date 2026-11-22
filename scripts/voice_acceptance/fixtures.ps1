# 使用本机中文合成音色生成验收语料，不采集用户麦克风。
$ErrorActionPreference = 'Stop'
$fixtureRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../.cache/voice-acceptance/fixtures'))
New-Item -ItemType Directory -Force -Path $fixtureRoot | Out-Null
$phrases = [ordered]@{
    uh = '嗯'
    uhuh = '嗯嗯'
    identity = '你是真人还是人工智能？'
    pause_prefix = '我想先问一下'
    pause_suffix = '你可以用普通话和我交流吗？'
    long_prompt = '我第一次用语音助手，请用四句话告诉我可以怎么和你聊天。'
    interrupt = '等一下，请用一句话回答，你能听清我说话吗？'
    interrupt_two = '先停一下，你先听我把问题说完，可以吗？'
    listening = '我这边听得很清楚，你能听到我说话吗？'
}
$voice = New-Object -ComObject SAPI.SpVoice
$token = New-Object -ComObject SAPI.SpObjectToken
$token.SetId('HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens\MSTTS_V110_zhCN_HuihuiM')
$voice.Voice = $token
$voice.Rate = 0
foreach ($item in $phrases.GetEnumerator()) {
    $stream = New-Object -ComObject SAPI.SpFileStream
    $stream.Format.Type = 18
    try {
        $stream.Open((Join-Path $fixtureRoot ($item.Key + '.wav')), 3, $false)
        $voice.AudioOutputStream = $stream
        $voice.Speak($item.Value) | Out-Null
    } finally {
        $stream.Close()
    }
}
$phrases | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $fixtureRoot 'phrases.json') -Encoding utf8
Write-Output ('Generated {0} offline Chinese speech fixtures.' -f $phrases.Count)
