# Windows Task Scheduler fallback for the book recorder (P-10). Used in M4 only if the
# systemd unit cannot be used. The shared "Start WSL at boot" task already starts WSL;
# this script only documents the fallback and changes nothing until M4.
Write-Output "register_recorder_task.ps1: fallback is set up in M4; nothing to do yet."
