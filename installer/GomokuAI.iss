; 五子棋AI —— Windows 安装包（Inno Setup 6）
;
; 由 .github/workflows/build.yml 的 windows job 调用（**只在手动 dispatch 时**，
; 打 tag 不触发 —— Windows 侧未经验证，不进 Release），形如：
;     ISCC.exe /DAppVersion=3.0.3 /DHasChinese=1 installer\GomokuAI.iss
;
; **为什么用 Inno 而不是继续发 7z。** 旧的 7z 包里是一个 install.bat，靠
; xcopy 拷文件 + PowerShell 建快捷方式。它有三个绕不过去的毛病：没有安装
; 向导（用户双击一个 .bat，看到一片黑窗口）、没有卸载项（"添加或删除程序"
; 里查无此物，卸载要靠另一个 .bat）、失败时只能 `pause` 让人自己看。这些
; 都是"安装程序"该做的事，不该由批处理假装。
;
; 打包内容与旧的 7z 完全一致：`dist\GomokuAI\`（PyInstaller onedir）。
; onedir 而不是 onefile：onefile 每次启动都要把上百 MB 解包到临时目录，
; PyQt5 程序的首启动会明显变慢。免安装的单文件版本由 CI 另外单独产出。
;
; **本文件必须存为带 BOM 的 UTF-8** —— 里面有中文（应用名、图标路径）。
; 没有 BOM 时 ISCC 会按 ANSI 解析，中文变成乱码。

#ifndef AppVersion
; 直接手动编译时（不传 /D）的兜底，正式构建一律由 CI 传入 tag 号。
#define AppVersion "0.0.0"
#endif

#define AppName "五子棋AI"
#define AppExeName "GomokuAI.exe"
#define AppPublisher "iamlinxuhan"

[Setup]
; AppId 是"这是同一个程序"的身份标识，**跨版本必须一字不改** —— 改了之后
; 新版本会被当成另一个程序装上，旧版本留在"添加或删除程序"里下不掉。
AppId={{226bf4a9-ac9a-40cb-a0bb-39cdafa8a9f5}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\GomokuAI
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; 装进 %LOCALAPPDATA% 而非 Program Files：不需要管理员权限（不弹 UAC），
; 且与旧的 install.bat 行为一致 —— 那里写的也是 %LOCALAPPDATA%\GomokuAI。
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=GomokuAI_Setup_v{#AppVersion}
SetupIconFile=..\五子棋.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
; 中文界面**只在编译器自带那份 .isl 存在时**才挂上：Inno Setup 从 6.3 起才
; 官方收录简体中文，更早的版本里没有这个文件，而 `MessagesFile` 指向不存在的
; 文件是**编译期硬错**。由 CI 探测后传 /DHasChinese=1 决定走哪条分支 ——
; 用编译器自带的那份（而不是把 .isl 收进仓库）就不会有版本错配：语言文件与
; 编译器永远同源。
#ifdef HasChinese
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif
Name: "english"; MessagesFile: "compiler:Default.isl"
; 英文那份永远挂上：万一中文语言文件缺失，至少还有一个可用界面，
; 不至于因为一条 #ifdef 判错就连装都装不了。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; recursesubdirs：PyInstaller onedir 的 _internal 目录里全是子目录。
Source: "..\dist\GomokuAI\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
