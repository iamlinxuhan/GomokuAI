# releases 分支：只放安装包

这个分支和源码没有任何关系（孤儿分支），只用来提供**不带重定向的直链**——
Microsoft Store 的提交页不接受会 302 的地址，而 Release 资产正好是 302。

**每个版本一个目录，链接里带版本号。** 商店把这里的 URL 当成不可变资源：
指向 `latest`、或者"同名文件原地替换、链接不变"，校验都会失败。

```
https://iamlinxuhan.github.io/GomokuAI/v4.3.0/GomokuAI_Windows_amd_x86_64_setup.exe
```

GitHub Pages 从本分支的根目录提供服务。备用地址（同一个文件，同样不重定向）：

```
https://raw.githubusercontent.com/iamlinxuhan/GomokuAI/releases/v4.3.0/GomokuAI_Windows_amd_x86_64_setup.exe
```

**发新版时新建一个目录**（`v4.4.0/` …），旧目录原样留着不动——商店里已经
提交的链接必须一直有效。

源码与更新日志在 `main`。
