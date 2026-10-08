# YuiLock APK 构建脚本（aapt2 + javac + d8 + zipalign + apksigner，无 Gradle）
# 自动探测 SDK/JDK（环境变量 → 常见路径 → 本机 E:\dev），签名密钥从仓库外读取：
#   $env:YUILOCK_KS_PATH / $env:YUILOCK_KS_PASS，或 <repo 同级>\keystore-backup\yuilock.keystore(.password)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$out = "$root\out"

function Find-Tool([string[]]$candidates, [string]$probe) {
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c $probe))) { return $c }
    }
    return $null
}

# ---------- SDK ----------
$sdkCandidates = @(
    $env:ANDROID_SDK_ROOT, $env:ANDROID_HOME,
    "$env:USERPROFILE\android-tools\sdk",
    "E:\dev\android-sdk"
)
$sdk = Find-Tool $sdkCandidates "platform-tools\adb.exe"
if (-not $sdk) { throw "找不到 Android SDK（设置 ANDROID_SDK_ROOT 或放到 E:\dev\android-sdk）" }

# ---------- JDK ----------
$jdkCandidates = @($env:JAVA_HOME, "$env:USERPROFILE\android-tools\jdk", "E:\dev\jdk-17")
$jdk = Find-Tool $jdkCandidates "bin\javac.exe"
if (-not $jdk) { throw "找不到 JDK（设置 JAVA_HOME 或放到 E:\dev\jdk-17）" }
$env:JAVA_HOME = $jdk
$env:Path = "$jdk\bin;$env:Path"

# ---------- build-tools（优先 34.0.0，否则取最高版本）----------
$btDir = "$sdk\build-tools"
$btVer = (Get-ChildItem $btDir -Directory | Sort-Object { [version]$_.Name } -Descending |
    Where-Object { Test-Path "$($_.FullName)\aapt2.exe" } | Select-Object -First 1).Name
if (-not $btVer) { throw "build-tools 下找不到 aapt2" }
$bt = "$btDir\$btVer"

# ---------- platform（优先 android-34，否则取最高）----------
$platDir = "$sdk\platforms"
$platNames = @(Get-ChildItem $platDir -Directory |
    Where-Object { Test-Path "$($_.FullName)\android.jar" } | Select-Object -ExpandProperty Name)
if ($platNames -contains 'android-34') {
    $plat = 'android-34'
} else {
    $plat = $platNames | Sort-Object { [version](($_ -replace '^android-', '') + '.0') } -Descending | Select-Object -First 1
}
if (-not $plat) { throw "platforms 下找不到 android.jar" }
$aj = "$platDir\$plat\android.jar"

"==> tools: sdk=$sdk build-tools=$btVer platform=$plat jdk=$jdk"

if (Test-Path $out) { Remove-Item $out -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force $out | Out-Null

"==> aapt2 compile"
& "$bt\aapt2.exe" compile --dir "$root\res" -o "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }

"==> aapt2 link"
& "$bt\aapt2.exe" link -o "$out\base.apk" -I $aj --manifest "$root\AndroidManifest.xml" -R "$out\res.zip" --java "$out\gen" --min-sdk-version 26 --target-sdk-version 34 --version-code 7 --version-name 1.6.1 --auto-add-overlay
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

"==> javac"
$libs = @(Get-ChildItem "$root\libs" -Filter *.jar -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$cp = $aj
if ($libs.Count -gt 0) { $cp = "$aj;" + ($libs -join ";") }
$srcs = @(Get-ChildItem "$root\src", "$out\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
javac -nowarn -encoding UTF-8 -source 8 -target 8 -cp $cp -d "$out\classes" $srcs
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

"==> d8"
$d8in = @(Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName })
if ($libs.Count -gt 0) { $d8in += $libs }
& "$bt\d8.bat" --release --lib $aj --min-api 26 --output $out $d8in
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

"==> package"
jar uf "$out\base.apk" -C $out classes.dex
if ($LASTEXITCODE -ne 0) { throw "jar failed" }
& "$bt\zipalign.exe" -f 4 "$out\base.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

"==> sign"
# 签名密钥不入仓库：优先环境变量，其次 repo 同级 keystore-backup\，最后旧的仓库内路径（兼容）
$ksCandidates = @()
if ($env:YUILOCK_KS_PATH) { $ksCandidates += $env:YUILOCK_KS_PATH }
$ksCandidates += @(
    "$root\yuilock.keystore",                      # 旧布局（仓库内，已 gitignore）
    "$root\..\..\keystore-backup\yuilock.keystore" # repo 外 keystore-backup\（推荐）
)
$ks = $null
foreach ($c in $ksCandidates) { if ($c -and (Test-Path $c)) { $ks = (Resolve-Path $c).Path; break } }
if (-not $ks) { throw "找不到 yuilock.keystore（设置 YUILOCK_KS_PATH 或放到 repo 同级 keystore-backup\）" }

$ksPass = $env:YUILOCK_KS_PASS
if (-not $ksPass) {
    $pwCandidates = @(
        "$root\..\..\keystore-backup\yuilock.keystore.password",
        "$root\keystore.password"
    )
    foreach ($c in $pwCandidates) { if ($c -and (Test-Path $c)) { $ksPass = (Get-Content $c -Raw).Trim(); break } }
}
if (-not $ksPass) { throw "找不到 keystore 口令（设置 YUILOCK_KS_PASS 或放 keystore.password 于 keystore-backup\）" }

& "$bt\apksigner.bat" sign --ks $ks --ks-pass "pass:$ksPass" --key-pass "pass:$ksPass" --out "$out\YuiLock.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }
& "$bt\apksigner.bat" verify "$out\YuiLock.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner verify failed" }

"==> 发布信息（请把这两个值与下载渠道一起公布）"
"APK SHA256:"
(Get-FileHash -Algorithm SHA256 "$out\YuiLock.apk").Hash
"证书指纹 (SHA-256):"
keytool -list -v -alias yuilock -keystore $ks -storepass $ksPass 2>$null | Select-String "SHA256:" | ForEach-Object { $_.Line.Trim() }

"BUILD OK => $out\YuiLock.apk"