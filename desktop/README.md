# LinguaFlow Windows 伴侣

`LinguaFlow.exe` 是 Windows 桌面入口：双击后检查容器健康状态，并在默认浏览器中打开 WebUI。
翻译、离线模型、LibreOffice/PDF 处理都在 Docker 容器内完成，因此 Windows 端无需安装 Python 或 Office。

## 配置服务器地址

首次运行会在 exe 同目录生成 `linguaflow.json`：

```json
{
  "server": "http://192.0.2.10:5463"
}
```

也可以设置环境变量 `LINGUAFLOW_URL`，或运行：

```bat
LinguaFlow.exe -server http://192.0.2.10:5463
```

`LinguaFlow.exe -status` 只检查连接，不打开浏览器。

## 重新构建

在 Linux/macOS 上安装 Go 1.22+ 后执行：

```bash
./build.sh
```

产物为 `exe/LinguaFlow.exe`（Windows x64，无 CGO 依赖）。
