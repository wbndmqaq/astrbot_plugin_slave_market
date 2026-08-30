# astrbot_plugin_slave_market

由 Yunzai 插件 [Slave-Market](https://gitee.com/Tloml-Starry/Slave-Market) 移植的 AstrBot 群互动经营游戏「奴隶市场」。

购买群友当奴隶、让奴隶打工赚金币、训练/决斗/排位赛抬身价、银行存取款吃利息、抢劫与赎身。SQLite 单文件存储，全部输出经独立 Playwright 渲染器以 HTML 模板出图，附独立端口 WebUI 管理面板。

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

## 依赖

- `playwright`（需执行一次 `python -m playwright install chromium`）
- `jinja2`、`aiohttp`
- 渲染失败或关闭 `use_image` 时自动回退纯文本，不中断指令。

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

## WebUI 管理面板

默认 `http://127.0.0.1:17818`，可在插件配置中改端口/监听地址/密码（非本机监听必须设置密码，否则拒绝启动）。功能：

- **总览**：玩家/群/金币/银行/奴隶统计
- **排行榜**：金币/身价/奴隶/银行四类，按群切换
- **市场**：全群身价一览
- **玩家**：分页列表、按 ID/昵称搜索、在线编辑（金币/身价/主人/银行）、删除存档（进回收站）
- **备份**：创建/恢复/删除全量备份（`VACUUM INTO` 一致性快照）
- **文案**：在线编辑打工文案与帮助长文本并热更新（无需重载插件）；帮助文案为可视化编辑——标题、分栏卡片、条目增删与排序都是表单操作，不用手写 JSON，纯文本兜底可按分栏一键生成
- **配置**：面板内直接修改插件配置（含嵌套配置组，密码留空保持原值）

## 存储

- SQLite 单文件库：`data/plugin_data/astrbot_plugin_slave_market/slave_market.db`，WAL 模式，所有读写经 `asyncio.to_thread`。
- 删除存档先挪入 `trash` 表留档，总量按 `backup_keep` 自动裁剪；全量备份为 `backups/` 下的独立 `.db` 快照，恢复时先关连接、替换文件、惰性重连。
- 不随插件更新被覆盖；从旧 JSON 存档版升级不会自动迁移数据（如需保留请手动迁移）。

## 玩法要点

- **购买**：价格为对方当前身价，成交后对方 +20 身价并获得等额金币；购买他人奴隶时原主人收回价款。
- **打工**：没有奴隶自己打工；有奴隶则让全部奴隶打工（10% 概率摸鱼掉身价），20% 概率触发意外支出。
- **训练**：花奴隶身价的 10% 训练，成功则身价 +20%；同一奴隶 2 小时 CD。
- **决斗/排位**：身价决定胜率，Elo 计分，段位从青铜到钻石。
- **银行**：存款上限随信用等级提升，每小时 1% 利息（最多计 24 小时）。

## 相对原版的改进

- **纯异步**：所有磁盘 IO（SQLite 读写、模板渲染、备份）走 `asyncio.to_thread`，不阻塞事件循环；WebUI 全部端点带全局错误中间件，异常返回 JSON 而非裸 500。
- **并发安全**：SQLite 写事务用 `threading.RLock` 串行化（WAL 下读写互不阻塞）；每用户指令锁消除连发指令的双花窗口。
- **无泄漏**：Playwright 渲染器懒启动、失败时显式关闭遗留实例防止孤儿进程，截图目录自动清理；数据库连接在 `terminate()` 统一关闭；回收站/备份按配置数量自动裁剪；指令锁表有容量上限；昵称缓存容量受控（超限先清过期再清最旧）。
- **全模板渲染**：所有回复（含错误提示）经 HTML 模板出图，统一视觉，纯文本兜底。
- **平台昵称适配**（参考 astrbot_plugin_shangbanzu）：aiocqhttp/OneBot 平台经 `get_group_member_info` 拉群名片；QQ 官方平台经 botpy 的 Route HTTP 接口探测成员昵称（灰度接口，未开放时静默负缓存）。拉取结果带 TTL 内存缓存并入库，市场/排行榜显示真实昵称；拉取失败静默，不影响指令。
- **跨平台**：不依赖 QQ 群成员列表 API，任何 AstrBot 平台适配器均可游玩。

## License

木兰宽松许可证 第2版
