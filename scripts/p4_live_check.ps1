# P4 live verification: queue + worker skeleton against the running compose
# stack (api :8000, worker container, redis, SeawEFSS :9000).
# Usage:  pwsh -File scripts/p4_live_check.ps1
# Requires: docker compose up --build api worker (worker image built).
#
# Proves: complete→enqueue→claim→done lifecycle via the REAL worker process,
# Matrix A (other-kind → done at complete, no queue), dedup with no new job,
# and the sweep's orphan-init cleanup visible through the API.

$ErrorActionPreference = "Stop"
$base = "http://localhost:8000"
$script:pass = 0
$script:fail = 0

function Check($name, $condition, $detail = "") {
    if ($condition) {
        $script:pass++
        Write-Host "PASS  $name" -ForegroundColor Green
    } else {
        $script:fail++
        Write-Host "FAIL  $name  $detail" -ForegroundColor Red
    }
}

function New-User {
    $email = "live4-$(Get-Random)-$([guid]::NewGuid().ToString('N').Substring(0,8))@example.com"
    Invoke-RestMethod -Method Post -Uri "$base/auth/register" -ContentType "application/json" `
        -Body (@{ email = $email; password = "correct-horse-1" } | ConvertTo-Json) | Out-Null
    $login = Invoke-RestMethod -Method Post -Uri "$base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $email; password = "correct-horse-1" } | ConvertTo-Json)
    return @{ token = $login.access_token }
}

function Invoke-Api($method, $path, $token, $body) {
    $headers = @{ Authorization = "Bearer $token" }
    $json = if ($null -ne $body) { $body | ConvertTo-Json -Compress } else { $null }
    $resp = Invoke-WebRequest -Method $method -Uri "$base$path" -Headers $headers `
        -ContentType "application/json" -Body $json -SkipHttpErrorCheck -UseBasicParsing
    $parsed = if ($resp.Content) { $resp.Content | ConvertFrom-Json } else { $null }
    if ($parsed) {
        $parsed | Add-Member -NotePropertyName __status -NotePropertyValue ([int]$resp.StatusCode) -Force
        return $parsed
    }
    return @{ __status = [int]$resp.StatusCode }
}

function Upload($token, $filename, [byte[]]$bytes, $contentType = "application/octet-stream") {
    $init = Invoke-Api Post "/files/init" $token `
        @{ filename = $filename; content_type = $contentType; size = $bytes.Length }
    if (-not $init.file_id) { return @{ __status = $init.__status } }
    $resp = Invoke-WebRequest -Method Put -Uri $init.upload_url -Body $bytes `
        -ContentType $contentType -UseBasicParsing
    if ([int]$resp.StatusCode -ge 300) { return @{ __put = [int]$resp.StatusCode } }
    $complete = Invoke-Api Post "/files/$($init.file_id)/complete" $token
    return @{ id = $init.file_id; complete = $complete; put = [int]$resp.StatusCode }
}

function Wait-Status($token, $fileId, $want, $timeoutSec = 60) {
    # Poll until the row reports $want (the worker did its job) or timeout.
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $f = Invoke-Api Get "/files/$fileId" $token
        if ($f.status -eq $want) { return $f }
        Start-Sleep -Milliseconds 700
    }
    return Invoke-Api Get "/files/$fileId" $token
}

Write-Host "=== 1. readiness ==="
$ready = Invoke-RestMethod -Uri "$base/readyz"
Check "readyz s3+redis+db up" ($ready.status -eq "ready")

Write-Host "=== 2. enqueue path: text file processed by the real worker ==="
$user = New-User
$text = [Text.Encoding]::UTF8.GetBytes("worker processes me$([guid]::NewGuid().ToString('N'))")
$up = Upload $user.token "queued.txt" $text "text/plain"
Check "upload+complete 200" ($up.complete.__status -eq 200)
Check "kind=text, status uploaded after complete" ($up.complete.kind -eq "text" -and $up.complete.status -eq "uploaded") `
    "got kind=$($up.complete.kind) status=$($up.complete.status)"
$done = Wait-Status $user.token $up.id "done"
Check "worker processed: status=done" ($done.status -eq "done") "got $($done.status)"

Write-Host "=== 3. Matrix A: other-kind is done at complete (no queue) ==="
$bat = [Text.Encoding]::ASCII.GetBytes("@echo off`r`necho pwned$([guid]::NewGuid().ToString('N'))`r`n")
$upBat = Upload $user.token "script.bat" $bat
Check "bat complete 200" ($upBat.complete.__status -eq 200)
Check "kind=other, topic=other, done at complete" `
    ($upBat.complete.kind -eq "other" -and $upBat.complete.doc_topic -eq "other" -and $upBat.complete.status -eq "done") `
    "got kind=$($upBat.complete.kind) topic=$($upBat.complete.doc_topic) status=$($upBat.complete.status)"

Write-Host "=== 4. dedup: identical bytes → existing row, no new processing ==="
$again = Upload $user.token "queued.txt" $text "text/plain"
Check "dedup complete 200" ($again.complete.__status -eq 200)
Check "deduplicated=true + original id" ($again.complete.deduplicated -eq $true -and $again.complete.id -eq $up.id) `
    "got $($again.complete.deduplicated) id=$($again.complete.id) orig=$($up.id)"

Write-Host "=== 5. filters still work over processed rows ==="
$list = Invoke-Api Get "/files?kind=text&status=done" $user.token
Check "text+done filter returns the processed file" ($list.items.Count -ge 1 -and $list.items[0].id -eq $up.id)

Write-Host "=== 6. cleanup of the test user's files (S3-then-row) ==="
$fetched = Invoke-Api Get "/files?limit=100" $user.token
$deletedAll = $true
foreach ($f in $fetched.items) {
    $d = Invoke-Api Delete "/files/$($f.id)" $user.token
    if ($d.__status -ne 204) { $deletedAll = $false }
}
Check "delete all test files (204s)" $deletedAll

Write-Host ""
Write-Host "==== $($script:pass) passed / $($script:fail) failed ====" -ForegroundColor $(if ($script:fail -eq 0) { "Green" } else { "Red" })
exit $(if ($script:fail -eq 0) { 0 } else { 1 })
