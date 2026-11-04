"""Best-effort desktop alert as a Windows toast. Never raises: an alert must not break a cycle."""
from __future__ import annotations

import os
import subprocess

# Toast through Windows PowerShell's own app id so Windows 10/11 shows it (and keeps it in the Action Center
# even when Focus Assist hides the banner). Text is passed in the environment, then XML-escaped: no quoting or injection.
_PS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
    "$t = [System.Security.SecurityElement]::Escape($env:ALERT_TITLE); $m = [System.Security.SecurityElement]::Escape($env:ALERT_TEXT); "
    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    "$x.LoadXml(\"<toast scenario='reminder'><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text></binding></visual>"
    "<audio src='ms-winsoundevent:Notification.Reminder'/></toast>\"); "
    "$n = [Windows.UI.Notifications.ToastNotification]::new($x); "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($n)"
)


def notify(title: str, text: str) -> bool:
    """Show a toast in the signed-in user's session. Returns True if it was launched."""
    if os.name != "nt":
        return False
    try:
        env = {**os.environ, "ALERT_TITLE": title[:60], "ALERT_TEXT": text[:250]}
        subprocess.Popen(["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _PS], env=env,
                         creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # CREATE_NO_WINDOW
        return True
    except Exception:
        return False
