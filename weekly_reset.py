"""
每周重置功能模块
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import os
import json
import time
import shutil
from datetime import datetime, timedelta
from typing import Dict, Any, List
import astrbot.api.message_components as Comp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import SlaveMarketPlugin

class WeeklyResetModule:
    def __init__(self, plugin: 'SlaveMarketPlugin'):
        self.plugin = plugin
        self.config = plugin.config
    
    def should_reset(self) -> bool:
        """检查是否应该执行重置"""
        if not self.config["weeklyReset"]["enabled"]:
            return False
        
        reset_time = self.config["weeklyReset"]["resetTime"]
        now = datetime.now()
        
        # 检查是否是重置时间
        if (now.weekday() == reset_time["day"] and 
            now.hour == reset_time["hour"] and 
            now.minute == reset_time["minute"]):
            return True
        
        return False
    
    def get_last_reset_time(self) -> int:
        """获取上次重置时间"""
        reset_info_path = os.path.join(self.plugin.data_path, "last_reset.json")
        if os.path.exists(reset_info_path):
            try:
                with open(reset_info_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("lastResetTime", 0)
            except Exception as e:
                logger.error(f"读取重置时间失败: {e}")
        return 0
    
    def save_last_reset_time(self) -> None:
        """保存本次重置时间"""
        reset_info_path = os.path.join(self.plugin.data_path, "last_reset.json")
        try:
            with open(reset_info_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "lastResetTime": int(time.time()),
                    "resetDate": datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存重置时间失败: {e}")
    
    def backup_rankings(self) -> None:
        """备份排行榜数据"""
        backup_dir = os.path.join(self.plugin.data_path, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(backup_dir, f"rankings_{timestamp}.json")
        
        # 获取所有群组的数据
        all_rankings = {}
        
        player_dir = os.path.join(self.plugin.data_path, "player")
        if os.path.exists(player_dir):
            for group_id in os.listdir(player_dir):
                group_path = os.path.join(player_dir, group_id)
                if os.path.isdir(group_path):
                    players = []
                    for filename in os.listdir(group_path):
                        if filename.endswith(".json") and filename != "backup":
                            user_id = filename[:-5]
                            player_data = self.plugin.get_player_data(group_id, user_id)
                            if player_data:
                                players.append({
                                    "user_id": user_id,
                                    "nickname": player_data.get("nickname", ""),
                                    "currency": player_data.get("currency", 0),
                                    "value": player_data.get("value", 0),
                                    "slaves_count": len(player_data.get("slaves", [])),
                                    "arena": player_data.get("arena", {})
                                })
                    
                    if players:
                        all_rankings[group_id] = {
                            "timestamp": int(time.time()),
                            "date": datetime.now().isoformat(),
                            "players": players
                        }
        
        try:
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(all_rankings, f, ensure_ascii=False, indent=4)
            logger.info(f"排行榜数据已备份到: {backup_file}")
        except Exception as e:
            logger.error(f"备份排行榜数据失败: {e}")
    
    def reset_player_data(self, group_id: str, user_id: str) -> None:
        """重置单个玩家数据"""
        try:
            # 读取原始数据
            original_data = self.plugin.get_player_data(group_id, user_id)
            if not original_data:
                return
            
            # 创建备份
            backup_dir = os.path.join(self.plugin.data_path, "player", group_id, "backup")
            os.makedirs(backup_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = os.path.join(backup_dir, f"{user_id}_{timestamp}.json")
            
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(original_data, f, ensure_ascii=False, indent=4)
            
            # 重置数据
            preserve_data = self.config["weeklyReset"]["preserveData"]
            
            new_data = {
                "user_id": user_id,
                "nickname": original_data.get("nickname", f"用户{user_id}") if preserve_data["nickname"] else f"用户{user_id}",
                "currency": 0,
                "value": preserve_data["basicValue"],
                "slaves": [],
                "master": None,
                "bank": {
                    "balance": 0,
                    "level": 1,
                    "limit": self.config["bank"]["initialLimit"],
                    "lastInterestTime": int(time.time())
                },
                "cooldowns": {},
                "arena": {
                    "tier": "青铜",
                    "points": 0,
                    "wins": 0,
                    "losses": 0
                },
                "lastWorkTime": 0,
                "createdAt": original_data.get("createdAt", int(time.time()))
            }
            
            self.plugin.save_player_data(group_id, user_id, new_data)
            
        except Exception as e:
            logger.error(f"重置玩家 {user_id} 数据失败: {e}")
    
    def perform_weekly_reset(self) -> Dict[str, Any]:
        """执行每周重置"""
        logger.info("开始执行每周重置...")
        
        reset_count = 0
        
        try:
            # 备份排行榜数据
            self.backup_rankings()
            
            # 重置所有群组的玩家数据
            player_dir = os.path.join(self.plugin.data_path, "player")
            if os.path.exists(player_dir):
                for group_id in os.listdir(player_dir):
                    group_path = os.path.join(player_dir, group_id)
                    if os.path.isdir(group_path):
                        for filename in os.listdir(group_path):
                            if filename.endswith(".json") and filename != "backup":
                                user_id = filename[:-5]
                                self.reset_player_data(group_id, user_id)
                                reset_count += 1
            
            # 保存重置时间
            self.save_last_reset_time()
            
            logger.info(f"每周重置完成，共重置 {reset_count} 个玩家数据")
            
            return {
                "success": True,
                "resetCount": reset_count,
                "message": f"每周重置完成，共重置 {reset_count} 个玩家数据"
            }
            
        except Exception as e:
            logger.error(f"每周重置失败: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    @filter.command("奴隶重置状态")
    async def reset_status(self, event: AstrMessageEvent):
        """查看重置状态"""
        last_reset_time = self.get_last_reset_time()
        
        if last_reset_time == 0:
            reply = "📊 每周重置状态\n\n"
            reply += "🔄 状态: 从未执行过重置\n"
            reply += "⏰ 下次重置: 每周一 00:00"
        else:
            last_reset = datetime.fromtimestamp(last_reset_time)
            now = datetime.now()
            
            # 计算下次重置时间
            days_until_monday = (7 - now.weekday()) % 7
            if days_until_monday == 0 and now.hour >= 0:
                days_until_monday = 7
            
            next_reset = now + timedelta(days=days_until_monday)
            next_reset = next_reset.replace(hour=0, minute=0, second=0, microsecond=0)
            
            reply = "📊 每周重置状态\n\n"
            reply += f"🕐 上次重置: {last_reset.strftime('%Y-%m-%d %H:%M:%S')}\n"
            reply += f"⏰ 下次重置: {next_reset.strftime('%Y-%m-%d %H:%M:%S')}\n"
            reply += f"📅 剩余时间: {days_until_monday} 天"
        
        yield event.plain_result(reply)
    
    @filter.command("手动奴隶重置")
    async def manual_reset(self, event: AstrMessageEvent):
        """手动执行重置（管理员功能）"""
        # 这里应该添加管理员权限检查
        # if not await self.is_admin(event.get_sender_id()):
        #     yield event.plain_result("你没有权限执行此操作")
        #     return
        
        result = self.perform_weekly_reset()
        
        if result["success"]:
            yield event.plain_result(f"✅ 手动重置完成！\n{result['message']}")
        else:
            yield event.plain_result(f"❌ 手动重置失败！\n错误: {result.get('error', '未知错误')}")
    
    @filter.command("上周排行榜")
    async def last_week_rankings(self, event: AstrMessageEvent):
        """查看上周排行榜"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        
        # 查找最新的备份文件
        backup_dir = os.path.join(self.plugin.data_path, "backups")
        if not os.path.exists(backup_dir):
            yield event.plain_result("暂无历史排行榜数据")
            return
        
        backup_files = [f for f in os.listdir(backup_dir) if f.startswith("rankings_") and f.endswith(".json")]
        if not backup_files:
            yield event.plain_result("暂无历史排行榜数据")
            return
        
        # 获取最新的备份文件
        backup_files.sort(reverse=True)
        latest_backup = os.path.join(backup_dir, backup_files[0])
        
        try:
            with open(latest_backup, 'r', encoding='utf-8') as f:
                backup_data = json.load(f)
            
            if group_id not in backup_data:
                yield event.plain_result("该群组暂无历史排行榜数据")
                return
            
            group_data = backup_data[group_id]
            players = group_data["players"]
            
            # 构建历史排行榜回复
            reply = "📜 上周排行榜回顾\n\n"
            reply += f"📅 统计时间: {group_data['date']}\n\n"
            
            # 金币排行榜
            currency_ranking = sorted(players, key=lambda x: x["currency"], reverse=True)[:5]
            reply += "💰 金币排行榜 TOP5:\n"
            for i, player in enumerate(currency_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                reply += f"{emoji} {player['nickname']} - {player['currency']:,} 金币\n"
            
            reply += "\n💎 身价排行榜 TOP5:\n"
            value_ranking = sorted(players, key=lambda x: x["value"], reverse=True)[:5]
            for i, player in enumerate(value_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                reply += f"{emoji} {player['nickname']} - {player['value']:,} 金币\n"
            
            reply += "\n👥 奴隶数量排行榜 TOP5:\n"
            slaves_ranking = sorted(players, key=lambda x: x["slaves_count"], reverse=True)[:5]
            for i, player in enumerate(slaves_ranking, 1):
                emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                reply += f"{emoji} {player['nickname']} - {player['slaves_count']} 个奴隶\n"
            
            yield event.plain_result(reply)
            
        except Exception as e:
            logger.error(f"读取历史排行榜数据失败: {e}")
            yield event.plain_result("读取历史排行榜数据失败")