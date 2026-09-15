<p align="center">  
  <img src="logo.png" width="120" alt="logo">
</p>

<h1 align="center">奴隶市场</h1>

由 Yunzai 插件 [Slave-Market](https://gitee.com/Tloml-Starry/Slave-Market) 移植的 AstrBot 群互动经营游戏「奴隶市场」。

购买群友当奴隶、让奴隶打工赚金币、训练/决斗/排位赛抬身价、银行存取款吃利息、抢劫与赎身。SQLite 单文件存储，全部输出经独立 Playwright 渲染器以 HTML 模板出图，附独立端口 WebUI 管理面板。

**要求 AstrBot >= 4.9.2** | 支持 `aiocqhttp`、`qq_official` 平台

---

## 安装

### 插件安装

AstrBot WebUI → 插件管理 → 搜索 `astrbot_plugin_slave_market` → 安装。

### 卡片渲染环境（可选）

卡片渲染基于本地 Playwright 截图。插件**绝不会**自动执行系统级安装——需要图片卡片时请手动安装（约 1~2 分钟）：

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

Windows PowerShell：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright/"
python -m playwright install chromium
```

#### ③（仅 Linux / Docker）安装系统运行库

仅当启动渲染时报 `libnspr4` / `libnss3` / `error while loading shared libraries` 才需要：

```bash
python -m playwright install-deps chromium
```

或手动安装：

```bash
apt-get update && apt-get install -y \
  libnspr4 libnss3 libgbm1 libasound2 \
  libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 libdrm2 \
  libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 libxfixes3 \
  libxkbcommon0 libxrandr2 libxext6 libpango-1.0-0
```

容器内 apt 官方源下载慢？可选换阿里镜像源后再装：

```bash
# Debian
sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources
# Ubuntu
sed -i 's|archive.ubuntu.com|mirrors.aliyun.com|g' /etc/apt/sources.list
apt-get update
```

#### ④ 重载插件

WebUI → 插件管理 → 本插件 → 重载。

环境未就绪时，首次渲染失败会输出一次完整指引（同一进程内只打一次），所有指令自动回退纯文本展示；安装完成后重载即可正常出图。

---

## 指令一览

前缀必须携带，`！`/`!` 均可。共 29 条路由。

| 分类 | 指令（含别名） | 参数 | 权限 |
|---|---|---|---|
| 帮助 | 奴隶帮助 / 奴隶菜单 / 群友帮助 / 群友菜单 / nl帮助 / nl菜单 | — | 所有人 |
| 市场 | 购买奴隶 / 购买群友 | @群友 或 QQ 号 | 所有人 |
| | 奴隶市场 / 群友市场 | — | 所有人 |
| | 奴隶身价排行榜 / 身价排行榜 | — | 所有人 |
| | 奴隶资金排行榜 / 金币排行榜 / 资金排行榜 | — | 所有人 |
| 个人 | 我的奴隶 / 我的群友 | — | 所有人 |
| | 打工 / 一键打工 / 工作 / 一键工作 | — | 所有人 |
| | 赎身 | — | 所有人 |
| | 抢劫 / 打劫 | @群友（可选，未指定则随机） | 所有人 |
| 奴隶 | 放生奴隶 / 放生群友 | @奴隶 或 QQ 号 | 所有人 |
| | 训练 | @奴隶 或 QQ 号 | 所有人 |
| | 一键训练 | — | 所有人 |
| 战斗 | 决斗 | @奴隶1 @奴隶2（两者须为你所有） | 所有人 |
| | 排位赛 | — | 所有人 |
| | 参加排位赛 | @奴隶 或 QQ 号 | 所有人 |
| 银行 | 存款 | 金额 | 所有人 |
| | 一键存款 | — | 所有人 |
| | 取款 | 金额 | 所有人 |
| | 银行信息 | — | 所有人 |
| | 领取利息 | — | 所有人 |
| | 升级信用 | — | 所有人 |
| | 一键升级信用 | — | 所有人 |
| | 转账 | 金额 @群友 | 所有人 |
| 维护 | 奴隶备份 / 群友备份 / nl备份 | — | 管理员 |
| | 奴隶备份列表 | — | 管理员 |
| | 奴隶恢复备份 | 序号 | 管理员 |
| | 奴隶删除备份 | 序号 | 管理员 |

---

## 玩法要点

以下数值均为默认值，全部可在配置中调整。

- **购买**：价格为对方当前身价，成交后对方身价上涨（默认 +20）并获得等额金币；购买他人奴隶时原主人收回价款。
- **打工**：没有奴隶时自己打工；有奴隶则让全部奴隶打工。奴隶有概率摸鱼（默认 10%）导致身价下降，也有概率触发意外支出（默认 20%）。
- **训练**：花费奴隶身价的一定比例（默认 10%）训练，成功则身价提升（默认 +20%）；同一奴隶有冷却（默认 2 小时）。
- **决斗**：指定双方奴隶出战，胜者身价上涨、败者身价下降，身价守恒（胜者涨幅 = min(胜者加成, 败者实际跌幅, 上限)）。
- **排位赛**：Elo 评分段位制晋级，段位从青铜到钻石，胜利获得金币奖励。
- **银行**：存款上限随信用等级提升，每小时利息率默认 1%，单次最多累计 24 小时利息。
- **抢劫**：概率性成功（默认 30%），成功抢走对方部分现金，失败则被罚款。
- **赎身**：以双倍身价买回自由身，含赎身税（默认 5%），每周限 3 次。

---

## 配置项

在 AstrBot WebUI 插件配置面板中在线调整，共 20 项顶级配置。以下仅列出关键项，完整列表见 `_conf_schema.json`。

### 渲染与 WebUI

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `use_image` | `true` | 启用 HTML 图片渲染，失败自动回退纯文本 |
| `render_scale` | `2.0` | 图片清晰度倍率（1.0~4.0） |
| `webui_enabled` | `true` | 启用独立 WebUI 管理面板 |
| `webui_host` | `127.0.0.1` | WebUI 监听地址（`0.0.0.0` = 所有网卡；非本机监听需先设密码） |
| `webui_port` | `17818` | WebUI 端口 |
| `webui_password` | `""` | 管理员密码（首次启动自动生成临时密码；明文自动替换为 Argon2id 哈希，永不落盘） |
| `webui_session_ttl` | `43200` | 登录会话有效期（秒），绝对有效期不做滑动续期 |
| `ignoreCDUsers` | `[]` | 免冷却用户 ID 列表 |

### 游戏参数

| 配置组 | 关键子项 | 默认值 | 说明 |
|---|---|---|---|
| **购买** `purchase` | `cooldown` / `valueGain` | `3600` / `20.0` | 冷却秒数 / 成交后身价上涨额 |
| **打工** `work` | `cooldown` / `slaveownerCooldown` | `3600` / `60` | 普通冷却 / 奴隶主冷却 |
| | `wageMin~Max` / `slaveWageMin~Max` | `10~100` / `5~20` | 自己打工工资范围 / 奴隶打工工资范围 |
| | `slackRate` / `expenseRate` | `0.1` / `0.2` | 摸鱼概率 / 意外支出概率 |
| **抢劫** `rob` | `cooldown` / `successRate` | `600` / `0.3` | 冷却 / 成功率 |
| | `stealRate` / `penalty` | `0.2` / `0.1` | 抢走比例 / 失败罚款比例 |
| | `maxSteal` / `maxPenalty` | `100` / `50` | 单次抢走上限 / 罚款上限 |
| **训练** `training` | `cooldown` / `successRate` | `7200` / `0.7` | 冷却 / 成功率 |
| | `costRate` / `valueIncreaseRate` | `0.1` / `0.2` | 花费占身价比例 / 成功身价提升比例 |
| **决斗** `arena` | `cooldown` / `entryFee` | `7200` / `50` | 冷却 / 报名费 |
| | `valueBonus` / `loseValueRate` | `0.1` / `0.05` | 胜者身价提升 / 败者身价下降 |
| | `maxWinBonus` / `minValue` | `0.2` / `100.0` | 胜者涨幅上限 / 败者身价下限 |
| **排位赛** `ranking` | `cooldown` / `rewardRate` | `3600` / `0.1` | 冷却 / 奖励系数 |
| **赎身** `buyBack` | `cooldown` / `maxTimes` | `86400` / `3` | 冷却 / 每周次数上限 |
| | `priceMulti` / `taxRate` | `2.0` / `0.05` | 赎身价倍率 / 税率 |
| | `valueIncreaseMulti` | `1.2` | 赎身后身价倍率 |
| **银行** `bank` | `initialLimit` / `interestRate` | `1000` / `0.01` | 初始存款上限 / 每小时利息率 |
| | `upgradePriceMulti` / `limitIncreaseMulti` | `1.2` / `1.25` | 升级费用倍数 / 上限增长倍数 |
| | `maxInterestTime` | `24` | 单次最多计息小时数 |
| **转账** `transfer` | `feeRate` / `minAmount` | `0.1` / `100` | 手续费率 / 最低金额 |
| **备份** `backupKeep` | `10` | 保留份数（超出自动清理最旧的，0 = 不限） |

> 银行的 `initialLimit` / `initialLevel` / `initialUpgradePrice` 只影响此后新建的存档，已有玩家不变。

---

## WebUI 管理面板

默认 `http://127.0.0.1:17818`，可在插件配置中改端口/监听地址/密码。

### 首次登录

1. 启动插件后查看日志，会打印临时管理员密码（亦写入 `admin_passwd.txt`，仅显示一次）
2. 浏览器访问面板地址，用临时密码登录
3. 首次登录会被强制要求改密
4. 改密后明文不再落盘，配置中的 `webui_password` 自动替换为 Argon2id 哈希

### 非本机监听的安全门禁

把 `webui_host` 改成 `0.0.0.0` 前，必须满足以下任一条件，否则拒绝启动：

- 在插件配置中设置 `webui_password`
- 先在环回地址（127.0.0.1）下用临时密码登录并在面板里改掉密码

原因：临时密码还有明文落盘，运维从没主动选过它，不能拿它守住公网后台。

### 面板功能

- **总览**：玩家/群/金币/银行/奴隶统计
- **排行榜**：金币/身价/奴隶/银行四类，按群切换
- **市场**：全群身价一览
- **玩家**：分页列表、按 ID/昵称搜索、在线编辑（金币/身价/主人/银行）、删除存档（进回收站）
- **备份**：创建/恢复/删除全量备份（`sqlite3.Connection.backup()` 一致性快照）
- **文案**：在线编辑打工文案、决斗/排位赛文案与帮助长文本并热更新（无需重载插件）；帮助文案为可视化编辑——标题、分栏卡片、条目增删与排序都是表单操作，不用手写 JSON
- **配置**：面板内直接修改插件配置（含嵌套配置组，密码留空保持原值）

### 会话与安全

- `webui_session_ttl` 是**绝对**有效期——登录后满这么久必然重登，不做空闲滑动续期
- 改密会立即吊销其它设备的会话，只保留当前这一个
- Argon2id 口令哈希 + JWT(HS256) + 服务端会话表
- Host / Origin / CSRF 校验、登录限速与全站失败闸门、请求体上限
- 密码与密钥永不回显

---

## 文案系统

所有游戏文案外置 JSON，可在 WebUI 在线编辑并热更新，无需改代码。

| 文件 | 说明 |
|---|---|
| `resources/data/gameTexts.json` | 决斗动作文案池、排位赛对手/事件/段位文案 |
| `resources/data/uiTexts.json` | 221 条 UI 回复文案（购买/抢劫/训练/决斗/排位/银行/备份/打工/榜单） |
| `resources/data/workCopywriting.json` | 打工文案池（奴隶主与奴隶各有专属文案） |
| `resources/texts/help.json` | 帮助长文本 |

文案改动保存在插件数据目录（`data/plugin_data/astrbot_plugin_slave_market/overrides/`），不写插件自身目录，插件升级不会吞掉编辑成果。

---

## 数据存储

- **SQLite 单文件存档**，WAL 模式，所有改钱操作走单事务，避免并发下重复结算
- 数据文件位于 `data/plugin_data/astrbot_plugin_slave_market/`
- 备份恢复前自动留档一份当前库，防止恢复错了没有回头路
- 坏行取证：解析失败的玩家行进回收站并打标，不阻塞其余玩家

---

## 架构

```
astrbot_plugin_slave_market/
├── main.py               # 插件生命周期 + 渲染输出管线
├── handlers/             # 指令路由（按业务域拆分，声明式注册）
│   ├── base.py           #   Route/install 基座 + 事件解析 + 每用户指令锁
│   ├── market_cmds.py    #   购买/放生/我的奴隶/市场/排行榜
│   ├── social_cmds.py    #   打工/抢劫/赎身
│   ├── battle_cmds.py    #   训练/决斗/排位赛
│   ├── bank_cmds.py      #   银行/转账
│   └── system_cmds.py    #   帮助/数据备份（管理员）
├── core/
│   ├── context.py        #   GameCtx 共享上下文
│   ├── auth.py           #   Argon2id / JWT / 密码存储
│   ├── service.py        #   GameService facade
│   ├── svc/              #   游戏逻辑按域 Mixin
│   ├── db/               #   SQLite 存储层 Mixin
│   ├── result.py         #   R 结果封装
│   ├── texts.py          #   文案加载器（内置 + 用户覆盖）
│   └── renderer.py       #   Playwright 渲染器
├── webui/                # 独立端口管理面板（aiohttp）
│   ├── server/           #   管理 API（auth/serve/api/admin）
│   └── index.html / style.css / app.js
└── resources/
    ├── data/             #   文案 JSON（内置默认，WebUI 可在线编辑）
    ├── texts/            #   帮助长文本
    └── templates/        #   10 套 HTML 渲染模板
```

---

## 许可证

本项目采用原项目 [木兰宽松许可证 第2版](LICENSE) 开源。

---

## 致谢

- [Slave-Market（原插件）](https://gitee.com/Tloml-Starry/Slave-Market) — 群友之间的增温小游戏，购买群友来替你打工，然后买下更多群友（doge
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 多平台聊天机器人框架

---

<div align="center">

如果觉得这个插件对你带来快乐，欢迎 Star 或者 PR 一下哈哈

</div>
