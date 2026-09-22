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
& "$bt\aapt2.exe" link -o "$out\base.apk" -I $aj --manifest "$root\AndroidManifest.xml" -R "$out\res.zip" --java "$out\gen" --min-sdk-version 26 --target-sdk-version 34 --version-code 1 --version-name 1.0.0 --auto-add-overlay
if ($LASTEXITCODE -ne 0) { throw "aapt2 link failed" }

"==> javac"
$srcs = @(Get-ChildItem "$root\src", "$out\gen" -Recurse -Filter *.java | ForEach-Object { $_.FullName })
javac -nowarn -encoding UTF-8 -source 8 -target 8 -cp $aj -d "$out\classes" $srcs
if ($LASTEXITCODE -ne 0) { throw "javac failed" }

"==> d8"
$classes = @(Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object { $_.FullName })
& "$bt\d8.bat" --release --lib $aj --min-api 26 --output $out $classes
if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

"==> package"
jar uf "$out\base.apk" -C $out classes.dex
if ($LASTEXITCODE -ne 0) { throw "jar failed" }
& "$bt\zipalign.exe" -f 4 "$out\base.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

"==> sign"
if (-not (Test-Path "$root\yuilock.keystore")) {
  keytool -genkeypair -keystore "$root\yuilock.keystore" -alias yuilock -keyalg RSA -keysize 2048 -validity 10000 -storepass yuilock123 -keypass yuilock123 -dname "CN=YuiLock" | Out-Null
}
& "$bt\apksigner.bat" sign --ks "$root\yuilock.keystore" --ks-pass pass:yuilock123 --key-pass pass:yuilock123 --out "$out\YuiLock.apk" "$out\aligned.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }
& "$bt\apksigner.bat" verify "$out\YuiLock.apk"
if ($LASTEXITCODE -ne 0) { throw "apksigner verify failed" }

"BUILD OK => $out\YuiLock.apk"
