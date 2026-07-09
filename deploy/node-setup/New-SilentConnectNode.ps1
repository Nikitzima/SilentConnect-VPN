<#
.SYNOPSIS
  Configure an already-existing bare server into a SilentConnect node - same
  idea as AmneziaVPN's own "point me at your server" flow: give it an IP,
  SSH user, and a password or private key, and it does the rest over SSH.

.DESCRIPTION
  This does NOT create the server for you - rent it from whichever provider
  you want (Hetzner, Aeza, anyone), the same few clicks as always, then hand
  its IP + SSH credentials to this script.

.EXAMPLE
  .\New-SilentConnectNode.ps1 -ServerAddress "1.2.3.4" -SshUser root -SshKeyPath "$env:USERPROFILE\.ssh\id_ed25519" -BaseDomain "silentconnect.net" -Subdomain "fi2"

.EXAMPLE
  .\New-SilentConnectNode.ps1 -ServerAddress "1.2.3.4:22" -SshUser root -SshPassword "hunter2" -BaseDomain "silentconnect.net" -Subdomain "fi2"
#>

param(
    [Parameter(Mandatory=$true)][string]$ServerAddress,
    [string]$SshUser = "root",
    [string]$SshKeyPath = "",
    [string]$SshKeyContent = "",
    [string]$SshPassword = "",
    [string]$BaseDomain = "",
    [string]$Subdomain = "",
    [string]$SniClassic = "sber.ru",
    [string]$SniFast = "st.kinopoisk.ru",
    [string]$SniGrpc = "vk.com",
    [switch]$SkipRouteBypass
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalSubjson = Join-Path $ScriptDir "..\subjson-service\app.py"
$ProvisionScript = Join-Path $ScriptDir "provision_silentconnect_node.sh"

if (-not (Test-Path $LocalSubjson)) { throw "Can't find subjson-service/app.py at $LocalSubjson" }
if (-not (Test-Path $ProvisionScript)) { throw "Can't find provision_silentconnect_node.sh at $ProvisionScript" }

# ---------- parse address ----------
$ip = $ServerAddress
$sshPort = 22
if ($ServerAddress -match "^(.+):(\d+)$") {
    $ip = $Matches[1]
    $sshPort = [int]$Matches[2]
}

# ---------- resolve SSH auth: key file > pasted key content > password ----------
$sshArgs = @("-p", $sshPort, "-o", "StrictHostKeyChecking=no")
$tempKeyFile = $null

if (-not [string]::IsNullOrWhiteSpace($SshKeyPath)) {
    if (-not (Test-Path $SshKeyPath)) { throw "SSH key file not found: $SshKeyPath" }
    $sshArgs += @("-i", $SshKeyPath)
} elseif (-not [string]::IsNullOrWhiteSpace($SshKeyContent)) {
    $tempKeyFile = Join-Path $env:TEMP "silentconnect_key_$([guid]::NewGuid()).tmp"
    # OpenSSH refuses key files that are world/group-readable - write then lock down.
    Set-Content -Path $tempKeyFile -Value $SshKeyContent -NoNewline
    icacls $tempKeyFile /inheritance:r | Out-Null
    icacls $tempKeyFile /grant:r "$($env:USERNAME):(R)" | Out-Null
    $sshArgs += @("-i", $tempKeyFile)
} elseif (-not [string]::IsNullOrWhiteSpace($SshPassword)) {
    if (-not (Get-Module -ListAvailable -Name Posh-SSH)) {
        throw "Password auth needs the 'Posh-SSH' PowerShell module (plain ssh.exe can't do non-interactive passwords). Install it once with: Install-Module Posh-SSH -Scope CurrentUser -Force -- or use a private key instead, which needs no extra module."
    }
} else {
    throw "Need one of: -SshKeyPath, -SshKeyContent, or -SshPassword"
}

function Invoke-RemoteCommand($command) {
    if (-not [string]::IsNullOrWhiteSpace($SshPassword)) {
        Import-Module Posh-SSH
        $secpasswd = ConvertTo-SecureString $SshPassword -AsPlainText -Force
        $cred = New-Object System.Management.Automation.PSCredential($SshUser, $secpasswd)
        $session = New-SSHSession -ComputerName $ip -Port $sshPort -Credential $cred -AcceptKey -ErrorAction Stop
        try {
            $result = Invoke-SSHCommand -SessionId $session.SessionId -Command $command -TimeOut 600
            Write-Output $result.Output
            if ($result.ExitStatus -ne 0) { throw "Remote command failed (exit $($result.ExitStatus))" }
        } finally {
            Remove-SSHSession -SessionId $session.SessionId | Out-Null
        }
    } else {
        & ssh @sshArgs "${SshUser}@${ip}" $command
        if ($LASTEXITCODE -ne 0) { throw "ssh exited with code $LASTEXITCODE" }
    }
}

function Copy-FileToRemote($localPath, $remotePath) {
    if (-not [string]::IsNullOrWhiteSpace($SshPassword)) {
        Import-Module Posh-SSH
        $secpasswd = ConvertTo-SecureString $SshPassword -AsPlainText -Force
        $cred = New-Object System.Management.Automation.PSCredential($SshUser, $secpasswd)

        # Split-Path assumes Windows-style paths and mangles a plain Unix
        # "/root/subjson-service/app.py" (no separator survives) - slice the
        # string by hand instead, since the remote side is always POSIX here.
        $lastSlash = $remotePath.LastIndexOf("/")
        $remoteDir = $remotePath.Substring(0, $lastSlash)
        $remoteFileName = $remotePath.Substring($lastSlash + 1)
        $localFileName = Split-Path $localPath -Leaf

        Set-SCPItem -ComputerName $ip -Port $sshPort -Credential $cred -Path $localPath -Destination $remoteDir -AcceptKey -ErrorAction Stop

        # Set-SCPItem uploads keeping the LOCAL file's name - rename remotely
        # if the caller wanted a different name at the destination.
        if ($localFileName -ne $remoteFileName) {
            $session = New-SSHSession -ComputerName $ip -Port $sshPort -Credential $cred -AcceptKey -ErrorAction Stop
            try {
                $mv = Invoke-SSHCommand -SessionId $session.SessionId -Command "mv '$remoteDir/$localFileName' '$remotePath'" -TimeOut 30
                if ($mv.ExitStatus -ne 0) { throw "remote rename failed: $($mv.Error)" }
            } finally {
                Remove-SSHSession -SessionId $session.SessionId | Out-Null
            }
        }
    } else {
        $scpArgs = $sshArgs | Where-Object { $_ -ne "-p" -and $_ -notmatch "^\d+$" } # scp uses -P not -p for port
        $portIdx = [array]::IndexOf($sshArgs, "-p")
        if ($portIdx -ge 0) { $scpArgs = @("-P", $sshArgs[$portIdx+1]) + ($sshArgs | Select-Object -Skip ($portIdx+2)) }
        & scp @scpArgs $localPath "${SshUser}@${ip}:${remotePath}"
        if ($LASTEXITCODE -ne 0) { throw "scp exited with code $LASTEXITCODE" }
    }
}

try {
    Write-Host "==> Checking SSH connectivity to $ip`:$sshPort" -ForegroundColor Green
    Invoke-RemoteCommand "echo SSH_OK"

    $UseRealDomain = -not [string]::IsNullOrWhiteSpace($BaseDomain)
    if ($UseRealDomain) {
        if ([string]::IsNullOrWhiteSpace($Subdomain)) { throw "-BaseDomain was given without -Subdomain - need both to build a real domain" }
        $Domain = "$Subdomain.$BaseDomain"

        Write-Host ""
        Write-Host "==> DNS record needed before subscriptions will work" -ForegroundColor Yellow
        Write-Host "    Go to your DNS provider (Cloudflare or wherever $BaseDomain is managed) and create:" -ForegroundColor Yellow
        Write-Host "        Type: A" -ForegroundColor Cyan
        Write-Host "        Name: $Subdomain" -ForegroundColor Cyan
        Write-Host "        Value: $ip" -ForegroundColor Cyan
        Write-Host "        Proxy status: DNS only / grey cloud (NOT proxied - Let's Encrypt needs to reach this server directly)" -ForegroundColor Cyan
        Write-Host ""

        Write-Host "==> Waiting for $Domain to resolve to $ip" -ForegroundColor Green
        $resolved = $false
        for ($i = 0; $i -lt 60; $i++) {
            try {
                $answer = (Resolve-DnsName -Name $Domain -Type A -ErrorAction Stop | Where-Object { $_.Type -eq "A" } | Select-Object -First 1).IPAddress
            } catch { $answer = $null }
            if ($answer -eq $ip) { $resolved = $true; break }
            if ($i -eq 0 -or $i % 6 -eq 0) {
                Write-Host "    still waiting ($([Math]::Round($i*5/60,1)) min) - current answer: $answer" -ForegroundColor DarkGray
            }
            Start-Sleep -Seconds 5
        }
        if (-not $resolved) {
            throw "$Domain never resolved to $ip after 5 minutes. Check the DNS record, then just re-run this script - it's safe to repeat."
        }
        Write-Host "    resolved correctly" -ForegroundColor Green
    } else {
        $Domain = "$ip.sslip.io"
        Write-Host "    no -BaseDomain/-Subdomain given, using sslip.io: $Domain (fine for a quick test, not for real clients)"
    }

    if (-not $SkipRouteBypass) {
        Write-Host "==> If you plan to test profiles from THIS pc afterward, add a direct-route bypass so the test doesn't get tunneled through your own VPN:" -ForegroundColor Yellow
        $gw = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Sort-Object RouteMetric | Select-Object -First 1).NextHop
        Write-Host "    route -p add $ip mask 255.255.255.255 $gw metric 1" -ForegroundColor Cyan
        Write-Host "    (run that in an elevated PowerShell - not needed just to provision the server)" -ForegroundColor Yellow
    }

    Write-Host "==> Uploading subjson-service and the provisioning script" -ForegroundColor Green
    Invoke-RemoteCommand "mkdir -p /root/subjson-service"
    Copy-FileToRemote $LocalSubjson "/root/subjson-service/app.py"
    Copy-FileToRemote $ProvisionScript "/root/provision.sh"
    Invoke-RemoteCommand "chmod +x /root/provision.sh"

    Write-Host "==> Running the provisioning script (this takes a few minutes)" -ForegroundColor Green
    Invoke-RemoteCommand "bash /root/provision.sh --domain $Domain --subjson-repo unused-local-copy --sni-classic $SniClassic --sni-fast $SniFast --sni-grpc $SniGrpc"

    Write-Host ""
    Write-Host "==> Done." -ForegroundColor Green
    Write-Host "    Server: $ip"
    Write-Host "    Domain: $Domain"
} finally {
    if ($tempKeyFile -and (Test-Path $tempKeyFile)) { Remove-Item $tempKeyFile -Force }
}
