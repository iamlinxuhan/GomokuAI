; 五子棋AI —— Windows 安装包（Inno Setup 6）
;
; 由 .github/workflows/build.yml 的 windows job 调用（任意 **tag 推送**，或
; 手动 dispatch 时勾选 `build_windows`），形如：
;     ISCC.exe /DAppVersion=3.0.5 /DHasChinese=1 installer\GomokuAI.iss
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
DefaultDirName={autopf}\GomokuAI
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; **全机安装**（Program Files + HKLM 卸载项），不是每用户装的 %LOCALAPPDATA% 那份。
;
; 2026-11-01 改的，直接原因是 Microsoft Store 的包检查：那套检查跑安装程序时
; 用的不是当前用户上下文，`PrivilegesRequired=lowest` 把卸载项写进 HKCU 之后，
; 检查侧读不到，于是「添加或删除程序中的条目」和「捆绑软件检查」两项都只能
; 报"无法标识"（问号，不是叉），提交时再进一步报 `PackageSet 无效`。
; HKLM 里的卸载项对任何上下文都可见，这一条从根上消掉。
;
; 代价是要弹 UAC（安装一次，不是每次启动）。应用本身不受影响：设置走
; QSettings（HKCU），安装包装的是 play 变体、一个文件都不往 exe 同目录写
; （见 gamelog.py 顶部那段），所以 Program Files 的只读属性不会碍事。
;
; `{autopf}` 在下面的架构指令下解析为 `C:\Program Files`；两条架构指令同时
; 保证 32 位 Windows 装不上 —— 打出来的 exe 是 64 位的。
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
; 产物名与 Linux 那套逐字同构：GomokuAI_<平台>_<stem>_<setup|run>[_debug|_play]。
; Linux 是 GomokuAI_Linux_amd_x86_64_setup.deb，这里是它的 .exe 版本。
;
; **`setup` 不带 debug/play 后缀**，因为它装出来的那个就是 play（不写日志）：
; 打包用的是 `dist\GomokuAI\`，那里的 `_build_flavor.py` 写的是 `"play"`，
; 见 workflow 的 windows job。想要日志的用户下 `..._run_debug.exe`。
;
; **版本号刻意不进名字**：Linux 的 setup.deb 也没有（`APP_VERSION` 只在
; tag 推送时才是真号码，手动 dispatch 拿到的 ref 是 main，会退化成 0.0.0，
; 写进文件名只会变成一个像是 bug 的 `v0.0.0`）。版本仍在安装程序的
; AppVersion 里，卸载项与"添加或删除程序"都看得到。
OutputBaseFilename=GomokuAI_Windows_amd_x86_64_setup
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
