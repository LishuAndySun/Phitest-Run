# PhiTest Web Training

PhiTest Web Training 是一个基于 FastAPI 和 SQLite 的个人训练记录网页。它与命令行程序 `PhiTest.py` 并行存在：Web 版使用自己的 `phitest.db`，不会自动读取或修改 CLI 的 JSONL 数据。

主要功能：维护谱面库、创建 Train 或 Test 场次、逐首保存成绩、查看单曲历史，以及按场次分组和筛选整组趋势。

## 环境要求

- Python 3.11 或更新版本
- Windows、macOS 或 Linux
- 同一局域网访问时，运行服务的设备和客户端设备需互通

## 安装

在项目目录打开终端，创建并启用虚拟环境，然后安装依赖。

Windows PowerShell：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 账户与登录

启动服务后打开首页，选择“注册”创建账户。用户名以明文保存，密码使用每个账户独立随机盐的 SHA-256 摘要保存；登录还需要 1Password 等兼容 TOTP 的 Authenticator 每 30 秒生成的 6 位代码。

首次注册会显示 TOTP 密钥和 `otpauth://` 配置链接。使用 Authenticator 扫描/打开链接，或手动输入密钥添加 PhiTest，然后用动态验证码登录。请将密钥保存在安全位置；忘记密码时必须提供该账户的 authenticator 验证码才能设置新密码。丢失 authenticator 密钥目前没有管理员恢复入口。

多个账户共用谱面曲库，但各自的训练、Test、成绩和分析数据相互隔离。既有数据库中的历史场次会在首个账户创建时归到该账户。

## 启动服务

在项目目录、且已启用虚拟环境的终端运行：

```bash
python server.py
```

服务监听所有网络接口的 `3249` 端口。也可以显式使用 Uvicorn 启动：

```bash
uvicorn server:app --host 0.0.0.0 --port 3249
```

本机访问：<http://127.0.0.1:3249>

在同一局域网中，其他设备可访问：`http://<运行服务设备的局域网 IPv4 地址>:3249`。网页顶部的“访问地址”区域会列出服务检测到的本机 IPv4 URL。若地址无法访问，请检查操作系统防火墙、Wi-Fi 客户端隔离和网络路由设置。

首次访问时在首页注册或登录；登录会话存储在 HttpOnly cookie 中。

## 使用流程

1. 在“添加谱面”中录入曲名、难度、等级和定数。
2. 创建 Train 或 Test，选择本场谱面。一个场次最多包含 10,000 条谱面记录；同一谱面可以在同一场次重复加入。
3. 在场次列表中点击“打开并录入”。每首成绩在页面中单独输入并保存。重新打开场次时，已保存的值会自动显示；尚未保存的输入不会写入数据库。
4. Train 输入 0～1,000,000 的整数分数。Test 每首输入 `N`、`Perfect`、`Good`、`Bad`、`Miss` 和整局 `MaxCombo`。系统校验 `N = P + G + B + M`，按 `ACC = (P + 0.65G) / N × 100` 和 `Score = 900000(P + 0.65G) / N + 100000MaxCombo / N` 计算；Test 至少包含 3 个不同曲名才会标记为正式结果。
5. 在“单曲历史趋势”中选择谱面查看该谱面各次已保存成绩。
6. “整组成绩趋势”为每个场次绘制单独曲线，每个点表示该场的一次游玩。可以勾选场次筛选要对比的曲线；图表支持点位提示、拖动、缩放和重置视图。
7. “数据备份”可以导出当前账户的谱面引用、场次和成绩为 `phitest-export.json`，也可以导入该文件恢复数据。导入会追加场次，不删除已有数据；导入文件必须来自 PhiTest Web 版的 v1 导出格式。

## 数据与备份

Web 数据库位于项目目录下的 `phitest.db`。SQLite 使用 WAL 模式；服务运行时可能同时出现 `phitest.db-wal` 和 `phitest.db-shm` 文件。备份前建议先停止服务，再复制数据库文件；不要在服务仍运行时只复制主数据库文件。

主要数据表：

- `songs`：曲名
- `charts`：谱面及难度、等级和定数
- `sessions`：Train/Test 场次
- `session_charts`：场次中的谱面条目、顺序和重复项
- `records`：每个场次谱面条目的已保存成绩和游玩时间

重复加入的谱面会作为不同场次条目保存，各自拥有独立成绩。再次保存同一条目会更新该条成绩；不同场次之间的历史记录互不覆盖。

## 安全注意事项

- 服务使用 HTTP，HTTP Basic 凭据在传输层没有加密。不要在不可信网络上直接暴露服务。
- 不要把 `3249` 端口直接映射到公网。远程访问建议使用可信的私有 VPN；更广泛部署时应在反向代理上启用 HTTPS，并配置访问控制。
- 运行服务的电脑应使用防火墙限制可访问网络，并定期备份数据库。
- 登录会话通过 HttpOnly cookie 保存；HTTP 部署时凭据和 TOTP 代码仍未加密传输。不要在不可信网络上暴露服务。
- SQLite 中保存密码摘要和 TOTP 密钥。数据库文件应视为敏感数据并做好访问控制与备份。

## 与命令行版的关系

`PhiTest.py` 保持独立。Web 版将数据保存在 `phitest.db`，不会自动导入、同步或转换命令行版的 JSONL 文件。需要跨版本迁移时，请先备份两边数据，再单独设计和验证迁移流程。
