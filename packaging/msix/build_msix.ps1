<#
.SYNOPSIS
    把 PyInstaller 的 onedir 产物打成一个可以直接上传到 Partner Center 的 MSIX。

.DESCRIPTION
    **为什么走 MSIX。** EXE 提交方式下认证卡在政策 10.2.9（包安全）：安装包是
    Inno 打出来的，没做代码签名，而 EXE 提交要求自己签。MSIX 不一样 —— 提交给
    商店的包由微软重新签名，本地只需要用一张**与清单里 Publisher 同名的自签名
    证书**封包，不需要买代码签名证书。

    另外三件事顺带解决：安装/卸载/升级由系统管，不再有"添加或删除程序里找不到
    条目"这一类校验；不需要管理员权限；包内文件全部进 WindowsApps，用户目录里
    不留自己拷贝的一份。

    清单里的 Identity Name / Publisher **必须与 Partner Center 上那个 MSIX 产品的
    「产品标识」逐字一致**，所以这两项由调用方传进来，脚本不猜。

.PARAMETER IdentityName
    Partner Center → 产品标识 → 包/标识/名称，形如 `12345iamlinxuhan.GomokuAI`

.PARAMETER IdentityPublisher
    Partner Center → 产品标识 → 包/标识/发布者，形如 `CN=1A2B3C4D-....`

.EXAMPLE
    ./packaging/msix/build_msix.ps1 `
        -IdentityName '12345iamlinxuhan.GomokuAI' `
        -IdentityPublisher 'CN=1A2B3C4D-0000-0000-0000-000000000000' `
        -AppVersion 4.3.1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$IdentityName,
    [Parameter(Mandatory = $true)][string]$IdentityPublisher,
    [string]$DisplayName = 'Gomoku AI',
    [string]$PublisherDisplayName = 'iamlinxuhan',
    [string]$Description = 'Offline Gomoku (five-in-a-row) with a C++ search engine',
    [string]$AppVersion = '0.0.0',
    [string]$SourceDir = 'dist\GomokuAI',
    [string]$OutFile = 'dist-msix\GomokuAI_Windows_amd_x86_64.msix',
    # 本地自测时置 $false 出一个不签名的包看体积；上传商店必须签。
    [switch]$SkipSign = $false
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Push-Location $repoRoot
try {
    # ---- 版本号：MSIX 要四段，缺的补 0 ----
    $parts = @(($AppVersion -split '\.') | ForEach-Object { [int]$_ })
    while ($parts.Count -lt 4) { $parts += 0 }
    $msixVersion = ($parts[0..3] -join '.')

    # ---- 舞台目录 ----
    $stage = Join-Path $repoRoot 'dist-msix\stage'
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $stage 'Assets') | Out-Null

    if (-not (Test-Path $SourceDir)) {
        throw "找不到 PyInstaller 产物：$SourceDir（先跑 pyinstaller --onedir）"
    }
    Copy-Item -Path (Join-Path $SourceDir '*') -Destination $stage -Recurse -Force
    if (-not (Test-Path (Join-Path $stage 'GomokuAI.exe'))) {
        throw "包里没有 GomokuAI.exe —— 清单里的 Executable 对不上，装上会启动失败"
    }

    # ---- 磁贴图 ----
    # 源图优先用仓库根那份 1024 的 input.png（图标 .ico 的原始图源），
    # 拿不到再退回 .ico。**不缩放 .ico 的 32 位帧**，那会把噪点放大成色块。
    Add-Type -AssemblyName System.Drawing
    $srcPng = Join-Path $repoRoot 'input.png'
    if (Test-Path $srcPng) {
        $source = [System.Drawing.Image]::FromFile($srcPng)
    }
    else {
        $icoPath = Get-ChildItem (Join-Path $repoRoot '*.ico') | Select-Object -First 1
        if (-not $icoPath) { throw '既没有 input.png 也没有 .ico，磁贴图无从生成' }
        $icon = New-Object System.Drawing.Icon -ArgumentList $icoPath.FullName, 256, 256
        $source = $icon.ToBitmap()
        $icoPath = $null
    }

    $tiles = @(
        @{ Name = 'StoreLogo.png';                 Size = 50 },
        @{ Name = 'StoreLogo.scale-200.png';       Size = 100 },
        @{ Name = 'Square44x44Logo.png';           Size = 44 },
        @{ Name = 'Square44x44Logo.scale-200.png'; Size = 88 },
        @{ Name = 'Square44x44Logo.scale-400.png'; Size = 176 },
        @{ Name = 'Square150x150Logo.png';         Size = 150 },
        @{ Name = 'Square150x150Logo.scale-200.png'; Size = 300 }
    )
    foreach ($t in $tiles) {
        $bmp = New-Object System.Drawing.Bitmap($t.Size, $t.Size)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $g.DrawImage($source, 0, 0, $t.Size, $t.Size)
        $g.Dispose()
        $bmp.Save((Join-Path $stage "Assets\$($t.Name)"), [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
    }
    $source.Dispose()

    # ---- 清单 ----
    $tpl = Get-Content (Join-Path $PSScriptRoot 'AppxManifest.xml.in') -Raw -Encoding UTF8
    # 逐句替换而不是方法链：`$x.` 写在行尾的续行要 PowerShell 7 才认，
    # 这段脚本偶尔也会被人拖到 Windows PowerShell 5.1 上跑一次。
    $manifest = $tpl
    $manifest = $manifest.Replace('@IDENTITY_NAME@', $IdentityName)
    $manifest = $manifest.Replace('@IDENTITY_PUBLISHER@', $IdentityPublisher)
    $manifest = $manifest.Replace('@VERSION@', $msixVersion)
    $manifest = $manifest.Replace('@DISPLAY_NAME@', $DisplayName)
    $manifest = $manifest.Replace('@PUBLISHER_DISPLAY_NAME@', $PublisherDisplayName)
    $manifest = $manifest.Replace('@DESCRIPTION@', $Description)
    # UTF-8 **无 BOM**：带 BOM 时 makeappx 会把 BOM 当成 XML 前面的杂物而报错。
    [IO.File]::WriteAllText((Join-Path $stage 'AppxManifest.xml'), $manifest,
        [System.Text.UTF8Encoding]::new($false))

    # ---- makeappx / signtool ----
    $kitRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (-not (Test-Path $kitRoot)) {
        throw "找不到 Windows SDK：$kitRoot —— MSIX 打包需要 makeappx.exe"
    }
    $findTool = {
        param($exe)
        Get-ChildItem $kitRoot -Recurse -Filter $exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending | Select-Object -First 1
    }
    $makeappx = & $findTool 'makeappx.exe'
    if (-not $makeappx) { throw "Windows SDK 里找不到 makeappx.exe（$kitRoot）" }

    New-Item -ItemType Directory -Force -Path (Split-Path $OutFile -Parent) | Out-Null
    if (Test-Path $OutFile) { Remove-Item -Force $OutFile }
    Write-Host "打包：$OutFile（清单版本 $msixVersion）"
    & $makeappx.FullName pack /d $stage /p $OutFile /o | Write-Host
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $OutFile)) {
        throw "makeappx 失败（退出码 $LASTEXITCODE）"
    }

    if ($SkipSign) {
        Write-Warning '跳过签名：这个包不能上传到 Partner Center'
    }
    else {
        # ---- 自签名证书 ----
        # 主题必须与清单里的 Publisher 逐字相同，否则封包报
        # "The certificate subject name is not valid"。
        # 商店会对上传的包重新签名，所以本地这张不需要是受信任的根。
        $signtool = & $findTool 'signtool.exe'
        if (-not $signtool) { throw "Windows SDK 里找不到 signtool.exe（$kitRoot）" }

        $pfx = Join-Path $repoRoot 'dist-msix\msix-signing.pfx'
        $pfxPassword = [Guid]::NewGuid().ToString('N')
        $secure = ConvertTo-SecureString -String $pfxPassword -Force -AsPlainText
        if (Test-Path $pfx) { Remove-Item -Force $pfx }
        $cert = New-SelfSignedCertificate -Type CodeSigningCert `
            -Subject $IdentityPublisher `
            -KeyUsage DigitalSignature `
            -FriendlyName 'Gomoku AI MSIX (self-signed)' `
            -CertStoreLocation 'Cert:\CurrentUser\My' `
            -KeyExportPolicy Exportable
        Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $secure | Out-Null

        & $signtool.FullName sign /fd SHA256 /f $pfx /p $pfxPassword $OutFile | Write-Host
        if ($LASTEXITCODE -ne 0) { throw "signtool 签名失败（退出码 $LASTEXITCODE）" }

        # 证书与私钥只留在本机，不进包也不进仓库。
        Remove-Item -Force $pfx
        Remove-Item -Path "Cert:\CurrentUser\My\$($cert.Thumbprint)" -Force
        Write-Host "已用自签名证书签名（主题 $IdentityPublisher）"
    }

    $size = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
    Write-Host "完成：$OutFile（$size MB）"
}
finally {
    Pop-Location
}
