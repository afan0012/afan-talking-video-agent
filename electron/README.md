# Electron 桌面壳

Electron 只负责窗口、启动和关闭本地 FastAPI 服务；业务界面仍由 `app/static/`
提供，模型接口也保持不变。

## 开发运行

在项目目录执行：

```powershell
npm install
npm run electron
```

如果系统没有 `python` 命令，可设置：

```powershell
$env:KOUBO_PYTHON = 'D:\path\to\python.exe'   # 换成你自己的 python.exe 完整路径
npm run electron
```

## Windows 打包

先用现有的 `scripts/build_windows.py` 生成 `dist-windows/口播智能体/`，再执行
`npm run package:win`。Electron 安装包会把该后端作为 sidecar 放到安装目录，
用户数据仍写入 `%LOCALAPPDATA%\口播智能体`，不会把 `data/` 或测试素材打进去。
