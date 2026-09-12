# MediaDrop 后端

该目录包含 Flask 服务、网页界面和后端自动化测试。完整的安装与使用说明请查看仓库根目录的 [README](../README.md)。

## 本地运行

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

浏览器访问 `http://127.0.0.1:8899`。桌面开发建议在仓库根目录执行 `npm start`，由 Electron 自动选择端口并启动服务。

## 数据目录

- `config.json`：下载位置、代理、Cookie 方式和并发数
- `jobs.sqlite3`：任务历史与可恢复状态
- `server.lock`：后端运行期间的数据目录锁，退出时释放，不需要手动删除
- `.mediadrop-cache/`：未完成任务的分段文件
- `tools/`：应用内校验更新的 yt-dlp

这些运行数据不会被打入安装包。完成文件会先在任务缓存内写完，再以原子方式发布到下载目录。

## 测试

在仓库根目录执行：

```bash
npm test
```

测试覆盖格式状态与异步响应隔离、ETA、缓存生命周期、任务恢复及取消竞争、数据目录锁、API 鉴权、并发配置、封装约束、诊断脱敏和 yt-dlp 版本选择与校验更新。

## HDR 类型显示

YouTube 的提取器可能把 HLG 和 HDR10+ 都标成 HDR10。解析时此类未验证格式统一显示 HDR；下载完成后检查本地文件开头最多 48 个视频包，在完成卡片和任务历史显示检测到的 HLG、HDR10+ 或 Dolby Vision。仅检测到 PQ 时显示 HDR (PQ)，短片段检测不推断整片没有动态元数据。

每个 FFprobe 检查限时 8 秒，优先使用内置工具；AV1 解码失败时尝试 PATH 中的 FFprobe。缺少可用解码器或检测失败不会使下载失败。AV1 的 HDR10+ 检测需要支持 AV1 软件解码的 FFprobe；只有色彩信息时保守显示 HDR (PQ)。这项修改不包含 SABR 实验引擎的安装。


### macOS 界面与系统集成

主界面采用侧边栏、统一任务列表与详情面板。新增链接保留已有任务；下载默认采用最高画质，均衡预设明确限制为 1080p。格式、字幕和封装选项集中在所选任务详情中，完成文件显示实测动态范围。

桌面设置使用独立窗口，支持系统／浅色／深色外观与列表密度，并同步系统强调色、对比度和减少动态效果。`⌘N` 新建下载、`⌘,` 打开设置、`⌘W` 关闭窗口；任务列表支持方向键选择，空格预览已完成文件。文件还可在 Finder 中显示、交给默认应用打开或拖出。

Mac 关闭主窗口只隐藏窗口，任务继续运行；退出应用时若仍有活动任务会提示。主进程独立更新 Dock 进度、聚合后台完成通知，并在活动下载期间防止空闲休眠，暂停全部任务后释放。主窗口会记忆位置，并在显示器布局变化后恢复到可见区域。原生 Liquid Glass 暂未接入。

列表保留最近 200 条历史和全部活动任务。Quick Look 的可播放格式取决于 macOS；不支持的文件可交给默认播放器。未开始下载的解析结果仅保存在当前窗口会话。HDR 标签核验不代表播放器或显示设备具备对应 HDR 输出能力。


### 界面细化（2026-09-12）

依据 Apple HIG 的 [macOS 字体排印](https://developer.apple.com/cn/design/human-interface-guidelines/typography)、[按钮](https://developer.apple.com/design/human-interface-guidelines/buttons)与[侧边栏](https://developer.apple.com/design/human-interface-guidelines/sidebars)建议，统一为 13px 桌面正文、紧凑系统风格控件和一致的矢量图标。工具栏与侧栏分界对齐，原生窗口控制按钮在 54px 工具栏内垂直居中。弹窗采用对齐标签、单一默认操作与较轻的遮罩；画质、动态范围改用选择菜单。原生 Liquid Glass 继续暂缓。

已取消的任务不作为错误展示；非视频任务不显示动态范围字段。菜单支持切换侧边栏（⌘⌥S）及任务信息（⌘⌥I）。浅色、深色、长标题、空列表、取消任务、下载中及设置界面均做了渲染检查；完整下载交互验证采用隔离模拟任务。

### Bundled YouTube enhanced engine (2026-09-12)

Desktop YouTube parsing and downloads now share a pinned experimental SABR engine
(yt-dlp PR #13515) and bgutil 2.0.0. Settings → Network → YouTube offers enhanced
(default) and standard modes. Parsed cards preserve the engine in download history.
The provider starts lazily using Electron's bundled Node runtime on a random
loopback port, is reused across requests, and exits with the backend. Provider
output is discarded so generated authentication data is not written to app logs.
`bash scripts/prepare-youtube.sh` assembles the components; the engine checksum is
pinned and upstream replacement fails the build until reviewed. No external Node,
Deno, or separately running token service is needed in the installed app.

Validation: original Vqwcgf0xBeQ parses at 2160p60; the actual download command
retrieved ~35 MB of AV1 video plus Opus audio. A 5-second remuxed sample measures
3840×2160 at 60000/1001 fps. This is a stream/sample verification, not a full 8.2 GB
movie download. Standard yt-dlp's update button does not replace the pinned enhanced
engine; updating enhanced components requires a new application build.

### Native Liquid Glass

macOS 26+ uses a small Node-API bridge to public AppKit `NSGlassEffectView`.
`npm run build:glass` requires the macOS 26 SDK and builds the native resource;
`dist:mac` includes this step automatically. The standard material is visible in
navigation and toolbar areas, while download input, task content, and details
remain opaque. No private glass variants or CSS blur emulation are used.

Native resources load only on supported macOS versions. Missing resources,
reduced transparency, and increased contrast retain the opaque interface.
Theme changes update the native material and web content together; the native
view is owned by the window and cannot intercept clicks. The renderer can only
request a theme for its own main-frame window, never provide native pointers.

### Download workspace refinement

The main window defaults to a sidebar and rounded download workspace. Details
open on demand from each task's information button (Escape closes them), with an
overlay on narrower windows. Ready tasks expose Download in the row; active and
finished tasks expose state-appropriate actions. The persistent link composer
retains multiline input and Command-Enter, grows up to a bounded height, and
continues to preserve the parsed YouTube engine for downloads. Structural divider
lines and the fixed footer are removed; task status stays alongside its content.

Validated with isolated Electron tests for download request dispatch, engine
preservation, detail opening/closing, light/dark themes, and narrow-window layout.

### Settings window polish

Settings now use an inset macOS title bar with explicitly visible native window
buttons, an accessible page close button, and a rounded scrolling content surface.
Preference categories include concise headings and descriptions; input rows share
consistent spacing and rounded backgrounds. Close, Escape, and Command-W close
only the settings window through a sender-checked IPC path. Isolated desktop tests
cover every category in light/dark mode, the minimum window size, all three close
paths, and continued backend availability after settings closes.
