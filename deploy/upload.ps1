# رفع ملفات المشروع إلى خادم Oracle Cloud عبر SCP (PowerShell على Windows)
# الاستخدام: .\deploy\upload.ps1 -Host "ubuntu@IP_ADDRESS" -Key "C:\path\to\private_key.ppk أو id_rsa"
param(
    [Parameter(Mandatory=$true)][string]$Host,
    [string]$Key = "",
    [string]$RemoteDir = "/opt/uae-aircraft-monitor"
)

$ErrorActionPreference = "Stop"
$files = @(
    "*.py", "requirements.txt", ".env", ".env.example", "README.md", "deploy\setup_server.sh",
    "data\airports.csv"
)

$scpArgs = @("-r")
if ($Key) { $scpArgs += @("-i", $Key) }
$scpArgs += $files
$scpArgs += "${Host}:${RemoteDir}/"

Write-Host "جارٍ إنشاء المجلد البعيد..."
if ($Key) {
    ssh -i $Key $Host "mkdir -p $RemoteDir/data $RemoteDir/deploy"
} else {
    ssh $Host "mkdir -p $RemoteDir/data $RemoteDir/deploy"
}

Write-Host "جارٍ رفع الملفات..."
& scp @scpArgs
if ($LASTEXITCODE -ne 0) { throw "فشل SCP" }

Write-Host "جارٍ منح صلاحية التنفيذ لسكربت التثبيت..."
if ($Key) {
    ssh -i $Key $Host "chmod +x $RemoteDir/deploy/setup_server.sh"
} else {
    ssh $Host "chmod +x $RemoteDir/deploy/setup_server.sh"
}

Write-Host "تم الرفع. الآن شغّل على الخادم:"
Write-Host "  sudo bash $RemoteDir/deploy/setup_server.sh"
