# YuiLock APK 构建脚本（aapt2 + javac + d8 + zipalign + apksigner，无 Gradle）
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$tools = "$env:USERPROFILE\android-tools"
$sdk = "$tools\sdk"; $jdk = "$tools\jdk"
$env:JAVA_HOME = $jdk
$env:Path = "$jdk\bin;$env:Path"
$bt = "$sdk\build-tools\34.0.0"
$aj = "$sdk\platforms\android-34\android.jar"
$out = "$root\out"

if (Test-Path $out) { Remove-Item $out -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force $out | Out-Null

"==> aapt2 compile"
& "$bt\aapt2.exe" compile --dir "$root\res" -o "$out\res.zip"
if ($LASTEXITCODE -ne 0) { throw "aapt2 compile failed" }

"==> aapt2 link"
& "$bt\aapt2.exe" link -o "$out\base.apk" -I $aj --manifest "$root\AndroidManifest.xml" -R "$out\res.zip" --java "$out\gen" --min-sdk-version 26 --target-sdk-version 34 --version-code 4 --version-name 1.4.0 --auto-add-overlay
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
# 签名口令不写入仓库：优先读环境变量 YUILOCK_KS_PASS，其次 android-app\keystore.password（已 gitignore），都没有则随机生成并落盘
$ksPass = $env:YUILOCK_KS_PASS
$ksPassFile = "$root\keystore.password"
if (-not $ksPass) {
  if (Test-Path $ksPassFile) { $ksPass = (Get-Content $ksPassFile -Raw).Trim() }
}
if (-not $ksPass) {
  $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
  $bytes = New-Object byte[] 24
  $rng.GetBytes($bytes)
  $ksPass = ([Convert]::ToBase64String($bytes) -replace "\+", "a" -replace "/", "b")
  Set-Content -Path $ksPassFile -Value $ksPass -Encoding ascii
  "generated keystore password -> $ksPassFile (gitignored, 请勿提交)"
}
if (-not (Test-Path "$root\yuilock.keystore")) {
  keytool -genkeypair -keystore "$root\yuilock.keystore" -alias yuilock -keyalg RSA -keysize 2048 -validity 10000 -storepass $ksPass -keypass $ksPass -dname "CN=YuiLock" | Out-Null
}
& "$bt\apksigner.bat" sign --ks "$root\yuilock.keystore" --ks-pass "pass:$ksPass" --key-pass "pass:$ksPass" --out "$out\YuiLock.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }
& "$bt\apksigner.bat" verify "$out\YuiLock.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner verify failed" }

"==> 发布信息（请把这两个值与下载渠道一起公布）"
"APK SHA256:"
(Get-FileHash -Algorithm SHA256 "$out\YuiLock.apk").Hash
"证书指纹 (SHA-256):"
keytool -list -v -alias yuilock -keystore "$root\yuilock.keystore" -storepass $ksPass 2>$null | Select-String "SHA256:" | ForEach-Object { $_.Line.Trim() }

"BUILD OK => $out\YuiLock.apk"
