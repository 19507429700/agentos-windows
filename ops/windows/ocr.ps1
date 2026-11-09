# 图片识字、PDF 扫描页识字：用 Windows 自带的识字功能（Windows.Media.Ocr）和 PDF 渲染（Windows.Data.Pdf），不用装东西。
# 由 ops/extract_text.py 调用，不用手动运行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File ocr.ps1 -In 请求.json -Out 结果.json
# 请求（UTF-8 的 JSON）：{"images": ["C:\\...\\01.png", ...]}  或  {"pdf": "C:\\...\\课件.pdf", "pages": [1, 3]}
# 结果（UTF-8 的 JSON）：
#   图片：{"engine": "zh-Hans-CN", "items": [{"path": ..., "ok": true, "text": ...} 或 {"path": ..., "ok": false, "error": ...}]}
#   PDF ：{"engine": ..., "total": 总页数, "items": [{"page": 1, "ok": true, "text": ...}, ...]}
# 用文件传数据，避开中文 Windows 命令行的编码问题。
param([Parameter(Mandatory = $true)][string]$In, [Parameter(Mandatory = $true)][string]$Out)
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

# WinRT 的异步调用在 PowerShell 里要转成 .NET 的 Task 再等
$asTaskOp = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
    Select-Object -First 1
$asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq "IAsyncAction" } |
    Select-Object -First 1
function Await($op, [Type]$type) {
    $task = $asTaskOp.MakeGenericMethod($type).Invoke($null, @($op))
    $null = $task.Wait(-1)
    return $task.Result
}
function AwaitAction($action) {
    $task = $asTaskAction.Invoke($null, @($action))
    $null = $task.Wait(-1)
}

# 识字引擎：优先简体中文（中文版 Windows 自带）；没有就用系统语言
$engine = $null
$engineName = ""
foreach ($tag in @("zh-Hans-CN", "zh-Hans", "zh-CN")) {
    $lang = [Windows.Globalization.Language]::new($tag)
    if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang); $engineName = $tag; break
    }
}
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    $engineName = "系统语言（没装中文识字包，中文可能认不出来）"
}
if ($null -eq $engine) { throw "这台 Windows 没有可用的识字语言包" }

function Read-Stream($stream) {
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $max = [Windows.Media.Ocr.OcrEngine]::MaxImageDimension
    if ($bitmap.PixelWidth -gt $max -or $bitmap.PixelHeight -gt $max) { throw "图片太大（超过 $max 像素）" }
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    return (($result.Lines | ForEach-Object { $_.Text }) -join "`n")
}

function Open-File([string]$path) {
    return Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
}

$req = [IO.File]::ReadAllText($In, [Text.Encoding]::UTF8) | ConvertFrom-Json
$items = New-Object System.Collections.ArrayList
$total = $null

if ($req.images) {
    foreach ($p in $req.images) {
        try {
            $file = Open-File $p
            $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
            try { $text = Read-Stream $stream } finally { $stream.Dispose() }
            $null = $items.Add([ordered]@{ path = $p; ok = $true; text = $text })
        } catch {
            $null = $items.Add([ordered]@{ path = $p; ok = $false; error = $_.Exception.Message })
        }
    }
} elseif ($req.pdf) {
    $doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync((Open-File $req.pdf))) ([Windows.Data.Pdf.PdfDocument])
    $total = [int]$doc.PageCount
    foreach ($n in $req.pages) {
        try {
            $page = $doc.GetPage([uint32]($n - 1))
            $opts = [Windows.Data.Pdf.PdfPageRenderOptions]::new()
            $w = $page.Size.Width; $h = $page.Size.Height
            $scale = 2000 / [Math]::Max([Math]::Max($w, $h), 1)   # 长边渲染到 2000 像素左右
            $opts.DestinationWidth = [uint32]([Math]::Round($w * $scale))
            $opts.DestinationHeight = [uint32]([Math]::Round($h * $scale))
            $ms = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
            try {
                AwaitAction ($page.RenderToStreamAsync($ms, $opts))
                $ms.Seek(0)   # 渲染完读写位置在末尾，回到开头再识字
                $text = Read-Stream $ms
            } finally { $ms.Dispose(); $page.Dispose() }
            $null = $items.Add([ordered]@{ page = [int]$n; ok = $true; text = $text })
        } catch {
            $null = $items.Add([ordered]@{ page = [int]$n; ok = $false; error = $_.Exception.Message })
        }
    }
}

$outObj = [ordered]@{ engine = $engineName; items = $items }
if ($null -ne $total) { $outObj.total = $total }
[IO.File]::WriteAllText($Out, ($outObj | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding $false))
