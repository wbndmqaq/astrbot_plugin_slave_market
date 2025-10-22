"""
训练功能模块
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import random
import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .main import SlaveMarketPlugin

class TrainingModule:
    def __init__(self, plugin: 'SlaveMarketPlugin'):
        self.plugin = plugin
        self.config = plugin.config
    
    @filter.command("训练奴隶")
    async def train_slave(self, event: AstrMessageEvent):
        """训练奴隶
        
        可以训练单个奴隶或批量训练所有奴隶
        """
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        # 检查是否有奴隶
        if not data.get("slaves"):
            yield event.plain_result("你没有奴隶，无法训练")
            return
        
        # 检查冷却时间
        if not self.plugin.check_cooldown(data, "training", self.config["training"]["cooldown"]):
            remaining = self.config["training"]["cooldown"] - (int(time.time()) - data["cooldowns"]["training"])
            yield event.plain_result(f"训练冷却中，还需等待 {remaining//60} 分钟")
            return
        
        # 解析训练参数
        message_str = event.message_str.strip()
        target_slave_id = None
        
        # 检查是否有指定奴隶ID
        if len(message_str.split()) > 1:
            try:
                target_slave_id = message_str.split()[1]
                if target_slave_id.startswith("@"):
                    target_slave_id = target_slave_id[1:]
            except:
                pass
        
        # 确定要训练的奴隶列表
        slaves_to_train = []
        if target_slave_id and target_slave_id in data["slaves"]:
            # 训练指定奴隶
            slaves_to_train = [target_slave_id]
        else:
            # 批量训练所有奴隶
            slaves_to_train = data["slaves"]
        
        if not slaves_to_train:
            yield event.plain_result("没有可训练的奴隶")
            return
        
        # 执行训练
        results = []
        total_cost = 0
        success_count = 0
        fail_count = 0
        
        for slave_id in slaves_to_train:
            slave_data = self.plugin.get_player_data(group_id, str(slave_id))
            if not slave_data:
                continue
            
            # 计算训练费用
            training_cost = int(slave_data["value"] * self.config["training"]["costRate"])
            
            if data["currency"] < training_cost:
                results.append({
                    "name": slave_data["nickname"],
                    "status": "failed",
                    "result": f"金币不足，需要{training_cost}金币",
                    "valueChange": 0
                })
                fail_count += 1
                continue
            
            # 检查训练成功率
            if random.random() < self.config["training"]["successRate"]:
                # 训练成功
                value_increase = int(slave_data["value"] * self.config["training"]["valueIncreaseRate"])
                
                # 扣除费用
                data["currency"] -= training_cost
                total_cost += training_cost
                
                # 提升奴隶价值
                slave_data["value"] += value_increase
                
                # 保存奴隶数据
                self.plugin.save_player_data(group_id, str(slave_id), slave_data)
                
                results.append({
                    "name": slave_data["nickname"],
                    "status": "success",
                    "result": f"训练成功，身价提升{value_increase}金币",
                    "valueChange": value_increase
                })
                success_count += 1
            else:
                # 训练失败
                half_cost = training_cost // 2
                data["currency"] -= half_cost
                total_cost += half_cost
                
                results.append({
                    "name": slave_data["nickname"],
                    "status": "failed",
                    "result": f"训练失败，损失{half_cost}金币",
                    "valueChange": 0
                })
                fail_count += 1
        
        # 设置冷却时间
        self.plugin.set_cooldown(data, "training")
        
        # 保存主人数据
        self.plugin.save_player_data(group_id, user_id, data)
        
        # 生成训练报告
        if len(slaves_to_train) == 1:
            # 单个训练结果
            result = results[0]
            if result["status"] == "success":
                yield event.plain_result(
                    f"✅ 训练成功！\n"
                    f"👤 奴隶: {result['name']}\n"
                    f"💰 花费: {total_cost} 金币\n"
                    f"📈 价值提升: {result['valueChange']} 金币\n"
                    f"💎 新身价: {self.plugin.get_player_data(group_id, slaves_to_train[0])['value']} 金币"
                )
            else:
                yield event.plain_result(
                    f"❌ 训练失败！\n"
                    f"👤 奴隶: {result['name']}\n"
                    f"💰 损失: {total_cost} 金币\n"
                    f"📋 原因: {result['result']}"
                )
        else:
            # 批量训练报告
            summary = {
                "totalSlaves": len(slaves_to_train),
                "successCount": success_count,
                "failCount": fail_count,
                "totalCost": total_cost,
                "remainingCurrency": data["currency"]
            }
            
            report = f"💪 批量训练完成！\n\n"
            report += f"📊 训练统计:\n"
            report += f"• 总奴隶数: {summary['totalSlaves']}\n"
            report += f"• 成功: {summary['successCount']}\n"
            report += f"• 失败: {summary['failCount']}\n"
            report += f"• 总花费: {summary['totalCost']} 金币\n"
            report += f"• 剩余金币: {summary['remainingCurrency']} 金币\n\n"
            
            if success_count > 0:
                report += "✅ 成功训练的奴隶:\n"
                for result in results:
                    if result["status"] == "success":
                        report += f"• {result['name']} (+{result['valueChange']}身价)\n"
            
            if fail_count > 0:
                report += "\n❌ 训练失败的奴隶:\n"
                for result in results:
                    if result["status"] == "failed":
                        report += f"• {result['name']}\n"
            
            yield event.plain_result(report)
    
    @filter.command("奴隶决斗")
    async def slave_arena(self, event: AstrMessageEvent):
        """奴隶决斗"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        # 检查是否有奴隶
        if not data.get("slaves"):
            yield event.plain_result("你没有奴隶，无法参与决斗")
            return
        
        # 检查冷却时间
        if not self.plugin.check_cooldown(data, "arena", self.config["arena"]["cooldown"]):
            remaining = self.config["arena"]["cooldown"] - (int(time.time()) - data["cooldowns"]["arena"])
            yield event.plain_result(f"决斗冷却中，还需等待 {remaining//60} 分钟")
            return
        
        # 检查参赛费用
        entry_fee = self.config["arena"]["entryFee"]
        if data["currency"] < entry_fee:
            yield event.plain_result(f"金币不足！参赛需要 {entry_fee} 金币，你只有 {data['currency']} 金币")
            return
        
        # 选择一个奴隶参赛
        slave_id = random.choice(data["slaves"])
        slave_data = self.plugin.get_player_data(group_id, str(slave_id))
        
        if not slave_data:
            yield event.plain_result("决斗失败：奴隶数据不存在")
            return
        
        # 扣除参赛费用
        data["currency"] -= entry_fee
        
        # 模拟决斗结果
        if random.random() < 0.5:
            # 获胜
            reward = int(entry_fee * (1 + self.config["arena"]["rewardRate"]))
            value_bonus = int(slave_data["value"] * self.config["arena"]["valueBonus"])
            
            # 发放奖励
            data["currency"] += reward
            slave_data["value"] += value_bonus
            
            result_message = f"🏆 决斗胜利！\n👤 参赛者: {slave_data['nickname']}\n💰 报名费: {entry_fee} 金币\n🎁 奖励: {reward} 金币\n💎 身价提升: {value_bonus} 金币"
        else:
            # 失败
            result_message = f"💔 决斗失败！\n👤 参赛者: {slave_data['nickname']}\n💰 损失报名费: {entry_fee} 金币"
        
        # 设置冷却时间
        self.plugin.set_cooldown(data, "arena")
        
        # 保存数据
        self.plugin.save_player_data(group_id, user_id, data)
        self.plugin.save_player_data(group_id, str(slave_id), slave_data)
        
        yield event.plain_result(result_message)
    
    @filter.command("排位赛")
    async def ranking_battle(self, event: AstrMessageEvent):
        """排位赛"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        # 检查冷却时间
        if not self.plugin.check_cooldown(data, "ranking", self.config["ranking"]["cooldown"]):
            remaining = self.config["ranking"]["cooldown"] - (int(time.time()) - data["cooldowns"]["ranking"])
            yield event.plain_result(f"排位赛冷却中，还需等待 {remaining//60} 分钟")
            return
        
        # 获取当前段位
        arena_data = data.get("arena", {})
        current_tier = arena_data.get("tier", "青铜")
        current_points = arena_data.get("points", 0)
        
        # 模拟排位赛结果
        if random.random() < 0.6:  # 60%胜率
            # 获胜
            base_reward = self.config["ranking"]["baseReward"]
            tier_bonus = self.config["ranking"]["tierBonus"].get(current_tier, 1)
            win_bonus = int(base_reward * self.config["ranking"]["winBonus"])
            
            total_reward = int(base_reward * tier_bonus + win_bonus)
            points_gained = random.randint(10, 25)
            
            # 更新数据
            data["currency"] += total_reward
            arena_data["points"] = current_points + points_gained
            arena_data["wins"] = arena_data.get("wins", 0) + 1
            
            # 检查段位提升
            new_tier = self.check_tier_promotion(arena_data["points"])
            if new_tier != current_tier:
                arena_data["tier"] = new_tier
                tier_message = f"\n🎉 段位提升: {current_tier} → {new_tier}"
            else:
                tier_message = ""
            
            result_message = f"🏆 排位赛胜利！\n📊 当前段位: {current_tier}\n💰 获得奖励: {total_reward} 金币\n⭐ 积分: +{points_gained}{tier_message}"
        else:
            # 失败
            points_lost = random.randint(5, 15)
            arena_data["points"] = max(0, current_points - points_lost)
            arena_data["losses"] = arena_data.get("losses", 0) + 1
            
            result_message = f"💔 排位赛失败！\n📊 当前段位: {current_tier}\n⭐ 积分: -{points_lost}"
        
        # 设置冷却时间
        self.plugin.set_cooldown(data, "ranking")
        
        # 保存数据
        data["arena"] = arena_data
        self.plugin.save_player_data(group_id, user_id, data)
        
        yield event.plain_result(result_message)
    
    def check_tier_promotion(self, points: int) -> str:
        """检查段位提升"""
        if points >= 2000:
            return "钻石"
        elif points >= 1500:
            return "铂金"
        elif points >= 1000:
            return "黄金"
        elif points >= 500:
            return "白银"
        else:
            return "青铜"