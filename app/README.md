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
- `.mediadrop-cache/`：未完成任务的分段文件
- `tools/`：应用内校验更新的 yt-dlp

这些运行数据不会被打入安装包。完成文件会先在任务缓存内写完，再以原子方式发布到下载目录。

## 测试

在仓库根目录执行：

```bash
npm test
```

测试覆盖格式状态隔离、ETA、缓存生命周期、任务恢复、API 鉴权、并发配置和 yt-dlp 校验更新。
