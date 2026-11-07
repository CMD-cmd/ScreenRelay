"""Prepare reviewable native add-in sources; does not install or load Office code."""
import hashlib
import json
from pathlib import Path
import sys

out=Path(sys.argv[1])
library=Path.home()/'Library/Application Support/Microsoft/UUDisplayAutomation/libUUNativeCursor.dylib'
source=f'''Attribute VB_Name = "UUDisplayNativeCursor"
Option Explicit

' UUDisplayAutomation local native cursor add-in.
' Only cursor rendering is handled. No workbook content is accessed.
#If Mac Then
Private Declare PtrSafe Function NativeInstall Lib "{library}" Alias "UUOfficeCursorInstall" () As Long
Private Declare PtrSafe Function NativeStop Lib "{library}" Alias "UUOfficeCursorStop" () As Long
#End If

Private Installed As Boolean

Public Sub UUCursorStart()
#If Mac Then
    Dim result As Long
    result = NativeInstall()
    If result <> 0 Then
        Err.Raise vbObjectError + 467, "UUDisplayAutomation", "Native cursor module unavailable: " & CStr(result)
    End If
    Installed = True
#End If
End Sub

Public Sub UUCursorStop()
#If Mac Then
    If Installed Then
        Dim result As Long
        result = NativeStop()
        If result <> 0 Then
            Err.Raise vbObjectError + 468, "UUDisplayAutomation", "Native cursor module did not unload: " & CStr(result)
        End If
        Installed = False
    End If
#End If
End Sub

Public Sub Auto_Open()
    UUCursorStart
End Sub

Public Sub Auto_Add()
    UUCursorStart
End Sub

Public Sub Auto_Close()
    UUCursorStop
End Sub

Public Sub Auto_Remove()
    UUCursorStop
End Sub
'''
(out/'UUDisplayNativeCursor.bas').write_text(source,encoding='ascii')
library_hash=hashlib.sha256((out/'libUUNativeCursor.dylib').read_bytes()).hexdigest()
# A separate immutable path leaves an already loaded Excel library untouched.
powerpoint_library=library.parent/'PowerPoint'/f'libUUPowerPointCursor-{library_hash[:12]}.dylib'
powerpoint_source=f'''Attribute VB_Name = "UUDisplayPowerPointCursor"
Option Explicit

' Screen Relay local PowerPoint cursor add-in.
' Only cursor rendering is handled; no presentation content is accessed.
#If Mac Then
Private Declare PtrSafe Function NativeInstall Lib "{powerpoint_library}" Alias "UUOfficeCursorInstall" () As Long
Private Declare PtrSafe Function NativeStop Lib "{powerpoint_library}" Alias "UUOfficeCursorStop" () As Long
Private Declare PtrSafe Function NativeState Lib "{powerpoint_library}" Alias "UUOfficeCursorGetState" () As Long
#End If

Private Installed As Boolean

Public Sub UUCursorStart()
#If Mac Then
    Dim result As Long
    result = NativeInstall()
    If result <> 0 Then
        Err.Raise vbObjectError + 467, "Screen Relay", "Native cursor module unavailable: " & CStr(result)
    End If
    Installed = True
#End If
End Sub

Public Sub UUCursorStop()
#If Mac Then
    If Installed Then
        Dim result As Long
        result = NativeStop()
        If result <> 0 Then
            Err.Raise vbObjectError + 468, "Screen Relay", "Native cursor module did not unload: " & CStr(result)
        End If
        Installed = False
    End If
#End If
End Sub

Public Sub UUCursorStatus()
#If Mac Then
    MsgBox "Screen Relay cursor state: " & CStr(NativeState()), vbInformation, "Screen Relay"
#End If
End Sub

Public Sub Auto_Open()
    UUCursorStart
End Sub

Public Sub Auto_Close()
    UUCursorStop
End Sub
'''
(out/'UUDisplayPowerPointCursor.bas').write_text(powerpoint_source,encoding='ascii',newline='\r\n')
manifest={'status':'prepared_not_loaded_in_excel','library_destination':str(library),
          'compiled_library_sha256':library_hash,
          'scope':'Excel bitmap image cursors; process-local pre-display scaling',
          'source':'UUDisplayAutomation local source; no third-party macro project',
          'global_macro_preferences_modified':False,'excel_application_modified':False,
          'hosts':{
              'com.microsoft.Excel':{'module':'UUDisplayNativeCursor.bas','library_destination':str(library)},
              'com.microsoft.Powerpoint':{'module':'UUDisplayPowerPointCursor.bas',
                  'library_destination':str(powerpoint_library),'status':'prepared_not_loaded_in_powerpoint',
                  'add_in_filename':'UUDisplayPowerPointCursor.ppam'}
          }}
(out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
print(json.dumps(manifest,ensure_ascii=False))
