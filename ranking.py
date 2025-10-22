"""
排行榜功能模块
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import os
import json
from typing import Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
    from .main import SlaveMarketPlugin

class RankingModule:
    def __init__(self, plugin: 'SlaveMarketPlugin'):
        self.plugin = plugin
    
    def get_all_players(self, group_id: str) -> List[Dict[str, Any]]:
        """获取群组内所有玩家数据"""
        players = []
        group_path = os.path.join(self.plugin.data_path, "player", group_id)
        
        if os.path.exists(group_path):
            for filename in os.listdir(group_path):
                if filename.endswith(".json"):
                    user_id = filename[:-5]  # 移除.json后缀
                    player_data = self.plugin.get_player_data(group_id, user_id)
                    if player_data:
                        players.append(player_data)
        
        return players
    
    @filter.command("排行榜")
    async def show_rankings(self, event: AstrMessageEvent):
        """显示排行榜"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        players = self.get_all_players(group_id)
        
        if not players:
            yield event.plain_result("暂无玩家数据")
            return
        
        # 金币排行榜
        currency_ranking = sorted(players, key=lambda x: x.get("currency", 0), reverse=True)[:10]
        
        # 身价排行榜
        value_ranking = sorted(players, key=lambda x: x.get("value", 0), reverse=True)[:10]
        
        # 奴隶数量排行榜
        slaves_ranking = sorted(players, key=lambda x: len(x.get("slaves", [])), reverse=True)[:10]
        
        # 构建回复消息
        reply = "🏆 奴隶市场排行榜 🏆\n\n"
        
        # 金币排行榜
        reply += "💰 金币排行榜:\n"
        for i, player in enumerate(currency_ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            reply += f"{emoji} {player.get('nickname', '未知')} - {player.get('currency', 0)} 金币\n"
        
        reply += "\n💎 身价排行榜:\n"
        for i, player in enumerate(value_ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            reply += f"{emoji} {player.get('nickname', '未知')} - {player.get('value', 0)} 金币\n"
        
        reply += "\n👥 奴隶数量排行榜:\n"
        for i, player in enumerate(slaves_ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            slave_count = len(player.get("slaves", []))
            reply += f"{emoji} {player.get('nickname', '未知')} - {slave_count} 个奴隶\n"
        
        yield event.plain_result(reply)
    
    @filter.command("金币排行")
    async def currency_ranking(self, event: AstrMessageEvent):
        """金币排行榜"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        players = self.get_all_players(group_id)
        
        if not players:
            yield event.plain_result("暂无玩家数据")
            return
        
        # 按金币排序
        ranking = sorted(players, key=lambda x: x.get("currency", 0), reverse=True)[:10]
        
        reply = "💰 金币排行榜 TOP10 💰\n\n"
        for i, player in enumerate(ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:2d}."
            nickname = player.get('nickname', '未知')
            currency = player.get('currency', 0)
            reply += f"{emoji} {nickname} - {currency:,} 金币\n"
        
        yield event.plain_result(reply)
    
    @filter.command("身价排行")
    async def value_ranking(self, event: AstrMessageEvent):
        """身价排行榜"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        players = self.get_all_players(group_id)
        
        if not players:
            yield event.plain_result("暂无玩家数据")
            return
        
        # 按身价排序
        ranking = sorted(players, key=lambda x: x.get("value", 0), reverse=True)[:10]
        
        reply = "💎 身价排行榜 TOP10 💎\n\n"
        for i, player in enumerate(ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:2d}."
            nickname = player.get('nickname', '未知')
            value = player.get('value', 0)
            reply += f"{emoji} {nickname} - {value:,} 金币\n"
        
        yield event.plain_result(reply)
    
    @filter.command("奴隶排行")
    async def slaves_ranking(self, event: AstrMessageEvent):
        """奴隶数量排行榜"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        players = self.get_all_players(group_id)
        
        if not players:
            yield event.plain_result("暂无玩家数据")
            return
        
        # 按奴隶数量排序
        ranking = sorted(players, key=lambda x: len(x.get("slaves", [])), reverse=True)[:10]
        
        reply = "👥 奴隶数量排行榜 TOP10 👥\n\n"
        for i, player in enumerate(ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:2d}."
            nickname = player.get('nickname', '未知')
            slave_count = len(player.get("slaves", []))
            reply += f"{emoji} {nickname} - {slave_count} 个奴隶\n"
        
        yield event.plain_result(reply)
    
    @filter.command("段位排行")
    async def tier_ranking(self, event: AstrMessageEvent):
        """段位排行榜"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        players = self.get_all_players(group_id)
        
        if not players:
            yield event.plain_result("暂无玩家数据")
            return
        
        # 按积分排序
        ranking = sorted(players, key=lambda x: x.get("arena", {}).get("points", 0), reverse=True)[:10]
        
        reply = "🏆 段位排行榜 TOP10 🏆\n\n"
        for i, player in enumerate(ranking, 1):
            emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:2d}."
            nickname = player.get('nickname', '未知')
            arena_data = player.get("arena", {})
            tier = arena_data.get("tier", "青铜")
            points = arena_data.get("points", 0)
            wins = arena_data.get("wins", 0)
            losses = arena_data.get("losses", 0)
            
            reply += f"{emoji} {nickname} - {tier} ({points}分) {wins}胜{losses}败\n"
        
        yield event.plain_result(reply)