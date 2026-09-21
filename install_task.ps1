# Registers the "LinkedIn Referral Bot" scheduled task for the current user.
# It starts `main.py --workday` with no window at logon, at unlock and at 08:00.
# The bot itself decides whether there is anything to do and exits if not.
#
# Pause:   Disable-ScheduledTask -TaskName "LinkedIn Referral Bot"
# Remove:  Unregister-ScheduledTask -TaskName "LinkedIn Referral Bot" -Confirm:$false

$root = $PSScriptRoot
$me = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\pythonw.exe" `
    -Argument "main.py --workday" -WorkingDirectory $root

$stateChange = Get-CimClass -Namespace ROOT\Microsoft\Windows\TaskScheduler `
    -ClassName MSFT_TaskSessionStateChangeTrigger
$unlock = New-CimInstance -CimClass $stateChange -ClientOnly `
    -Property @{ StateChange = 8; UserId = $me }  # 8 = session unlock

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $me),
    $unlock,
    (New-ScheduledTaskTrigger -Daily -At "08:00")
)

# IgnoreNew: a trigger that fires while the bot is running does nothing.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

# Interactive: Chrome needs the desktop session, so this only runs while logged on.
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "LinkedIn Referral Bot" -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Force
