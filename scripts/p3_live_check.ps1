# P3 live verification: every files route against the running compose stack.
# Usage:  pwsh -File scripts/p3_live_check.ps1
# Requires: docker compose up (api on :8000, SeaweedFS on :9000).

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
    $email = "live-$(Get-Random)-$([guid]::NewGuid().ToString('N').Substring(0,8))@example.com"
    Invoke-RestMethod -Method Post -Uri "$base/auth/register" -ContentType "application/json" `
        -Body (@{ email = $email; password = "correct-horse-1" } | ConvertTo-Json) | Out-Null
    $login = Invoke-RestMethod -Method Post -Uri "$base/auth/login" -ContentType "application/json" `
        -Body (@{ email = $email; password = "correct-horse-1" } | ConvertTo-Json)
    return @{ token = $login.access_token }
}

function Invoke-Api($method, $path, $token, $body) {
    $headers = @{ Authorization = "Bearer $token" }
    $json = if ($null -ne $body) { $body | ConvertTo-Json -Compress } else { $null }
    # -SkipHttpErrorCheck: one uniform path for success AND error bodies; 204
    # has no content → parsed as $null.
    $resp = Invoke-WebRequest -Method $method -Uri "$base$path" -Headers $headers `
        -ContentType "application/json" -Body $json -SkipHttpErrorCheck -UseBasicParsing
    $parsed = if ($resp.Content) { $resp.Content | ConvertFrom-Json } else { $null }
    if ($parsed) {
        $parsed | Add-Member -NotePropertyName __status -NotePropertyValue ([int]$resp.StatusCode) -Force
        $parsed | Add-Member -NotePropertyName __body -NotePropertyValue $resp.Content -Force
        return $parsed
    }
    return @{ __status = [int]$resp.StatusCode; __body = $resp.Content }
}

function Init-File($token, $filename, $size) {
    return Invoke-Api Post "/files/init" $token `
        @{ filename = $filename; content_type = "application/octet-stream"; size = $size }
}

function Put-Bytes($uploadUrl, [byte[]]$bytes) {
    try {
        $resp = Invoke-WebRequest -Method Put -Uri $uploadUrl -Body $bytes `
            -ContentType "application/octet-stream" -UseBasicParsing
        return [int]$resp.StatusCode
    } catch {
        # Distinguish "connection failed" (-1) from real HTTP errors — a null
        # Response would otherwise coerce to 0 and look like success.
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        Write-Host "      (PUT connection failed: $($_.Exception.Message))" -ForegroundColor Yellow
        return -1
    }
}

function Upload($token, $filename, [byte[]]$bytes) {
    $init = Init-File $token $filename $bytes.Length
    if ($init.file_id) {
        $putStatus = Put-Bytes $init.upload_url $bytes
        if ($putStatus -ge 300) { return @{ __put = $putStatus } }
        return Invoke-Api Post "/files/$($init.file_id)/complete" $token
    }
    return $init
}

# --- sample bytes (magic heads decide the kind) --------------------------------
$enc = [System.Text.Encoding]::ASCII
$jpeg = [byte[]](0xFF,0xD8,0xFF,0xE0,0x00,0x10) + $enc.GetBytes("JFIF") + [byte[]]::new(24)
$png  = [byte[]](0x89) + $enc.GetBytes("PNG") + [byte[]](0x0D,0x0A,0x1A,0x0A) + [byte[]]::new(24)
$webp = $enc.GetBytes("RIFF") + [byte[]](0x24,0x00,0x00,0x00) + $enc.GetBytes("WEBPVP8 ") + [byte[]]::new(20)
$pdf  = $enc.GetBytes("%PDF-1.4`n%") + [byte[]](0xE2,0xE3,0xCF,0xD3,0x0A) + [byte[]]::new(24)
$docx = $enc.GetBytes("PK") + [byte[]](0x03,0x04) + $enc.GetBytes("[Content_Types].xml") + [byte[]]::new(16)
$txt  = $enc.GetBytes("hello from the live check`n")
$csv  = $enc.GetBytes("a,b,c`n1,2,3`n")
$bat  = $enc.GetBytes("@echo off`r`necho pwned`r`n")
$fakeJpg = [byte[]](0x4D,0x5A,0x90,0x00,0x03,0x00,0x00,0x00) + [byte[]]::new(24)  # an .exe named .jpg

Write-Host "=== 0. readyz ===" -ForegroundColor Cyan
$ready = Invoke-RestMethod "$base/readyz"
Check "readyz all deps up" ($ready.checks.redis -eq "up" -and $ready.checks.db -eq "up" -and $ready.checks.s3 -eq "up") ($ready | ConvertTo-Json -Compress)

Write-Host "=== 1. register + login ===" -ForegroundColor Cyan
$owner = New-User; $intruder = New-User
Check "owner login (JWT)" ($owner.token.Length -gt 50)
Check "intruder login" ($intruder.token.Length -gt 50)

Write-Host "=== 2. kind classification (init -> PUT -> complete) ===" -ForegroundColor Cyan
$rJpg = Upload $owner.token "photo.jpg" $jpeg
Check "jpeg -> image" ($rJpg.kind -eq "image" -and $rJpg.mime_type -eq "image/jpeg") ($rJpg | ConvertTo-Json -Compress)
$rPng = Upload $owner.token "shot.png" $png
Check "png -> image" ($rPng.kind -eq "image" -and $rPng.mime_type -eq "image/png") ""
$rWebp = Upload $owner.token "anim.webp" $webp
Check "webp -> image" ($rWebp.kind -eq "image" -and $rWebp.mime_type -eq "image/webp") ""
$rPdf = Upload $owner.token "report.pdf" $pdf
Check "pdf -> pdf" ($rPdf.kind -eq "pdf" -and $rPdf.mime_type -eq "application/pdf") ""
$rDocx = Upload $owner.token "notes.docx" $docx
Check "docx -> document" ($rDocx.kind -eq "document") ""
$rTxt = Upload $owner.token "readme.txt" $txt
Check "txt -> text" ($rTxt.kind -eq "text" -and $rTxt.mime_type -eq "text/plain") ""
$rCsv = Upload $owner.token "data.csv" $csv
Check "csv -> text" ($rCsv.kind -eq "text" -and $rCsv.mime_type -eq "text/csv") ""
$rBat = Upload $owner.token "setup.bat" $bat
Check "bat -> other+done (browsable)" ($rBat.kind -eq "other" -and $rBat.doc_topic -eq "other" -and $rBat.status -eq "done") ""
$rFake = Upload $owner.token "trojan.jpg" $fakeJpg
Check "sniff wins: fake .jpg -> other" ($rFake.kind -eq "other" -and $rFake.status -eq "done") ""

Write-Host "=== 3. dedup ===" -ForegroundColor Cyan
$rDup = Upload $owner.token "again.jpg" $jpeg
Check "duplicate bytes -> deduplicated=true, original id" `
    ($rDup.deduplicated -eq $true -and $rDup.id -eq $rJpg.id) ($rDup | ConvertTo-Json -Compress)

Write-Host "=== 4. failure modes ===" -ForegroundColor Cyan
$noPut = Init-File $owner.token "never-put.bin" 10
$comp = Invoke-Api Post "/files/$($noPut.file_id)/complete" $owner.token
Check "complete without PUT -> 409 upload_not_found" ($comp.__status -eq 409 -and $comp.__body -like "*upload_not_found*") ($comp.__body)
$big = Init-File $owner.token "big.bin" (25MB + 1)
Check "oversize init -> 422 file_too_large" ($big.__status -eq 422 -and $big.__body -like "*file_too_large*") ($big.__body)

Write-Host "=== 5. list + cursor + filters ===" -ForegroundColor Cyan
$page1 = Invoke-Api Get "/files?limit=4" $owner.token
Check "list newest-first (init'd-last row, then last upload)" `
    ($page1.items[0].original_name -eq "never-put.bin" -and $page1.items[1].original_name -eq "trojan.jpg") `
    ($page1.items | ForEach-Object { $_.original_name } | Select-Object -First 3)
Check "next_cursor present" ($null -ne $page1.next_cursor)
$page2 = Invoke-Api Get "/files?limit=4&cursor=$($page1.next_cursor)" $owner.token
Check "cursor page 2 distinct" ($page2.items.Count -gt 0 -and $page2.items[0].id -ne $page1.items[0].id)
$imgFilter = Invoke-Api Get "/files?kind=image" $owner.token
Check "kind=image filter" (($imgFilter.items | ForEach-Object { $_.kind } | Select-Object -Unique) -eq "image")
$topicFilter = Invoke-Api Get "/files?doc_topic=other" $owner.token
Check "doc_topic=other filter" (($topicFilter.items | ForEach-Object { $_.doc_topic } | Select-Object -Unique) -eq "other")
$badFilter = Invoke-Api Get "/files?kind=nope" $owner.token
Check "bad kind -> 422 bad_filter" ($badFilter.__status -eq 422 -and $badFilter.__body -like "*bad_filter*") ($badFilter.__body)

Write-Host "=== 6. ownership (404 not 403) ===" -ForegroundColor Cyan
$fid = $rPng.id
if (-not $fid) { throw "PNG upload failed — cannot run ownership checks" }
$cross = Invoke-Api Get "/files/$fid" $intruder.token
Check "cross-user GET -> 404" ($cross.__status -eq 404) ($cross.__status)
$crossDel = Invoke-Api Delete "/files/$fid" $intruder.token
Check "cross-user DELETE -> 404" ($crossDel.__status -eq 404)

Write-Host "=== 7. view url / rename / delete ===" -ForegroundColor Cyan
$url = Invoke-Api Get "/files/$fid/url" $owner.token
if (-not $url.url) { throw "view-url endpoint returned no url: $($url | ConvertTo-Json -Compress)" }
$fetch = Invoke-WebRequest -Uri $url.url -UseBasicParsing
Check "presigned GET serves bytes" ($fetch.StatusCode -eq 200 -and $fetch.Content.Length -eq $png.Length)
$dl = Invoke-Api Get "/files/$fid/url?download=true" $owner.token
Check "download disposition url" ($dl.url -like "*attachment*")
$renamed = Invoke-Api Patch "/files/$fid" $owner.token @{ original_name = "vacation 2026.png" }
Check "PATCH rename" ($renamed.original_name -eq "vacation 2026.png") ($renamed | ConvertTo-Json -Compress)
$del = Invoke-Api Delete "/files/$fid" $owner.token
$gone = Invoke-Api Get "/files/$fid" $owner.token
Check "DELETE -> 204 then GET -> 404" ($del.__status -eq 204 -and $gone.__status -eq 404) ($del.__status)
$goneObj = try { (Invoke-WebRequest -Uri $url.url -UseBasicParsing).StatusCode } catch { [int]$_.Exception.Response.StatusCode }
Check "S3 object gone" ($goneObj -ge 400) ($goneObj)

Write-Host "=== 8. SeaweedFS CORS (browser PUT support) ===" -ForegroundColor Cyan
try {
    $opt = Invoke-WebRequest -Method Options -Uri "http://localhost:9000/smart-file-handler/x" `
        -Headers @{ Origin = "http://localhost:5173"; "Access-Control-Request-Method" = "PUT" } -UseBasicParsing
    $acao = $opt.Headers["Access-Control-Allow-Origin"]
    Check "CORS preflight allows browser PUT" ($acao -eq "*" -or $acao -eq "http://localhost:5173") "Access-Control-Allow-Origin: $acao"
} catch {
    Check "CORS preflight allows browser PUT" $false $_.Exception.Message
}

Write-Host ""
Write-Host "==== $script:pass passed / $script:fail failed ====" -ForegroundColor $(if ($script:fail -eq 0) { "Green" } else { "Red" })
exit $(if ($script:fail -eq 0) { 0 } else { 1 })
