<#
.SYNOPSIS
  Simple GUI wrapper around New-SilentConnectNode.ps1 - same idea as
  AmneziaVPN's own "configure your server" screen: IP, SSH user, password or
  key, go. Doesn't create the server - rent that from wherever you like first.
#>

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainScript = Join-Path $ScriptDir "New-SilentConnectNode.ps1"

$form = New-Object System.Windows.Forms.Form
$form.Text = "SilentConnect - Configure your server"
$form.Size = New-Object System.Drawing.Size(620, 700)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false

function New-Label($text, $x, $y) {
    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text = $text
    $lbl.Location = New-Object System.Drawing.Point($x, $y)
    $lbl.AutoSize = $true
    $form.Controls.Add($lbl)
    return $lbl
}

function New-TextBox($x, $y, $width, $isPassword) {
    $tb = New-Object System.Windows.Forms.TextBox
    $tb.Location = New-Object System.Drawing.Point($x, $y)
    $tb.Width = $width
    if ($isPassword) { $tb.UseSystemPasswordChar = $true }
    $form.Controls.Add($tb)
    return $tb
}

$y = 15

New-Label "Server IP[:port] (rent this from Hetzner/Aeza/anyone first, same as always):" 15 $y | Out-Null
$y += 20
$txtAddress = New-TextBox 15 $y 570 $false
$txtAddress.Text = "255.255.255.255:22"
$txtAddress.ForeColor = [System.Drawing.Color]::Gray
$y += 35

New-Label "SSH username:" 15 $y | Out-Null
$y += 20
$txtUser = New-TextBox 15 $y 200 $false
$txtUser.Text = "root"
$y += 35

New-Label "Password or SSH private key (paste the whole key incl. BEGIN/END lines):" 15 $y | Out-Null
$y += 20
$txtAuth = New-Object System.Windows.Forms.TextBox
$txtAuth.Location = New-Object System.Drawing.Point(15, $y)
$txtAuth.Width = 570
$txtAuth.Height = 70
$txtAuth.Multiline = $true
$txtAuth.ScrollBars = "Vertical"
$form.Controls.Add($txtAuth)
$y += 80

$lblAuthHint = New-Label "A pasted key is used directly. A plain password needs the Posh-SSH module (one-time: Install-Module Posh-SSH)." 15 $y
$lblAuthHint.ForeColor = [System.Drawing.Color]::DimGray
$lblAuthHint.Font = New-Object System.Drawing.Font($lblAuthHint.Font.FontFamily, 7.5)
$y += 25

New-Label "Base domain (e.g. silentconnect.net) - leave empty for a throwaway sslip.io test:" 15 $y | Out-Null
$y += 20
$txtBaseDomain = New-TextBox 15 $y 570 $false
$y += 35

New-Label "Subdomain (e.g. fi2) - required if base domain is set:" 15 $y | Out-Null
$y += 20
$txtSubdomain = New-TextBox 15 $y 570 $false
$y += 40

$grpSni = New-Object System.Windows.Forms.GroupBox
$grpSni.Text = "Camouflage SNIs (defaults match the rest of the fleet - only change if you know why)"
$grpSni.Location = New-Object System.Drawing.Point(15, $y)
$grpSni.Size = New-Object System.Drawing.Size(570, 90)
$form.Controls.Add($grpSni)

$lblC = New-Object System.Windows.Forms.Label
$lblC.Text = "Classic:"; $lblC.Location = New-Object System.Drawing.Point(10, 25); $lblC.AutoSize = $true
$grpSni.Controls.Add($lblC)
$txtSniClassic = New-Object System.Windows.Forms.TextBox
$txtSniClassic.Location = New-Object System.Drawing.Point(70, 22); $txtSniClassic.Width = 150; $txtSniClassic.Text = "sber.ru"
$grpSni.Controls.Add($txtSniClassic)

$lblF = New-Object System.Windows.Forms.Label
$lblF.Text = "Fast:"; $lblF.Location = New-Object System.Drawing.Point(240, 25); $lblF.AutoSize = $true
$grpSni.Controls.Add($lblF)
$txtSniFast = New-Object System.Windows.Forms.TextBox
$txtSniFast.Location = New-Object System.Drawing.Point(280, 22); $txtSniFast.Width = 150; $txtSniFast.Text = "st.kinopoisk.ru"
$grpSni.Controls.Add($txtSniFast)

$lblG = New-Object System.Windows.Forms.Label
$lblG.Text = "gRPC:"; $lblG.Location = New-Object System.Drawing.Point(10, 55); $lblG.AutoSize = $true
$grpSni.Controls.Add($lblG)
$txtSniGrpc = New-Object System.Windows.Forms.TextBox
$txtSniGrpc.Location = New-Object System.Drawing.Point(70, 52); $txtSniGrpc.Width = 150; $txtSniGrpc.Text = "vk.com"
$grpSni.Controls.Add($txtSniGrpc)

$y += 100

$btnCreate = New-Object System.Windows.Forms.Button
$btnCreate.Text = "Configure Server"
$btnCreate.Location = New-Object System.Drawing.Point(15, $y)
$btnCreate.Size = New-Object System.Drawing.Size(150, 35)
$btnCreate.BackColor = [System.Drawing.Color]::FromArgb(46, 125, 50)
$btnCreate.ForeColor = [System.Drawing.Color]::White
$form.Controls.Add($btnCreate)

$btnCopyRoute = New-Object System.Windows.Forms.Button
$btnCopyRoute.Text = "Copy route-bypass command"
$btnCopyRoute.Location = New-Object System.Drawing.Point(175, $y)
$btnCopyRoute.Size = New-Object System.Drawing.Size(200, 35)
$btnCopyRoute.Enabled = $false
$form.Controls.Add($btnCopyRoute)

$lblStatus = New-Object System.Windows.Forms.Label
$lblStatus.Text = "Idle"
$lblStatus.Location = New-Object System.Drawing.Point(390, ($y + 8))
$lblStatus.AutoSize = $true
$lblStatus.Font = New-Object System.Drawing.Font($lblStatus.Font, [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($lblStatus)

$y += 40

$btnTestSub = New-Object System.Windows.Forms.Button
$btnTestSub.Text = "Get test subscription"
$btnTestSub.Location = New-Object System.Drawing.Point(15, $y)
$btnTestSub.Size = New-Object System.Drawing.Size(230, 30)
$form.Controls.Add($btnTestSub)

$y += 40

$txtLog = New-Object System.Windows.Forms.TextBox
$txtLog.Location = New-Object System.Drawing.Point(15, $y)
$txtLog.Size = New-Object System.Drawing.Size(570, 260)
$txtLog.Multiline = $true
$txtLog.ScrollBars = "Vertical"
$txtLog.ReadOnly = $true
$txtLog.Font = New-Object System.Drawing.Font("Consolas", 8.5)
$txtLog.BackColor = [System.Drawing.Color]::Black
$txtLog.ForeColor = [System.Drawing.Color]::LightGreen
$form.Controls.Add($txtLog)

$script:job = $null
$script:lastIp = ""
$script:lastGw = ""

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000

$timer.Add_Tick({
    if ($null -eq $script:job) { return }

    $newOutput = Receive-Job -Job $script:job
    foreach ($line in $newOutput) {
        $text = $line | Out-String
        $txtLog.AppendText($text)

        if ($text -match "route -p add ([\d\.]+) mask [\d\.]+ ([\d\.]+)") {
            $script:lastIp = $Matches[1]
            $script:lastGw = $Matches[2]
            $btnCopyRoute.Enabled = $true
        }
    }

    if ($script:job.State -in @("Completed", "Failed", "Stopped")) {
        if ($script:job.State -eq "Completed") {
            $lblStatus.Text = "Done"
            $lblStatus.ForeColor = [System.Drawing.Color]::DarkGreen
        } else {
            $lblStatus.Text = "Failed - see log"
            $lblStatus.ForeColor = [System.Drawing.Color]::DarkRed
            $errText = ($script:job.ChildJobs[0].Error | Out-String)
            if ($errText.Trim()) { $txtLog.AppendText("`r`nERROR:`r`n$errText") }
        }
        Remove-Job -Job $script:job -Force
        $script:job = $null
        $btnCreate.Enabled = $true
        $timer.Stop()
    }
})

$btnCopyRoute.Add_Click({
    if ($script:lastIp -and $script:lastGw) {
        $cmd = "route -p add $($script:lastIp) mask 255.255.255.255 $($script:lastGw) metric 1"
        [System.Windows.Forms.Clipboard]::SetText($cmd)
        [System.Windows.Forms.MessageBox]::Show("Copied to clipboard:`n$cmd`n`nPaste this into an elevated PowerShell window.", "Route bypass command") | Out-Null
    }
})

$btnTestSub.Add_Click({
    if ([string]::IsNullOrWhiteSpace($txtAddress.Text) -or $txtAddress.Text -eq "255.255.255.255:22") {
        [System.Windows.Forms.MessageBox]::Show("Server IP is required.", "Missing field") | Out-Null
        return
    }
    if ([string]::IsNullOrWhiteSpace($txtAuth.Text)) {
        [System.Windows.Forms.MessageBox]::Show("Password or SSH private key is required.", "Missing field") | Out-Null
        return
    }

    # Works against any server already provisioned by "Configure Server" at
    # least once (it reuses that run's cached test client) - does not run
    # the full install, just ensures a subscription link exists for it.
    $remoteCmd = @'
STATE_DIR=/root/.silentconnect-provision
if [[ ! -f "$STATE_DIR/test_uuid" ]]; then
  echo "ERROR_NO_TEST_CLIENT"
  exit 1
fi
TEST_UUID="$(cat "$STATE_DIR/test_uuid")"
if [[ -f "$STATE_DIR/test_subid" ]]; then
  TEST_SUBID="$(cat "$STATE_DIR/test_subid")"
else
  TEST_SUBID="$(openssl rand -hex 8)"
  echo "$TEST_SUBID" > "$STATE_DIR/test_subid"
fi
PUBLIC_HOST="$(grep '^PUBLIC_HOST=' /root/subjson-service/subjson.env | cut -d= -f2)"
CADDY_PORT="$(grep -oP 'https_port\s+\K[0-9]+' /etc/caddy/Caddyfile 2>/dev/null || echo 4430)"
python3 -c "
import sqlite3, json, time
test_uuid = '$TEST_UUID'
test_subid = '$TEST_SUBID'
now_ms = int(time.time() * 1000)
conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cur = conn.cursor()
cur.execute('SELECT id, settings FROM inbounds WHERE tag = ?', ('registry-only',))
row = cur.fetchone()
client = {'id': test_uuid, 'flow': 'xtls-rprx-vision', 'email': 'provision-test', 'limitIp': 0, 'totalGB': 0, 'expiryTime': 0, 'enable': True, 'tgId': 0, 'subId': test_subid, 'comment': '', 'reset': 0, 'created_at': now_ms, 'updated_at': now_ms}
if row is None:
    settings = json.dumps({'clients': [client], 'decryption': 'none'})
    stream_settings = json.dumps({'network': 'tcp', 'security': 'none'})
    sniffing = json.dumps({'enabled': False})
    cur.execute('INSERT INTO inbounds (user_id, up, down, total, remark, enable, expiry_time, listen, port, protocol, settings, stream_settings, tag, sniffing) VALUES (1, 0, 0, 0, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?)', ('registry-only', '127.0.0.1', 23385, 'vless', settings, stream_settings, 'registry-only', sniffing))
else:
    inbound_id, raw_settings = row
    settings = json.loads(raw_settings)
    if not any(c.get('id') == test_uuid for c in settings.get('clients', [])):
        settings.setdefault('clients', []).append(client)
        cur.execute('UPDATE inbounds SET settings = ? WHERE id = ?', (json.dumps(settings), inbound_id))
conn.commit()
conn.close()
"
echo "SUBSCRIPTION_URL=https://${PUBLIC_HOST}:${CADDY_PORT}/my-secret-sub/import/${TEST_SUBID}"
'@

    $ip = $txtAddress.Text
    $sshPort = 22
    if ($txtAddress.Text -match "^(.+):(\d+)$") {
        $ip = $Matches[1]
        $sshPort = [int]$Matches[2]
    }
    $authValue = $txtAuth.Text
    $isKey = $authValue.TrimStart().StartsWith("-----BEGIN")

    $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
    $btnTestSub.Enabled = $false
    try {
        $output = ""
        if ($isKey) {
            $tempKeyFile = Join-Path $env:TEMP "silentconnect_key_$([guid]::NewGuid()).tmp"
            Set-Content -Path $tempKeyFile -Value $authValue -NoNewline
            icacls $tempKeyFile /inheritance:r | Out-Null
            icacls $tempKeyFile /grant:r "$($env:USERNAME):(R)" | Out-Null
            try {
                $output = & ssh -p $sshPort -o StrictHostKeyChecking=no -i $tempKeyFile "$($txtUser.Text)@$ip" $remoteCmd 2>&1 | Out-String
            } finally {
                Remove-Item $tempKeyFile -Force -ErrorAction SilentlyContinue
            }
        } else {
            if (-not (Get-Module -ListAvailable -Name Posh-SSH)) {
                throw "Password auth needs the 'Posh-SSH' module: Install-Module Posh-SSH -Scope CurrentUser -Force"
            }
            Import-Module Posh-SSH
            $secpasswd = ConvertTo-SecureString $authValue -AsPlainText -Force
            $cred = New-Object System.Management.Automation.PSCredential($txtUser.Text, $secpasswd)
            $session = New-SSHSession -ComputerName $ip -Port $sshPort -Credential $cred -AcceptKey -ErrorAction Stop
            try {
                $result = Invoke-SSHCommand -SessionId $session.SessionId -Command $remoteCmd -TimeOut 30
                $output = $result.Output | Out-String
            } finally {
                Remove-SSHSession -SessionId $session.SessionId | Out-Null
            }
        }

        if ($output -match "ERROR_NO_TEST_CLIENT") {
            [System.Windows.Forms.MessageBox]::Show("This server has no cached test client yet - run 'Configure Server' on it at least once first.", "No test client found") | Out-Null
        } elseif ($output -match "SUBSCRIPTION_URL=(\S+)") {
            $url = $Matches[1]
            [System.Windows.Forms.Clipboard]::SetText($url)
            [System.Windows.Forms.MessageBox]::Show("Copied to clipboard:`n$url", "Test subscription link") | Out-Null
        } else {
            [System.Windows.Forms.MessageBox]::Show("Could not parse a result. Raw output:`n$output", "Unexpected output") | Out-Null
        }
    } catch {
        [System.Windows.Forms.MessageBox]::Show("Failed: $($_.Exception.Message)", "Error") | Out-Null
    } finally {
        $form.Cursor = [System.Windows.Forms.Cursors]::Default
        $btnTestSub.Enabled = $true
    }
})

$btnCreate.Add_Click({
    if ([string]::IsNullOrWhiteSpace($txtAddress.Text) -or $txtAddress.Text -eq "255.255.255.255:22") {
        [System.Windows.Forms.MessageBox]::Show("Server IP is required.", "Missing field") | Out-Null
        return
    }
    if ([string]::IsNullOrWhiteSpace($txtAuth.Text)) {
        [System.Windows.Forms.MessageBox]::Show("Password or SSH private key is required.", "Missing field") | Out-Null
        return
    }
    if (-not [string]::IsNullOrWhiteSpace($txtBaseDomain.Text) -and [string]::IsNullOrWhiteSpace($txtSubdomain.Text)) {
        [System.Windows.Forms.MessageBox]::Show("Base domain was given but subdomain is empty - need both, or neither.", "Missing field") | Out-Null
        return
    }

    $txtLog.Clear()
    $lblStatus.Text = "Running..."
    $lblStatus.ForeColor = [System.Drawing.Color]::DarkOrange
    $btnCreate.Enabled = $false
    $btnCopyRoute.Enabled = $false

    $authValue = $txtAuth.Text
    $isKey = $authValue.TrimStart().StartsWith("-----BEGIN")

    $paramList = @{
        ServerAddress = $txtAddress.Text
        SshUser = $txtUser.Text
        SniClassic = $txtSniClassic.Text
        SniFast = $txtSniFast.Text
        SniGrpc = $txtSniGrpc.Text
    }
    if ($isKey) { $paramList["SshKeyContent"] = $authValue } else { $paramList["SshPassword"] = $authValue }
    if (-not [string]::IsNullOrWhiteSpace($txtBaseDomain.Text)) {
        $paramList["BaseDomain"] = $txtBaseDomain.Text
        $paramList["Subdomain"] = $txtSubdomain.Text
    }

    $script:job = Start-Job -ScriptBlock {
        param($scriptPath, $params)
        # Write-Host goes to the Information stream (6), not the success/error
        # streams - 2>&1 alone never brings it into what Receive-Job sees, so
        # every colored "==>" status line AND the route-bypass command itself
        # (both printed via Write-Host in New-SilentConnectNode.ps1) silently
        # never reached this GUI's log box or its route-bypass regex at all.
        & $scriptPath @params *>&1
    } -ArgumentList $MainScript, $paramList

    $timer.Start()
})

$form.Add_FormClosing({
    if ($null -ne $script:job) {
        Stop-Job -Job $script:job -ErrorAction SilentlyContinue
        Remove-Job -Job $script:job -Force -ErrorAction SilentlyContinue
    }
})

[void]$form.ShowDialog()
