<p align="center">  
  <img src="logo.png" width="120" alt="logo">
</p>

<h1 align="center">astrbot_plugin_slave_market</h1>

由 Yunzai 插件 [Slave-Market](https://gitee.com/Tloml-Starry/Slave-Market) 移植的 AstrBot 群互动经营游戏「奴隶市场」。

购买群友当奴隶、让奴隶打工赚金币、训练/决斗/排位赛抬身价、银行存取款吃利息、抢劫与赎身。SQLite 单文件存储，全部输出经独立 Playwright 渲染器以 HTML 模板出图，附独立端口 WebUI 管理面板。

---

## 安装方式：WebUI 插件市场

AstrBot WebUI → 插件管理 → 搜索 `astrbot_plugin_slave_market` → 安装。

---

## 架构

```
astrbot_plugin_slave_market/
├── main.py               # 插件生命周期 + 渲染输出管线，指令通过声明式路由安装
├── handlers/             # 指令路由表（按业务域拆分，Route 声明式注册）
│   ├── base.py           #   Route/install 基座 + 事件解析辅助 + 每用户指令锁
│   ├── market_cmds.py    #   购买/放生/我的奴隶/市场/排行榜
│   ├── social_cmds.py    #   打工/抢劫/赎身
│   ├── battle_cmds.py    #   训练/决斗/排位赛
│   ├── bank_cmds.py      #   银行/转账
│   └── system_cmds.py    #   帮助/数据备份（管理员）
├── core/
│   ├── context.py        #   GameCtx 共享上下文（服务/数据库/渲染器/平台昵称拉取）
│   ├── db.py             #   SQLite 存储层（WAL + VACUUM INTO 备份 + trash 回收站）
│   ├── service.py        #   游戏逻辑（返回统一 R 结构：tmpl+data+text 回退）
│   ├── result.py         #   R 结果封装
│   ├── texts.py          #   长文本加载器（resources/texts/*.json，可热更新）
│   └── renderer.py       #   独立 Playwright 渲染器（懒启动/串行截图/自动清理）
├── webui/                # 独立端口管理面板（aiohttp + Argon2id + JWT 会话 + 全局错误中间件）
│   ├── server.py         #   全量管理 API
│   └── index.html / style.css / app.js
└── resources/
    ├── data/workCopywriting.json   # 游戏文案（WebUI 可在线编辑并热更新）
    ├── texts/help.json             # 帮助长文本（WebUI 可在线编辑并热更新）
    └── templates/*.html            # 消息图片 Jinja2 模板
```

新增一条指令的步骤：在 `core/service.py` 写服务函数返回 `R`，在 `handlers/` 对应域文件写 `async run(ctx, event)` 并追加一条 `Route(pattern, name, doc, run)`。

---

### 卡片渲染环境安装教程（可选，不影响文字回复）

卡片渲染基于本地 Playwright 截图实现。出于安全考虑，插件**绝不会**自动执行任何系统级安装——不修改 apt 源、不运行 apt-get、不自动 pip 装包、不自动下载浏览器内核。需要图片卡片时请按下面步骤手动安装（约 1~2 分钟）：

#### ① 安装 playwright Python 包

```bash
pip install playwright
# 国内网络可用清华镜像：
pip install playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
```

#### ② 下载 Chromium 浏览器内核

```bash
python -m playwright install chromium
# 国内网络可用 npmmirror 加速：
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ python -m playwright install chromium
```

Windows PowerShell 写法：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright/"
python -m playwright install chromium
```

#### ③（仅 Linux / Docker 容器）安装系统运行库

仅当启动渲染时报 `libnspr4` / `libnss3` / `error while loading shared libraries` 才需要，需 root：

```bash
python -m playwright install-deps chromium
```

或手动安装系统库：

```bash
apt-get update && apt-get install -y \
  libnspr4 libnss3 libgbm1 libasound2 \
  libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 libdrm2 \
  libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxfixes3 \
  libxkbcommon0 libxrandr2 libxext6 libpango-1.0-0
```

容器内 apt 官方源下载慢？可选换阿里镜像源后再装：

```bash
# Debian 12 (bookworm)
sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
# Ubuntu 22.04
sed -i 's|archive.ubuntu.com|mirrors.aliyun.com|g' /etc/apt/sources.list
apt-get update
```

#### ④ 重载插件

WebUI → 插件管理 → 本插件 → 重载。

环境未就绪时，日志会输出一次完整指引，所有指令自动回退纯文本展示；安装完成后重载即可正常出图。

---

## 指令一览

| 分类 | 指令 |
|---|---|
| 帮助 | 奴隶帮助 / 奴隶菜单 / 群友帮助 / 群友菜单 |
| 市场 | 购买奴隶 @群友（或 QQ 号）、奴隶市场、奴隶身价排行榜、奴隶资金排行榜 |
| 个人 | 我的奴隶、打工（一键打工）、赎身、抢劫 @群友 |
| 奴隶 | 放生奴隶 @奴隶、训练 @奴隶、一键训练 |
| 战斗 | 决斗 @奴隶1 @奴隶2、排位赛、参加排位赛 @奴隶 |
| 银行 | 存款 金额、一键存款、取款 金额、银行信息、领取利息、升级信用、一键升级信用、转账 金额 @群友 |
| 维护 | 奴隶备份、奴隶备份列表、奴隶恢复备份 序号、奴隶删除备份 序号（均为管理员） |

前缀必须携带，`！`/`!` 均可。

---

## WebUI 管理面板

默认 `http://127.0.0.1:17818`，可在插件配置中改端口/监听地址/密码（非本机监听必须设置密码，否则拒绝启动）。功能：

- **总览**：玩家/群/金币/银行/奴隶统计
- **排行榜**：金币/身价/奴隶/银行四类，按群切换
- **市场**：全群身价一览
- **玩家**：分页列表、按 ID/昵称搜索、在线编辑（金币/身价/主人/银行）、删除存档（进回收站）
- **备份**：创建/恢复/删除全量备份（`VACUUM INTO` 一致性快照）
- **文案**：在线编辑打工文案与帮助长文本并热更新（无需重载插件）；帮助文案为可视化编辑——标题、分栏卡片、条目增删与排序都是表单操作，不用手写 JSON，纯文本兜底可按分栏一键生成
- **配置**：面板内直接修改插件配置（含嵌套配置组，密码留空保持原值）

---

## 玩法要点

- **购买**：价格为对方当前身价，成交后对方 +20 身价并获得等额金币；购买他人奴隶时原主人收回价款。
- **打工**：没有奴隶自己打工；有奴隶则让全部奴隶打工（10% 概率摸鱼掉身价），20% 概率触发意外支出。
- **训练**：花奴隶身价的 10% 训练，成功则身价 +20%；同一奴隶 2 小时 CD。
- **决斗/排位**：身价决定胜率，Elo 计分，段位从青铜到钻石。
- **银行**：存款上限随信用等级提升，每小时 1% 利息（最多计 24 小时）。

---

##  许可证

本项目采用原项目 [木兰宽松许可证 第2版](LICENSE) 开源。

---

## 致谢

- [Slave-Market（原插件）](https://gitee.com/Tloml-Starry/Slave-Market) — 群友之间的增温小游戏，购买群友来替你打工，然后买下更多群友（doge
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 多平台聊天机器人框架

---

<div align="center">

如果觉得这个插件对你带来快乐，欢迎 Star 或者 PR 一下哈哈

</div>
