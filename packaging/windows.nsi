Unicode true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

Name "Share Monkey"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Share Monkey"
InstallDirRegKey HKCU "Software\Share Monkey" "InstallDir"
RequestExecutionLevel user
ManifestDPIAware true
SetCompressor /SOLID lzma
SetCompressorDictSize 32
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "Share Monkey"
VIAddVersionKey /LANG=2052 "FileDescription" "Share Monkey 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Share Monkey contributors"

!define MUI_ICON "${PAYLOAD}\monkey.ico"
!define MUI_UNICON "${PAYLOAD}\monkey.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\bin\Share Monkey.exe"
!define MUI_FINISHPAGE_RUN_TEXT "启动 Share Monkey"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Share Monkey 需要 64 位 Windows。"
    Abort
  ${EndIf}
  FindWindow $0 "" "Share Monkey"
  ${If} $0 != 0
    MessageBox MB_ICONINFORMATION "请先从任务栏通知区退出 Share Monkey，再安装更新。"
    Abort
  ${EndIf}
FunctionEnd

Section "Share Monkey"
  SetShellVarContext current
  SetRegView 64
  SetOutPath "$INSTDIR\Python"
  File /r "${PAYLOAD}\Python\*.*"
  SetOutPath "$INSTDIR\pkgs"
  File /r "${PAYLOAD}\pkgs\*.*"
  SetOutPath "$INSTDIR\bin"
  File /r "${PAYLOAD}\bin\*.*"
  SetOutPath "$INSTDIR\licenses"
  File /r "${PAYLOAD}\licenses\*.*"
  SetOutPath "$INSTDIR"
  File "${PAYLOAD}\monkey.ico"
  File "${PAYLOAD}\THIRD_PARTY_NOTICES.md"
  File "${PAYLOAD}\payload-manifest.json"
  CreateShortcut "$SMPROGRAMS\Share Monkey.lnk" "$INSTDIR\bin\Share Monkey.exe" "" "$INSTDIR\monkey.ico"
  CreateShortcut "$DESKTOP\Share Monkey.lnk" "$INSTDIR\bin\Share Monkey.exe" "" "$INSTDIR\monkey.ico"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Share Monkey" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "DisplayName" "Share Monkey"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "DisplayIcon" "$INSTDIR\monkey.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey" "NoRepair" 1
SectionEnd

Function un.onInit
  FindWindow $0 "" "Share Monkey"
  ${If} $0 != 0
    MessageBox MB_ICONINFORMATION "请先从任务栏通知区退出 Share Monkey，再卸载。"
    Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  SetRegView 64
  Delete "$SMPROGRAMS\Share Monkey.lnk"
  Delete "$DESKTOP\Share Monkey.lnk"
  RMDir /r "$INSTDIR\Python"
  RMDir /r "$INSTDIR\pkgs"
  RMDir /r "$INSTDIR\bin"
  RMDir /r "$INSTDIR\licenses"
  Delete "$INSTDIR\monkey.ico"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\payload-manifest.json"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Share Monkey"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Share Monkey"
SectionEnd
