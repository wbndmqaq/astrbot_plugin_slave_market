"""
奴隶管理功能模块
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .main import SlaveMarketPlugin

class SlaveManagementModule:
    def __init__(self, plugin: 'SlaveMarketPlugin'):
        self.plugin = plugin
        self.config = plugin.config
    
    @filter.command("赎身")
    async def buy_back_freedom(self, event: AstrMessageEvent):
        """赎身"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        nickname = event.get_sender_name()
        
        # 确保玩家存在
        data = self.plugin.ensure_player_exists(group_id, user_id, nickname)
        
        # 检查是否有主人
        if not data.get("master"):
            yield event.plain_result("你已经是自由身了，不需要赎身")
            return
        
        # 获取主人信息
        master_data = self.plugin.get_player_data(group_id, str(data["master"]))
        if not master_data:
            yield event.plain_result("赎身失败：主人数据不存在")
            return
        
        # 计算赎身价格（身价的1.5倍）
        buyback_price = int(data["value"] * 1.5)
        
        if data["currency"] < buyback_price:
            yield event.plain_result(f"金币不足！赎身需要 {buyback_price} 金币，你只有 {data['currency']} 金币")
            return
        
        # 检查冷却时间
        if not self.plugin.check_cooldown(data, "buyback", self.config["buyBack"]["cooldown"]):
            remaining = self.config["buyBack"]["cooldown"] - (int(time.time()) - data["cooldowns"]["buyback"])
            yield event.plain_result(f"赎身冷却中，还需等待 {remaining//3600} 小时")
            return
        
        # 执行赎身
        data["currency"] -= buyback_price
        master_data["currency"] += buyback_price
        
        # 从主人的奴隶列表中移除
        if user_id in master_data.get("slaves", []):
            master_data["slaves"].remove(user_id)
        
        # 清除主人关系
        old_master = data["master"]
        data["master"] = None
        
        # 身价降为原来的20%
        old_value = data["value"]
        data["value"] = int(data["value"] * 0.2)
        
        # 设置冷却时间
        self.plugin.set_cooldown(data, "buyback")
        
        # 保存数据
        self.plugin.save_player_data(group_id, user_id, data)
        self.plugin.save_player_data(group_id, str(old_master), master_data)
        
        yield event.plain_result(f"✅ 赎身成功！\n💰 花费: {buyback_price} 金币\n💎 身价变化: {old_value} → {data['value']} 金币\n🎉 你现在自由了！")
    
    @filter.command("放生奴隶")
    async def release_slave(self, event: AstrMessageEvent, target_user: str):
        """放生奴隶"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        master_id = str(event.get_sender_id())
        master_name = event.get_sender_name()
        
        # 确保主人存在
        master_data = self.plugin.ensure_player_exists(group_id, master_id, master_name)
        
        # 解析目标用户ID
        if target_user.startswith("@"):
            target_id = target_user[1:]
        else:
            target_id = target_user
        
        if not target_id or target_id == master_id:
            yield event.plain_result("无法放生自己或无效的目标")
            return
        
        # 检查是否是主人的奴隶
        if target_id not in master_data.get("slaves", []):
            yield event.plain_result("该用户不是你的奴隶")
            return
        
        # 获取奴隶数据
        slave_data = self.plugin.get_player_data(group_id, target_id)
        if not slave_data:
            yield event.plain_result("放生失败：奴隶数据不存在")
            return
        
        # 从主人的奴隶列表中移除
        master_data["slaves"].remove(target_id)
        
        # 清除奴隶的主人关系
        slave_data["master"] = None
        
        # 给予放生奖励（提升好感度，增加奴隶价值）
        value_increase = int(slave_data["value"] * 0.1)  # 增加10%价值
        slave_data["value"] += value_increase
        
        # 保存数据
        self.plugin.save_player_data(group_id, master_id, master_data)
        self.plugin.save_player_data(group_id, target_id, slave_data)
        
        yield event.plain_result(f"🕊️ 放生成功！\n👤 放生对象: {slave_data['nickname']}\n💎 身价提升: {value_increase} 金币\n🎉 {slave_data['nickname']} 现在自由了！")
    
    @filter.command("转让奴隶")
    async def transfer_slave(self, event: AstrMessageEvent, target_user: str, new_owner: str):
        """转让奴隶"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        master_id = str(event.get_sender_id())
        master_name = event.get_sender_name()
        
        # 确保主人存在
        master_data = self.plugin.ensure_player_exists(group_id, master_id, master_name)
        
        # 解析目标用户ID（要转让的奴隶）
        if target_user.startswith("@"):
            slave_id = target_user[1:]
        else:
            slave_id = target_user
        
        # 解析新主人ID
        if new_owner.startswith("@"):
            new_master_id = new_owner[1:]
        else:
            new_master_id = new_owner
        
        # 验证输入
        if not slave_id or not new_master_id:
            yield event.plain_result("参数错误，请指定要转让的奴隶和新主人")
            return
        
        if slave_id == master_id or new_master_id == master_id:
            yield event.plain_result("无法转让给自己")
            return
        
        if slave_id == new_master_id:
            yield event.plain_result("无法将奴隶转让给自己")
            return
        
        # 检查是否是主人的奴隶
        if slave_id not in master_data.get("slaves", []):
            yield event.plain_result("该用户不是你的奴隶")
            return
        
        # 获取奴隶数据
        slave_data = self.plugin.get_player_data(group_id, slave_id)
        if not slave_data:
            yield event.plain_result("转让失败：奴隶数据不存在")
            return
        
        # 确保新主人存在
        new_master_data = self.plugin.ensure_player_exists(group_id, new_master_id, f"用户{new_master_id}")
        
        # 执行转让
        # 从原主人的奴隶列表中移除
        master_data["slaves"].remove(slave_id)
        
        # 添加到新主人的奴隶列表
        if "slaves" not in new_master_data:
            new_master_data["slaves"] = []
        new_master_data["slaves"].append(slave_id)
        
        # 更新奴隶的主人
        slave_data["master"] = new_master_id
        
        # 保存数据
        self.plugin.save_player_data(group_id, master_id, master_data)
        self.plugin.save_player_data(group_id, new_master_id, new_master_data)
        self.plugin.save_player_data(group_id, slave_id, slave_data)
        
        yield event.plain_result(f"🔄 转让成功！\n👤 奴隶: {slave_data['nickname']}\n🏠 新主人: {new_master_data['nickname']}\n🎉 转让完成！")
    
    @filter.command("奴隶详情")
    async def slave_details(self, event: AstrMessageEvent, target_user: str):
        """查看奴隶详情"""
        if not event.get_group_id():
            yield event.plain_result("该游戏只能在群内使用")
            return
        
        group_id = str(event.get_group_id())
        
        # 解析目标用户ID
        if target_user.startswith("@"):
            target_id = target_user[1:]
        else:
            target_id = target_user
        
        if not target_id:
            yield event.plain_result("请指定要查看的用户")
            return
        
        # 获取用户数据
        target_data = self.plugin.get_player_data(group_id, target_id)
        if not target_data:
            yield event.plain_result("用户数据不存在")
            return
        
        # 构建详细信息
        reply = f"📋 {target_data.get('nickname', '未知用户')} 的详细信息\n\n"
        reply += f"💰 金币: {target_data.get('currency', 0)}\n"
        reply += f"💎 身价: {target_data.get('value', 0)}\n"
        reply += f"👥 奴隶数量: {len(target_data.get('slaves', []))}\n"
        
        # 段位信息
        arena_data = target_data.get("arena", {})
        reply += f"🏆 段位: {arena_data.get('tier', '青铜')}\n"
        reply += f"⭐ 积分: {arena_data.get('points', 0)}\n"
        reply += f"📊 战绩: {arena_data.get('wins', 0)}胜 {arena_data.get('losses', 0)}败\n"
        
        # 银行信息
        bank_data = target_data.get("bank", {})
        reply += f"🏦 银行存款: {bank_data.get('balance', 0)}\n"
        reply += f"💳 信用等级: {bank_data.get('level', 1)}\n"
        
        # 主人信息
        if target_data.get("master"):
            master_data = self.plugin.get_player_data(group_id, str(target_data["master"]))
            if master_data:
                reply += f"🔗 主人: {master_data.get('nickname', '未知')}\n"
        
        # 奴隶列表
        if target_data.get("slaves"):
            reply += "\n👥 拥有的奴隶:\n"
            for slave_id in target_data["slaves"]:
                slave_data = self.plugin.get_player_data(group_id, str(slave_id))
                if slave_data:
                    reply += f"  • {slave_data.get('nickname', '未知')} (身价: {slave_data.get('value', 0)})\n"
        
        yield event.plain_result(reply)