"""Generate source for the bundled, per-user Office cursor loaders.

Run during development; end users receive compiled XLAM/PPAM files in the app.
Only fixed system libraries are declared statically. The owned add-in's actual
directory supplies the native library path, including Unicode user directories.
"""
from pathlib import Path
import sys

COMMON = r'''Option Explicit

#If Mac Then
Private Declare PtrSafe Function DLOpen Lib "/usr/lib/libSystem.B.dylib" Alias "dlopen" (ByVal path As LongPtr, ByVal mode As Long) As LongPtr
Private Declare PtrSafe Function DLSymbol Lib "/usr/lib/libSystem.B.dylib" Alias "dlsym" (ByVal handle As LongPtr, ByVal symbol As LongPtr) As LongPtr
Private Declare PtrSafe Function CopyBytes Lib "/usr/lib/libSystem.B.dylib" Alias "memcpy" (ByVal target As LongPtr, ByVal source As LongPtr, ByVal count As LongPtr) As LongPtr
Private Declare PtrSafe Function PrepareCall Lib "/usr/lib/libffi.dylib" Alias "ffi_prep_cif" (ByVal cif As LongPtr, ByVal abi As Long, ByVal count As Long, ByVal resultType As LongPtr, ByVal args As LongPtr) As Long
Private Declare PtrSafe Sub InvokeCall Lib "/usr/lib/libffi.dylib" Alias "ffi_call" (ByVal cif As LongPtr, ByVal address As LongPtr, ByVal result As LongPtr, ByVal args As LongPtr)
Private Declare PtrSafe Function CFStringMake Lib "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation" Alias "CFStringCreateWithCString" (ByVal allocator As LongPtr, ByVal bytes As LongPtr, ByVal encoding As Long) As LongPtr
Private Declare PtrSafe Function PolicyBoolean Lib "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation" Alias "CFPreferencesGetAppBooleanValue" (ByVal key As LongPtr, ByVal applicationID As LongPtr, ByVal valid As LongPtr) As Byte
Private Declare PtrSafe Sub ReleaseCF Lib "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation" Alias "CFRelease" (ByVal value As LongPtr)
Private Declare PtrSafe Function MakeFileURL Lib "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation" Alias "CFURLCreateWithFileSystemPath" (ByVal allocator As LongPtr, ByVal path As LongPtr, ByVal style As Long, ByVal directory As Byte) As LongPtr
Private Declare PtrSafe Function FileSystemBytes Lib "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation" Alias "CFURLGetFileSystemRepresentation" (ByVal url As LongPtr, ByVal absolute As Byte, ByVal buffer As LongPtr, ByVal capacity As LongPtr) As Byte
#End If

Private NativeHandle As LongPtr
Private FFIHandle As LongPtr
Private Cif(0 To 7) As Currency
Private CifReady As Boolean
Private Started As Boolean

Private Function UTF8(ByVal text As String) As Byte()
    Dim bytes() As Byte
    Dim position As Long, index As Long, code As Long, low As Long
    ReDim bytes(0 To Len(text) * 4)
    index = 1
    Do While index <= Len(text)
        code = AscW(Mid$(text, index, 1)) And &HFFFF&
        If code >= &HD800& And code <= &HDBFF& Then
            If index = Len(text) Then Err.Raise vbObjectError + 470, "Screen Relay", "Invalid Unicode path"
            low = AscW(Mid$(text, index + 1, 1)) And &HFFFF&
            If low < &HDC00& Or low > &HDFFF& Then Err.Raise vbObjectError + 470, "Screen Relay", "Invalid Unicode path"
            code = &H10000 + (code - &HD800&) * &H400& + low - &HDC00&
            index = index + 1
        ElseIf code >= &HDC00& And code <= &HDFFF& Then
            Err.Raise vbObjectError + 470, "Screen Relay", "Invalid Unicode path"
        End If
        If code = 0 Then Err.Raise vbObjectError + 470, "Screen Relay", "Invalid NUL in path"
        If code < &H80& Then
            bytes(position) = code: position = position + 1
        ElseIf code < &H800& Then
            bytes(position) = &HC0 Or (code \ &H40&): position = position + 1
            bytes(position) = &H80 Or (code And &H3F&): position = position + 1
        ElseIf code < &H10000 Then
            bytes(position) = &HE0 Or (code \ &H1000&): position = position + 1
            bytes(position) = &H80 Or ((code \ &H40&) And &H3F&): position = position + 1
            bytes(position) = &H80 Or (code And &H3F&): position = position + 1
        Else
            bytes(position) = &HF0 Or (code \ &H40000): position = position + 1
            bytes(position) = &H80 Or ((code \ &H1000&) And &H3F&): position = position + 1
            bytes(position) = &H80 Or ((code \ &H40&) And &H3F&): position = position + 1
            bytes(position) = &H80 Or (code And &H3F&): position = position + 1
        End If
        index = index + 1
    Loop
    ReDim Preserve bytes(0 To position)
    UTF8 = bytes
End Function

Private Function OpenLibrary(ByVal path As String) As LongPtr
    Dim bytes() As Byte
    bytes = UTF8(path)
    OpenLibrary = DLOpen(VarPtr(bytes(0)), 2)
End Function

Private Function OpenNativeLibrary() As LongPtr
    Dim folder As String, path As String, bytes() As Byte
    Dim pathString As LongPtr, url As LongPtr, valid As Byte
    folder = AddinDirectory()
    If Left$(folder, 1) = "/" Then
        bytes = UTF8(folder & "/libUUNativeCursor.dylib")
        If UBound(bytes) > 8192 Then Err.Raise vbObjectError + 474, "Screen Relay", "Unsupported add-in path"
    ElseIf InStr(folder, ":") > 1 Then
        path = folder & ":libUUNativeCursor.dylib"
        bytes = UTF8(path)
        pathString = CFStringMake(0, VarPtr(bytes(0)), &H8000100)
        If pathString = 0 Then Err.Raise vbObjectError + 474, "Screen Relay", "Unsupported add-in path"
        url = MakeFileURL(0, pathString, 1, 0)
        ReleaseCF pathString
        If url = 0 Then Err.Raise vbObjectError + 474, "Screen Relay", "Unsupported add-in path"
        ReDim bytes(0 To 8192)
        valid = FileSystemBytes(url, 1, VarPtr(bytes(0)), 8193)
        ReleaseCF url
        If valid = 0 Or bytes(0) <> 47 Then Err.Raise vbObjectError + 474, "Screen Relay", "Unsupported add-in path"
    Else
        Err.Raise vbObjectError + 474, "Screen Relay", "Unsupported add-in path"
    End If
    OpenNativeLibrary = DLOpen(VarPtr(bytes(0)), 2)
End Function

Private Function FindSymbol(ByVal handle As LongPtr, ByVal name As String) As LongPtr
    Dim bytes() As Byte
    bytes = UTF8(name)
    FindSymbol = DLSymbol(handle, VarPtr(bytes(0)))
    If FindSymbol = 0 Then Err.Raise vbObjectError + 471, "Screen Relay", "Native cursor symbol unavailable"
End Function

Private Function PolicyBlocks(ByVal domain As String) As Boolean
    Dim keyBytes() As Byte, domainBytes() As Byte
    Dim key As LongPtr, app As LongPtr, valid As Byte
    keyBytes = UTF8("DisableVisualBasicExternalDylibs")
    domainBytes = UTF8(domain)
    key = CFStringMake(0, VarPtr(keyBytes(0)), &H8000100)
    app = CFStringMake(0, VarPtr(domainBytes(0)), &H8000100)
    If key = 0 Or app = 0 Then Err.Raise vbObjectError + 472, "Screen Relay", "Cannot check Office policy"
    PolicyBlocks = PolicyBoolean(key, app, VarPtr(valid)) <> 0
    ReleaseCF app
    ReleaseCF key
End Function

Private Sub PrepareNative()
    Dim result As Long
    If NativeHandle <> 0 And CifReady Then Exit Sub
    If PolicyBlocks("com.microsoft.office") Or PolicyBlocks(HostDomain()) Then
        Err.Raise vbObjectError + 473, "Screen Relay", "Office policy disables external native libraries"
    End If
    FFIHandle = OpenLibrary("/usr/lib/libffi.dylib")
    If FFIHandle = 0 Then Err.Raise vbObjectError + 475, "Screen Relay", "System libffi unavailable"
    result = PrepareCall(VarPtr(Cif(0)), 1, 0, FindSymbol(FFIHandle, "ffi_type_sint32"), 0)
    If result = 2 Then result = PrepareCall(VarPtr(Cif(0)), 2, 0, FindSymbol(FFIHandle, "ffi_type_sint32"), 0)
    If result <> 0 Then Err.Raise vbObjectError + 476, "Screen Relay", "Unsupported native call ABI"
    NativeHandle = OpenNativeLibrary()
    If NativeHandle = 0 Then Err.Raise vbObjectError + 477, "Screen Relay", "Native cursor library could not load"
    CifReady = True
End Sub

Private Function NativeCall(ByVal name As String) As Long
    Dim returned As Currency, result As Long, ignored As LongPtr
    PrepareNative
    InvokeCall VarPtr(Cif(0)), FindSymbol(NativeHandle, name), VarPtr(returned), 0
    ignored = CopyBytes(VarPtr(result), VarPtr(returned), 4)
    NativeCall = result
End Function

Public Sub UUCursorStart()
#If Mac Then
    Dim result As Long
    result = NativeCall("UUOfficeCursorInstall")
    If result <> 0 Then Err.Raise vbObjectError + 478, "Screen Relay", "Native cursor module unavailable: " & CStr(result)
    Started = True
#End If
End Sub

Public Sub UUCursorStop()
#If Mac Then
    If Started Then
        Dim result As Long
        result = NativeCall("UUOfficeCursorStop")
        If result <> 0 Then Err.Raise vbObjectError + 479, "Screen Relay", "Native cursor module did not stop: " & CStr(result)
        Started = False
    End If
#End If
End Sub

Public Sub UUCursorStatus()
#If Mac Then
    MsgBox "Screen Relay cursor state: " & CStr(NativeCall("UUOfficeCursorGetState")), vbInformation, "Screen Relay"
#End If
End Sub

Public Sub Auto_Open()
    UUCursorStart
End Sub

Public Sub Auto_Close()
    UUCursorStop
End Sub
'''


def module(host):
    if host == 'excel':
        name = 'UUDisplayNativeCursor'
        directory = r'''
Private Function HostDomain() As String
    HostDomain = "com.microsoft.Excel"
End Function

Private Function AddinDirectory() As String
    AddinDirectory = ThisWorkbook.Path
End Function

Public Sub Auto_Add()
    UUCursorStart
End Sub

Public Sub Auto_Remove()
    UUCursorStop
End Sub
'''
    elif host == 'powerpoint':
        name = 'UUDisplayPowerPointCursor'
        directory = r'''
Private Function HostDomain() As String
    HostDomain = "com.microsoft.Powerpoint"
End Function

Private Function AddinDirectory() As String
    Dim item As AddIn, found As Long, filename As String
    For Each item In Application.AddIns
        If LCase$(item.Name) = "uudisplaypowerpointcursor.ppam" Or LCase$(item.Name) = "uudisplaypowerpointcursor" Then
            found = found + 1
            filename = item.FullName
        End If
    Next item
    If found <> 1 Then Err.Raise vbObjectError + 480, "Screen Relay", "Enable exactly one Screen Relay PowerPoint add-in"
    Dim separator As String, lastSeparator As Long
    separator = IIf(Left$(filename, 1) = "/", "/", ":")
    lastSeparator = InStrRev(filename, separator)
    If lastSeparator <= 1 Then Err.Raise vbObjectError + 481, "Screen Relay", "Unsupported add-in location"
    AddinDirectory = Left$(filename, lastSeparator - 1)
End Function
'''
    else:
        raise ValueError(host)
    return f'Attribute VB_Name = "{name}"\n' + COMMON + directory


def prepare(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for host in ('excel', 'powerpoint'):
        name = 'UUDisplayNativeCursor' if host == 'excel' else 'UUDisplayPowerPointCursor'
        (directory / (name + '.bas')).write_text(module(host), encoding='ascii', newline='\r\n')


if __name__ == '__main__':
    prepare(sys.argv[1])
