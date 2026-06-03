# Mac Cleanup Checklist

## Remove (when ready)
- [ ] Android Studio + emulator images (~10GB+)
- [ ] Proxyman
- [ ] `brew uninstall android-platform-tools` (adb)
- [ ] `pip3 uninstall frida frida-tools`
- [ ] APK files: `~/Documents/personal_projects/psx-agent/jsglobal_*.apk`
- [ ] Frida files: `~/frida-server.xz`, `~/frida-server`, `~/c8750f0d.0`, `~/mitm.pem`, `~/ssl-bypass.js`
- [ ] Java (if not needed for PDF extraction)

## Keep
- [ ] VS Code / Cursor
- [ ] Python 3.9 (system)
- [ ] pip packages in `requirements.txt`

## Pending work (before cleanup)
- [ ] Capture Trade Cast (`tc.jsglobalonline.com`) API to get reliable order placement
- [ ] Wire broker into `agent.py` for auto-execution
- [ ] Refresh `.AspNetCore.Session` cookie in `profile.yaml` when it expires
