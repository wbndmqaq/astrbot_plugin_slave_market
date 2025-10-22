"""
抢劫功能模块
基于原Yunzai-Bot V3的Rob.js转换
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import random
import time
from typing import Dict, Any, List, Optional

if TYPE_CHECKING:
    from .main import SlaveMarketPlugin

class RobModule:
    def __init__(self, plugin: 'SlaveMarketPlugin'):
        self.plugin = plugin
        self.config = plugin.config
    
    @filter.command("抢劫")
    async def rob(self, event: AstrMessageEvent):
        """抢劫其他玩家金币
        
        尝试抢劫其他玩家的金币，有成功率和冷却时间限制
        """
        try:
            if not event.get_group_id():
                yield event.plain_result("该功能只能在群内使用")
                return
            
            group_id = str(event.get_group_id())
            user_id = str(event.get_sender_id())
            nickname = event.get_sender_name()
            
            # 确保玩家存在
            robber_data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
            
            # 检查权限
            if not self.check_permission(event):
                current_time = int(time.time())
                last_rob_time = robber_data.get("lastRobTime", 0)
                rob_cooldown = self.config["rob"]["cooldown"]
                
                if current_time - last_rob_time < rob_cooldown:
                    remaining_time = rob_cooldown - (current_time - last_rob_time)
                    hours = remaining_time // 3600
                    minutes = (remaining_time % 3600) // 60
                    yield event.plain_result(f"抢劫冷却中，剩余时间：{hours}小时{minutes}分钟")
                    return
            
            # 获取目标用户
            target_id = None
            if hasattr(event, 'at') and event.at:
                # 如果有@目标
                target_id = str(event.at)
                self.plugin.ensure_player_exists(group_id, target_id, f"用户{target_id}")
            else:
                # 随机选择目标
                all_players = self.get_all_players(group_id)
                if len(all_players) < 2:
                    yield event.plain_result("群内玩家不足，无法抢劫")
                    return
                
                potential_targets = [pid for pid in all_players if pid != user_id]
                if not potential_targets:
                    yield event.plain_result("没有可抢劫的目标")
                    return
                
                target_id = random.choice(potential_targets)
            
            if target_id == user_id:
                yield event.plain_result("不能抢劫自己")
                return
            
            # 获取目标玩家数据
            target_data = self.plugin.get_player_data(group_id, target_id)
            if not target_data:
                yield event.plain_result("目标玩家数据不存在")
                return
            
            # 检查目标是否有足够的金币
            if target_data.get("currency", 0) < 10:
                yield event.plain_result("目标玩家金币太少，不值得抢劫")
                return
            
            # 执行抢劫
            success_rate = self.config["rob"]["successRate"]
            if random.random() < success_rate:
                # 抢劫成功
                rob_amount = min(random.randint(10, 50), target_data["currency"])
                
                # 更新数据
                robber_data["currency"] += rob_amount
                target_data["currency"] -= rob_amount
                robber_data["lastRobTime"] = int(time.time())
                
                # 保存数据
                self.plugin.save_player_data(group_id, user_id, robber_data)
                self.plugin.save_player_data(group_id, target_id, target_data)
                
                logger.info(f"用户{user_id}成功抢劫用户{target_id}，获得{rob_amount}金币")
                yield event.plain_result(
                    f"🎉 抢劫成功！\n"
                    f"💰 从 {target_data.get('nickname', '未知用户')} 处抢到 {rob_amount} 金币\n"
                    f"💵 你现在的金币：{robber_data['currency']}"
                )
            else:
                # 抢劫失败
                penalty_rate = self.config["rob"]["penalty"]
                penalty_amount = int(robber_data.get("currency", 0) * penalty_rate)
                penalty_amount = max(penalty_amount, 5)  # 最低惩罚5金币
                
                # 更新数据
                robber_data["currency"] -= penalty_amount
                robber_data["lastRobTime"] = int(time.time())
                
                # 保存数据
                self.plugin.save_player_data(group_id, user_id, robber_data)
                
                logger.info(f"用户{user_id}抢劫失败，损失{penalty_amount}金币")
                yield event.plain_result(
                    f"💔 抢劫失败！\n"
                    f"💸 被警察抓住，罚款 {penalty_amount} 金币\n"
                    f"💵 你现在的金币：{robber_data['currency']}"
                )
                
        except Exception as e:
            logger.error(f"抢劫指令执行失败: {e}")
            yield event.plain_result("抢劫失败，请稍后重试")
    
    def check_permission(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有特殊权限（跳过冷却）
        
        Args:
            event: 消息事件
            
        Returns:
            bool: 是否有权限
        """
        user_id = str(event.get_sender_id())
        ignore_users = self.config.get("ignoreCDUsers", [])
        return user_id in [str(uid) for uid in ignore_users]
    
    def get_all_players(self, group_id: str) -> List[str]:
        """获取群组内所有玩家ID列表
        
        Args:
            group_id: 群组ID
            
        Returns:
            List[str]: 玩家ID列表
        """
        players = []
        group_path = self.plugin.get_player_data_path(group_id, "").replace(".json", "")
        group_dir = os.path.dirname(group_path)
        
        if os.path.exists(group_dir):
            for filename in os.listdir(group_dir):
                if filename.endswith(".json") and filename != "backup":
                    user_id = filename[:-5]  # 移除.json后缀
                    players.append(user_id)
        
        return players