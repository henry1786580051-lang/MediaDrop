# Changelog

## V1.0.6 - 2026-07-13

### New Features
- **HDR 自动识别**：区分 SDR、HDR、HDR10、HDR10+、HLG 与 Dolby Vision，可选择自动、仅 HDR 或仅 SDR
- **YouTube 登录支持**：支持浏览器 Cookie 与 `cookies.txt`，提供认证状态测试
- **yt-dlp 在线维护**：可在设置中检查并更新 yt-dlp，本版本内置 `2026.07.04`
- **下载队列**：最多同时运行 2 个任务，支持排队、暂停、继续和取消

### Interface
- **完整汉化**：主界面、设置、下载状态和常见错误提示均已中文化
- **界面重构**：主界面加宽，设置改为分组式右侧抽屉并自动保存
- **格式状态独立**：MP4、MP3、JPG 及不同画质分别记录完成状态，切换后正确显示“下载”或“重新下载”
- **HDR 筛选修复**：选择“仅 HDR”后不再混入 SDR 格式

### Reliability & Security
- **安全缓存**：下载先进入独立任务缓存，成功后再原子发布到目标目录
- **无覆盖提交**：同名和并发完成的文件自动编号，避免覆盖用户文件
- **完整清理**：失败、取消、退出和下次启动时清理孤立缓存及半成品
- **路径保护**：防止目录穿越与缓存根目录符号链接劫持
- **跨磁盘支持**：下载目录位于其他磁盘时安全复制并提交完整文件

### Bug Fixes
- 修复解析后切换 MP4、MP3、JPG 仍下载原选项的问题
- 修复 MP4 完成后切换到 JPG 仍显示“重新下载”的问题
- 修复快速取消任务时过早释放并发名额的问题
- 移除完成后的二次保存传输，避免重复写入已落盘文件

### Tests & Packaging
- 新增格式状态回归测试和 12 项缓存生命周期测试
- GitHub Actions 标签发布改为构建 Windows x64/ARM64 EXE，本地构建 macOS ARM64 DMG
- 发布构建固定 yt-dlp `2026.07.04`，并排除本地配置、缓存和测试文件

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
