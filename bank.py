"""
银行功能模块
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .main import SlaveMarketPlugin

class BankModule:
    def __init__(self, plugin: 'SlaveMarketPlugin'):
        self.plugin = plugin
        self.config = plugin.config
    
    @filter.command("存款")
    async def deposit(self, event: AstrMessageEvent, amount: int):
        """存款"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        if amount <= 0:
            yield event.plain_result("存款金额必须大于0")
            return
        
        if data["currency"] < amount:
            yield event.plain_result(f"金币不足！你只有 {data['currency']} 金币")
            return
        
        bank_data = data.get("bank", {})
        current_balance = bank_data.get("balance", 0)
        limit = bank_data.get("limit", self.config["bank"]["initialLimit"])
        
        if current_balance + amount > limit:
            yield event.plain_result(f"超出存款限额！当前限额: {limit} 金币")
            return
        
        # 执行存款
        data["currency"] -= amount
        bank_data["balance"] = current_balance + amount
        data["bank"] = bank_data
        
        self.plugin.save_player_data(group_id, user_id, data)
        
        yield event.plain_result(f"✅ 存款成功！\n💰 存入: {amount} 金币\n🏦 余额: {bank_data['balance']} 金币")
    
    @filter.command("取款")
    async def withdraw(self, event: AstrMessageEvent, amount: int):
        """取款"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        if amount <= 0:
            yield event.plain_result("取款金额必须大于0")
            return
        
        bank_data = data.get("bank", {})
        current_balance = bank_data.get("balance", 0)
        
        if current_balance < amount:
            yield event.plain_result(f"存款不足！你只有 {current_balance} 金币存款")
            return
        
        # 执行取款
        data["currency"] += amount
        bank_data["balance"] = current_balance - amount
        data["bank"] = bank_data
        
        self.plugin.save_player_data(group_id, user_id, data)
        
        yield event.plain_result(f"✅ 取款成功！\n💰 取出: {amount} 金币\n💼 现金: {data['currency']} 金币\n🏦 存款: {bank_data['balance']} 金币")
    
    @filter.command("升级信用")
    async def upgrade_credit(self, event: AstrMessageEvent):
        """升级信用等级"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        bank_data = data.get("bank", {})
        current_level = bank_data.get("level", 1)
        
        # 计算升级价格
        upgrade_price = int(self.config["bank"]["initialUpgradePrice"] * 
                          (self.config["bank"]["upgradePriceMulti"] ** (current_level - 1)))
        
        if data["currency"] < upgrade_price:
            yield event.plain_result(f"金币不足！升级需要 {upgrade_price} 金币，你只有 {data['currency']} 金币")
            return
        
        # 执行升级
        data["currency"] -= upgrade_price
        bank_data["level"] = current_level + 1
        bank_data["limit"] = int(self.config["bank"]["initialLimit"] * 
                               (self.config["bank"]["limitIncreaseMulti"] ** (bank_data["level"] - 1)))
        data["bank"] = bank_data
        
        self.plugin.save_player_data(group_id, user_id, data)
        
        yield event.plain_result(f"✅ 信用等级提升！\n📈 新等级: {bank_data['level']}\n💳 新限额: {bank_data['limit']} 金币\n💰 花费: {upgrade_price} 金币")
    
    @filter.command("领取利息")
    async def collect_interest(self, event: AstrMessageEvent):
        """领取利息"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        bank_data = data.get("bank", {})
        balance = bank_data.get("balance", 0)
        last_interest_time = bank_data.get("lastInterestTime", int(time.time()))
        
        # 计算利息
        current_time = int(time.time())
        hours_passed = min((current_time - last_interest_time) // 3600, 
                          self.config["bank"]["maxInterestTime"])
        
        if hours_passed == 0:
            yield event.plain_result("还没有产生利息，请稍后再来")
            return
        
        interest = int(balance * self.config["bank"]["interestRate"] * hours_passed)
        
        if interest <= 0:
            yield event.plain_result("没有可领取的利息")
            return
        
        # 发放利息
        data["currency"] += interest
        bank_data["lastInterestTime"] = current_time
        data["bank"] = bank_data
        
        self.plugin.save_player_data(group_id, user_id, data)
        
        yield event.plain_result(f"💰 利息领取成功！\n⏰ 计息时间: {hours_passed} 小时\n💸 获得利息: {interest} 金币")