# releases 分支：只放安装包

这个分支和源码没有任何关系（孤儿分支），只用来提供**不带重定向的直链**——
Microsoft Store 的提交页不接受会 302 的地址，而 Release 资产正好是 302。

当前文件：`GomokuAI_Windows_amd_x86_64_setup.exe`（4.3.0）。

```
https://raw.githubusercontent.com/iamlinxuhan/GomokuAI/releases/GomokuAI_Windows_amd_x86_64_setup.exe
```

发版后把同名文件替换、提交、推上来，链接不变。
源码与更新日志在 `main`。
