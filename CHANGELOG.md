# Changelog

## V1.0.2

### Breaking Changes
- **桌面应用不再依赖系统 Python** — Flask 后端通过 PyInstaller 编译为独立可执行文件，用户无需安装 Python 3

### New Features
- **开箱即用** — 安装包内含 Python 运行时、yt-dlp、ffmpeg，下载后直接使用，无需额外安装任何依赖
- **代理可达性检测** — 配置的代理不可达时自动降级直连，避免下载失败

### Improvements
- 移除启动时对系统 Python / yt-dlp / ffmpeg 的依赖检查（已打包）
- 错误对话框改为提示"请重新安装"（打包模式下）
- `.gitignore` 新增构建产物目录

### Technical
- `app.py` 新增 frozen 模式路径解析（`get_base_dir` / `get_resource_dir` / `get_ytdlp_path` / `get_ffmpeg_dir`）
- `main.js` 支持检测并使用打包后的二进制（`mediadrop-server` / `yt-dlp` / `ffmpeg`）
- 新增 PyInstaller spec 文件 `app/mediadrop-server.spec`
- CI 新增 Python + PyInstaller 构建步骤，自动下载 yt-dlp 和 ffmpeg 二进制
- 用户数据（config.json、下载文件）存于 Electron userData 目录，可写且持久

## V1.0.1

### New Features
- **端口自适应** — 默认端口 8899 被占用时自动切换到下一个可用端口
- **智能代理** — 自动检测系统代理，支持手动配置，代理不可达时自动降级直连
- **代理自动检测按钮** — 一键检测当前系统代理并设置
- **标题命名** — 下载文件以视频标题命名，重复下载自动加序号

### Improvements
- 移除启动时暴力杀死占用端口进程的行为
- 设置面板新增代理配置区域
- 支持 GitHub releases/latest 链接始终指向最新版本

### Bug Fixes
- Windows ARM64 安装包文件名冲突（CI 构建产物互相覆盖）

## V1.0.0

- 初始发布版本
- 支持 YouTube、TikTok、Instagram 等 1000+ 网站下载
- MP4 / MP3 / JPG 多格式输出
- 批量下载、暂停/恢复/取消、画质选择
- macOS DMG / Windows EXE 安装包
- Web 版 + Docker 支持
