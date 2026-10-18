cd "C:\Users\keke.khaka\OneDrive - NTT Ltd\Desktop\Python\agents\System Design+Security Mutli-Agent"
$base  = "http://127.0.0.1:8010"
$token = (Invoke-RestMethod -Method Post -Uri "$base/auth/token" -ContentType "application/json" -Body '{"username":"keke"}').access_token

# Describe what you want designed. A new session_id per run: letters, digits, _ or - only.
$body = @{ requirements = "Design a multi-tenant document storage API where users upload, share and delete files."; session_id = "run-001" } | ConvertTo-Json
[System.IO.File]::WriteAllText("$PWD\body.json", $body)

curl.exe -s -N -X POST "$base/project/design/stream" -H "Authorization: Bearer $token" -H "Content-Type: application/json" --data-binary "@body.json" | ForEach-Object {
  if ($_ -like "event:*") { $ev = $_.Substring(7) }
  elseif ($_ -like "data:*") {
    $d = $_.Substring(6) | ConvertFrom-Json
    switch ($ev) {
      "node"  { if ($d.node -ne "supervisor") { "[{0:HH:mm:ss}] -> {1}" -f (Get-Date), $d.node } }
      "final" { $d | ConvertTo-Json -Depth 6 | Out-File result.json -Encoding utf8; "DONE. approved=$($d.approved) revisions=$($d.total_revisions). Saved to result.json" }
      "error" { "ERROR: $($d.message)" }
    }
  }
}
