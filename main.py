"""
AstrBot 奴隶市场插件 - 完全符合AstrBot官方开发标准
基于原Yunzai-Bot V3奴隶市场插件转换

遵循AstrBot插件开发规范：
- 插件类继承自Star基类
- 使用@register装饰器注册插件
- 主文件命名为main.py
- 完善的错误处理和日志记录
- 符合PEP8编码规范
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import astrbot.api.message_components as Comp
import yaml
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# 导入功能模块
from .bank import BankModule
from .ranking import RankingModule
from .rob import RobModule
from .slave_management import SlaveManagementModule
from .training import TrainingModule
from .weekly_reset import WeeklyResetModule


@register(
    "slave_market",
    "Tloml-Starry",
    "一个群聊文字游戏插件，玩家可以体验当主人和奴隶的乐趣",
    "1.0.0",
    "https://github.com/Tloml-Starry/astrbot_plugin_slave_market",
)
class SlaveMarketPlugin(Star):
    """奴隶市场插件主类

    完全符合AstrBot插件开发标准，继承自Star基类，
    使用@register装饰器注册插件元数据信息。
    """

    def __init__(self, context: Context):
        """插件初始化

        Args:
            context: AstrBot上下文对象，包含大多数组件
        """
        super().__init__(context)

        # 插件路径配置
        self.plugin_path = os.path.dirname(os.path.abspath(__file__))
        self.data_path = os.path.join(self.plugin_path, "data")
        self.config_path = os.path.join(self.plugin_path, "config.yaml")

        # 初始化数据目录
        try:
            os.makedirs(self.data_path, exist_ok=True)
            os.makedirs(os.path.join(self.data_path, "player"), exist_ok=True)
            logger.info("数据目录初始化完成")
        except Exception as e:
            logger.error(f"数据目录初始化失败: {e}")
            raise

        # 加载配置
        self.config = self.load_config()

        # 加载文案
        self.copywriting = self.load_copywriting()

        # 初始化功能模块
        self.bank_module = BankModule(self)
        self.training_module = TrainingModule(self)
        self.ranking_module = RankingModule(self)
        self.slave_management_module = SlaveManagementModule(self)
        self.weekly_reset_module = WeeklyResetModule(self)
        self.rob_module = RobModule(self)

        # 启动定时任务
        asyncio.create_task(self.check_weekly_reset())

        logger.info("奴隶市场插件已成功加载并初始化完成")

    def load_config(self) -> Dict[str, Any]:
        """加载配置文件

        Returns:
            Dict[str, Any]: 配置字典
        """
        default_config = {
            "buyBack": {"cooldown": 86400, "maxTimes": 3, "taxRate": 0.05},
            "rob": {"cooldown": 600, "successRate": 0.3, "penalty": 0.1},
            "work": {"cooldown": 3600, "slaveownerCooldown": 60},
            "purchase": {"cooldown": 3600},
            "bank": {
                "initialLimit": 1000,
                "initialLevel": 1,
                "upgradePriceMulti": 1.2,
                "limitIncreaseMulti": 1.25,
                "initialUpgradePrice": 100,
                "interestRate": 0.01,
                "maxInterestTime": 24,
            },
            "training": {
                "cooldown": 7200,
                "successRate": 0.7,
                "costRate": 0.1,
                "valueIncreaseRate": 0.2,
            },
            "arena": {
                "cooldown": 7200,
                "entryFee": 50,
                "rewardRate": 0.2,
                "valueBonus": 0.1,
            },
            "ranking": {
                "cooldown": 3600,
                "baseReward": 10,
                "winBonus": 0.2,
                "tierBonus": {
                    "青铜": 1,
                    "白银": 1.2,
                    "黄金": 1.5,
                    "铂金": 2,
                    "钻石": 3,
                },
            },
            "transfer": {"feeRate": 0.1, "minAmount": 100},
            "weeklyReset": {
                "enabled": True,
                "resetTime": {"day": 1, "hour": 0, "minute": 0},
                "preserveData": {"nickname": True, "basicValue": 100},
            },
        }

        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}

                # 合并默认配置
                for key, value in default_config.items():
                    if key not in config:
                        config[key] = value

                logger.info("配置文件加载成功")
                return config
            except Exception as e:
                logger.error(f"配置文件加载失败: {e}")
                logger.info("使用默认配置")
                return default_config

        logger.info("配置文件不存在，使用默认配置")
        return default_config

    def load_copywriting(self) -> Dict[str, List[str]]:
        """加载文案配置

        Returns:
            Dict[str, List[str]]: 文案字典
        """
        copywriting_path = os.path.join(
            self.plugin_path, "resources", "data", "workCopywriting.json"
        )
        if os.path.exists(copywriting_path):
            try:
                with open(copywriting_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info("文案文件加载成功")
                    return data
            except Exception as e:
                logger.error(f"文案文件加载失败: {e}")

        # 默认文案
        logger.info("使用默认文案")
        return {
            "slaveowner": [
                "被家族赞助初始资金，获得收入",
                "一时兴起买了张彩票中了小奖，获得收入",
                "在闲鱼上卖东西，获得收入",
                "一时兴起在地铁口表演才艺，靠路人打赏，获得收入",
            ],
            "slave": [
                "努力打工赚钱，获得收入",
                "完成兼职任务，获得收入",
                "帮忙做家务，获得收入",
            ],
        }

    def get_player_data_path(self, group_id: str, user_id: str) -> str:
        """获取玩家数据文件路径

        Args:
            group_id: 群组ID
            user_id: 用户ID

        Returns:
            str: 文件路径
        """
        group_path = os.path.join(self.data_path, "player", group_id)
        os.makedirs(group_path, exist_ok=True)
        return os.path.join(group_path, f"{user_id}.json")

    def get_player_data(self, group_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """获取玩家数据

        Args:
            group_id: 群组ID
            user_id: 用户ID

        Returns:
            Optional[Dict[str, Any]]: 玩家数据或None
        """
        file_path = self.get_player_data_path(group_id, user_id)
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取玩家数据失败: {e}")
        return None

    def save_player_data(
        self, group_id: str, user_id: str, data: Dict[str, Any]
    ) -> None:
        """保存玩家数据

        Args:
            group_id: 群组ID
            user_id: 用户ID
            data: 玩家数据
        """
        file_path = self.get_player_data_path(group_id, user_id)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存玩家数据失败: {e}")

    def ensure_player_exists(
        self, group_id: str, user_id: str, nickname: str = ""
    ) -> Dict[str, Any]:
        """确保玩家存在，如果不存在则创建

        Args:
            group_id: 群组ID
            user_id: 用户ID
            nickname: 昵称

        Returns:
            Dict[str, Any]: 玩家数据
        """
        data = self.get_player_data(group_id, user_id)
        if not data:
            data = {
                "user_id": user_id,
                "nickname": nickname or f"用户{user_id}",
                "currency": 0,
                "value": 100,
                "slaves": [],
                "master": None,
                "bank": {
                    "balance": 0,
                    "level": 1,
                    "limit": self.config["bank"]["initialLimit"],
                    "lastInterestTime": int(time.time()),
                },
                "cooldowns": {},
                "arena": {"tier": "青铜", "points": 0, "wins": 0, "losses": 0},
                "lastWorkTime": 0,
                "createdAt": int(time.time()),
            }
            self.save_player_data(group_id, user_id, data)
            logger.info(f"创建新玩家数据: 群{group_id} 用户{user_id}")
        return data

    def check_cooldown(self, data: Dict[str, Any], action: str, cooldown: int) -> bool:
        """检查冷却时间

        Args:
            data: 玩家数据
            action: 动作名称
            cooldown: 冷却时间（秒）

        Returns:
            bool: 是否可以通过冷却检查
        """
        current_time = int(time.time())
        last_time = data.get("cooldowns", {}).get(action, 0)
        return current_time - last_time >= cooldown

    def set_cooldown(self, data: Dict[str, Any], action: str) -> None:
        """设置冷却时间

        Args:
            data: 玩家数据
            action: 动作名称
        """
        if "cooldowns" not in data:
            data["cooldowns"] = {}
        data["cooldowns"][action] = int(time.time())

    def check_permission(self, user_id: str) -> bool:
        """检查用户是否有特殊权限（跳过冷却）

        Args:
            user_id: 用户ID

        Returns:
            bool: 是否有权限
        """
        ignore_users = self.config.get("ignoreCDUsers", [])
        return str(user_id) in [str(uid) for uid in ignore_users]

    def get_all_players(self, group_id: str) -> List[str]:
        """获取群组内所有玩家ID列表

        Args:
            group_id: 群组ID

        Returns:
            List[str]: 玩家ID列表
        """
        players = []
        group_path = self.get_player_data_path(group_id, "").replace(".json", "")
        group_dir = os.path.dirname(group_path)

        if os.path.exists(group_dir):
            for filename in os.listdir(group_dir):
                if filename.endswith(".json") and filename != "backup":
                    user_id = filename[:-5]  # 移除.json后缀
                    players.append(user_id)

        return players

    def get_group_list(self) -> List[str]:
        """获取所有群组列表

        Returns:
            List[str]: 群组ID列表
        """
        groups = []
        player_dir = os.path.join(self.data_path, "player")

        if os.path.exists(player_dir):
            for dirname in os.listdir(player_dir):
                if (
                    os.path.isdir(os.path.join(player_dir, dirname))
                    and dirname.isdigit()
                ):
                    groups.append(dirname)

        return groups

    async def check_weekly_reset(self):
        """检查每周重置
        每分钟检查一次是否需要执行每周重置
        """
        while True:
            try:
                if self.weekly_reset_module.should_reset():
                    result = self.weekly_reset_module.perform_weekly_reset()
                    if result["success"]:
                        logger.info(f"每周重置已自动执行：{result['message']}")
            except Exception as e:
                logger.error(f"每周重置检查失败: {e}")

            await asyncio.sleep(60)  # 每分钟检查一次

    # ===== 指令处理函数 =====

    @filter.command("奴隶市场")
    async def market_info(self, event: AstrMessageEvent):
        """查看奴隶市场信息

        这是奴隶市场的核心指令，显示用户和奴隶的基本信息
        """
        try:
            if not event.get_group_id():
                yield event.plain_result("该游戏只能在群内使用")
                return

            group_id = str(event.get_group_id())
            user_id = str(event.get_sender_id())

            # 确保玩家存在
            data = self.ensure_player_exists(group_id, user_id, event.get_sender_name())

            # 构建市场信息
            market_data = {"user": data, "slaves": []}

            # 获取奴隶详细信息
            for slave_id in data.get("slaves", []):
                slave_data = self.get_player_data(group_id, str(slave_id))
                if slave_data:
                    market_data["slaves"].append(slave_data)

            # 渲染HTML模板
            html_content = self.render_market_html(market_data)

            # 这里应该实现实际的HTML渲染
            # 暂时使用文本回复
            reply = self.generate_market_text(market_data)
            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"奴隶市场指令执行失败: {e}")
            yield event.plain_result("执行失败，请稍后重试")

    def render_market_html(self, data: Dict[str, Any]) -> str:
        """渲染市场信息HTML

        Args:
            data: 市场数据

        Returns:
            str: HTML内容
        """
        return """
        <div style="font-family: 'Microsoft YaHei', sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; border-radius: 15px;">
            <h1 style="color: white; text-align: center; margin-bottom: 20px;">🌟 奴隶市场 🌟</h1>
            <div style="background: rgba(255,255,255,0.9); padding: 15px; border-radius: 10px; margin-bottom: 15px;">
                <h2 style="color: #333;">👤 我的信息</h2>
                <p style="color: #666;">昵称: {{ user.nickname }}</p>
                <p style="color: #666;">金币: {{ user.currency }}</p>
                <p style="color: #666;">身价: {{ user.value }}</p>
                <p style="color: #666;">奴隶数量: {{ user.slaves|length }}</p>
            </div>
            {% if slaves %}
            <div style="background: rgba(255,255,255,0.9); padding: 15px; border-radius: 10px;">
                <h2 style="color: #333;">👥 我的奴隶</h2>
                {% for slave in slaves %}
                <div style="border-bottom: 1px solid #eee; padding: 10px 0;">
                    <p style="color: #666;">{{ slave.nickname }} - 身价: {{ slave.value }} 金币</p>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        """

    def generate_market_text(self, data: Dict[str, Any]) -> str:
        """生成市场信息文本

        Args:
            data: 市场数据

        Returns:
            str: 格式化的文本信息
        """
        user = data["user"]
        slaves = data["slaves"]

        reply = "🌟 奴隶市场 🌟\n\n"
        reply += f"👤 {user.get('nickname', '未知用户')} 的信息\n"
        reply += f"💰 金币: {user.get('currency', 0):,} 金币\n"
        reply += f"💎 身价: {user.get('value', 0):,}\n"
        reply += f"👥 奴隶数量: {len(slaves)}\n"

        if slaves:
            reply += "\n📋 拥有的奴隶:\n"
            for slave in slaves:
                reply += f"  • {slave.get('nickname', '未知')} - 身价: {slave.get('value', 0)} 金币\n"

        if user.get("master"):
            master_data = self.get_player_data(
                str(user.get("group_id", "")), str(user["master"])
            )
            if master_data:
                reply += f"\n🔗 主人: {master_data.get('nickname', '未知')}\n"

        return reply

    @filter.command("打工")
    async def work(self, event: AstrMessageEvent):
        """打工赚钱

        玩家可以通过打工获得金币，有奴隶的奴隶主收益更高
        """
        try:
            if not event.get_group_id():
                yield event.plain_result("该游戏只能在群内使用")
                return

            group_id = str(event.get_group_id())
            user_id = str(event.get_sender_id())
            nickname = event.get_sender_name()

            # 确保玩家存在
            data = self.ensure_player_exists(group_id, user_id, nickname)

            # 检查冷却时间
            if not self.check_cooldown(data, "work", self.config["work"]["cooldown"]):
                remaining = self.config["work"]["cooldown"] - (
                    int(time.time()) - data["cooldowns"]["work"]
                )
                yield event.plain_result(f"打工冷却中，还需等待 {remaining // 60} 分钟")
                return

            # 计算收益
            is_slaveowner = len(data.get("slaves", [])) > 0
            if is_slaveowner:
                # 奴隶主收益更高
                base_income = random.randint(50, 150)
                work_descriptions = self.copywriting.get(
                    "slaveowner", ["完成工作获得收入"]
                )
            else:
                base_income = random.randint(20, 80)
                work_descriptions = self.copywriting.get("slave", ["完成工作获得收入"])

            description = random.choice(work_descriptions)

            # 更新数据
            data["currency"] += base_income
            self.set_cooldown(data, "work")
            self.save_player_data(group_id, user_id, data)

            logger.info(f"用户{user_id}打工获得{base_income}金币")
            yield event.plain_result(f"✅ {description}\n💰 获得 {base_income} 金币！")

        except Exception as e:
            logger.error(f"打工指令执行失败: {e}")
            yield event.plain_result("打工失败，请稍后重试")

    @filter.command("购买奴隶")
    async def purchase_slave(self, event: AstrMessageEvent, target_user: str):
        """购买奴隶

        购买其他玩家作为奴隶，需要支付金币

        Args:
            target_user: 目标用户（@用户名或QQ号）
        """
        try:
            if not event.get_group_id():
                yield event.plain_result("该游戏只能在群内使用")
                return

            group_id = str(event.get_group_id())
            buyer_id = str(event.get_sender_id())
            buyer_name = event.get_sender_name()

            # 确保购买者存在
            buyer_data = self.ensure_player_exists(group_id, buyer_id, buyer_name)

            # 解析目标用户ID
            target_id = None
            if target_user.startswith("@"):
                target_id = target_user[1:]
            else:
                target_id = target_user

            if not target_id or target_id == buyer_id:
                yield event.plain_result("无法购买自己或无效的目标")
                return

            # 确保目标用户存在
            target_data = self.ensure_player_exists(
                group_id, target_id, f"用户{target_id}"
            )

            # 检查冷却时间
            if not self.check_cooldown(
                buyer_data, "purchase", self.config["purchase"]["cooldown"]
            ):
                remaining = self.config["purchase"]["cooldown"] - (
                    int(time.time()) - buyer_data["cooldowns"]["purchase"]
                )
                yield event.plain_result(f"购买冷却中，还需等待 {remaining // 60} 分钟")
                return

            # 检查是否已经是奴隶
            if target_id in buyer_data.get("slaves", []):
                yield event.plain_result("该用户已经是你的奴隶了")
                return

            # 检查目标是否已有主人
            if target_data.get("master") and target_data["master"] != buyer_id:
                yield event.plain_result("该用户已经有主人了")
                return

            # 计算购买价格
            purchase_price = int(target_data["value"] * 1.2)

            # 检查金币是否足够
            if buyer_data["currency"] < purchase_price:
                yield event.plain_result(
                    f"金币不足！需要 {purchase_price} 金币，你只有 {buyer_data['currency']} 金币"
                )
                return

            # 执行购买
            buyer_data["currency"] -= purchase_price
            if "slaves" not in buyer_data:
                buyer_data["slaves"] = []
            buyer_data["slaves"].append(target_id)

            target_data["master"] = buyer_id

            # 设置冷却时间
            self.set_cooldown(buyer_data, "purchase")

            # 保存数据
            self.save_player_data(group_id, buyer_id, buyer_data)
            self.save_player_data(group_id, target_id, target_data)

            logger.info(f"用户{buyer_id}购买用户{target_id}，花费{purchase_price}金币")
            yield event.plain_result(
                f"✅ 成功购买奴隶 {target_data['nickname']}！\n💰 花费 {purchase_price} 金币"
            )

        except Exception as e:
            logger.error(f"购买奴隶指令执行失败: {e}")
            yield event.plain_result("购买失败，请稍后重试")

    @filter.command("我的奴隶")
    async def my_slaves(self, event: AstrMessageEvent):
        """查看我的奴隶信息

        显示玩家自己的信息和拥有的奴隶列表
        """
        try:
            if not event.get_group_id():
                yield event.plain_result("该游戏只能在群内使用")
                return

            group_id = str(event.get_group_id())
            user_id = str(event.get_sender_id())
            nickname = event.get_sender_name()

            # 确保玩家存在
            data = self.ensure_player_exists(group_id, user_id, nickname)

            # 构建回复消息
            reply = f"👤 {nickname} 的信息\n"
            reply += f"💰 金币: {data.get('currency', 0):,} 金币\n"
            reply += f"💎 身价: {data.get('value', 0):,}\n"
            reply += f"👥 奴隶数量: {len(data.get('slaves', []))}\n"

            if data.get("slaves"):
                reply += "\n📋 奴隶列表:\n"
                for slave_id in data["slaves"]:
                    slave_data = self.get_player_data(group_id, str(slave_id))
                    if slave_data:
                        reply += f"  • {slave_data['nickname']} (身价: {slave_data['value']})\n"

            if data.get("master"):
                master_data = self.get_player_data(group_id, str(data["master"]))
                if master_data:
                    reply += f"\n🔗 主人: {master_data['nickname']}"

            yield event.plain_result(reply)

        except Exception as e:
            logger.error(f"我的奴隶指令执行失败: {e}")
            yield event.plain_result("获取信息失败，请稍后重试")

    @filter.command("抢劫")
    async def rob(self, event: AstrMessageEvent):
        """抢劫其他玩家金币

        尝试抢劫其他玩家的金币，有成功率和冷却时间限制
        """
        async for result in self.rob_module.rob(event):
            yield result

    @filter.command("奴隶帮助")
    async def help(self, event: AstrMessageEvent):
        """显示帮助信息

        显示插件的所有可用指令和功能说明
        """
        try:
            help_text = """
🌟 奴隶市场帮助 🌟

📊 基础功能:
• #奴隶市场 - 查看市场信息
• #购买奴隶 @群友/QQ号 - 购买奴隶
• #我的奴隶 - 查看个人信息
• #打工 - 赚取金币
• #抢劫 - 抢劫其他玩家金币

🏦 银行系统:
• #银行信息 - 查看银行信息
• #存款 数量 - 存款
• #取款 数量 - 取款
• #升级信用 - 升级银行等级
• #领取利息 - 领取利息

⚔️ 竞技系统:
• #训练奴隶 - 训练奴隶提升价值
• #奴隶决斗 - 奴隶对战
• #排位赛 - 参与排位赛

👥 奴隶管理:
• #赎身 - 赎回自由身
• #放生奴隶 @群友/QQ号 - 放生奴隶

📈 排行榜:
• #排行榜 - 查看所有排行榜
• #金币排行 - 金币排行榜
• #身价排行 - 身价排行榜
• #奴隶排行 - 奴隶数量排行榜

🔄 系统功能:
• #奴隶重置状态 - 查看重置状态
• #上周排行榜 - 查看上周排行榜

💡 提示:
• 每周一00:00自动重置数据
• 重置前会自动备份排行榜
• 合理使用各种功能，享受游戏乐趣！
            """

            yield event.plain_result(help_text)

        except Exception as e:
            logger.error(f"帮助指令执行失败: {e}")
            yield event.plain_result("获取帮助信息失败")

    def terminate(self):
        """插件终止函数

        当插件被卸载/停用时会调用此函数
        用于清理资源和保存状态
        """
        try:
            logger.info("奴隶市场插件正在卸载...")
            # 这里可以添加清理逻辑
            logger.info("奴隶市场插件已卸载")
        except Exception as e:
            logger.error(f"插件卸载失败: {e}")


# 导出插件类
__all__ = ["SlaveMarketPlugin"]
