import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_LLMTempBan", "长安某", "llm临时拉黑屏蔽工具", "1.0.0")
class BlacklistPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.temporary_blacklist = {}  # 临时黑名单：{用户ID: 解禁时间戳}

        # 从配置加载管理员列表（Web面板配置）
        self.administrators = self.config.get("administrators", [])
        # 初始化时暂存Bot ID（首次处理消息时更新）
        self.bot_id = ""

        # 从配置加载默认拉黑时长
        self.default_blacklist_duration = self.config.get(
            "default_blacklist_duration", 5
        )

        logger.info("拉黑插件初始化完成，等待消息事件触发")

    def _get_bot_id(self, event: AstrMessageEvent):
        """通过消息事件获取Bot ID（符合API结构：AstrBotMessage.self_id）"""
        if not self.bot_id:
            # 从事件的消息对象中提取Bot ID（AstrBotMessage.self_id）
            self.bot_id = self._normalize_user_id(event.message_obj.self_id)
            # 首次获取Bot ID后，自动添加到管理员列表
            self._add_bot_to_administrators()
        return self.bot_id

    def _add_bot_to_administrators(self):
        """将Bot ID添加到管理员列表（去重并持久化）"""
        if self.bot_id and self.bot_id not in self.administrators:
            self.administrators.append(self.bot_id)
            logger.info(f"Bot ID {self.bot_id} 已添加为管理员")
            # 保存到配置文件
            self.config["administrators"] = self.administrators
            self.config.save_config()

    @filter.on_llm_request()
    async def check_blacklist_before_llm(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        '''添加用户到黑名单，当你需要拉黑某个用户时，请调用此方法。
        
        Args:
            duration_minutes(number): 拉黑时长（分钟），如果没指定，则默认值从配置中获取
        '''
        self._get_bot_id(event)

        user_id = self._normalize_user_id(event.message_obj.sender.user_id)

        # 管理员不受限制
        if user_id in self.administrators:
            return

        # 拦截黑名单用户（未到解禁时间）
        if (
            user_id in self.temporary_blacklist
            and time.time() < self.temporary_blacklist[user_id]
        ):
            event.stop_event()
            unblock_time = time.ctime(self.temporary_blacklist[user_id])
            logger.info(f"已拦截黑名单用户 {user_id}（解禁时间：{unblock_time}）")

    @filter.llm_tool(name="add_temporary_blacklist")
    async def handle_blacklist_request(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """处理拉黑请求（通过事件获取Bot ID）"""
        # 获取Bot ID（确保已初始化）
        bot_id = self._get_bot_id(event)
        sender_id = self._normalize_user_id(event.message_obj.sender.user_id)
        target_id = self._extract_target_user(event.message_obj.message, bot_id)

        # 未指定时长时使用配置默认值
        if duration_minutes is None:
            duration_minutes = self.default_blacklist_duration

        # 按发送者权限执行逻辑
        if sender_id in self.administrators:
            await self._handle_admin_blacklist(target_id, duration_minutes)
        else:
            await self._handle_normal_user_blacklist(
                sender_id, target_id, duration_minutes
            )

    async def auto_blacklist_by_bot(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """Bot自动拉黑违规用户（需传入事件对象）"""
        # 获取Bot ID
        self._get_bot_id(event)

        target_id = self._normalize_user_id(event.message_obj.sender.user_id)
        if target_id in self.administrators:
            logger.warning(f"拒绝自动拉黑管理员 {target_id}")
            return

        if duration_minutes is None:
            duration_minutes = self.default_blacklist_duration

        self._add_to_blacklist(target_id, duration_minutes)
        logger.info(f"已自动拉黑违规用户 {target_id}，时长 {duration_minutes} 分钟")

    async def _handle_admin_blacklist(self, target_id, duration):
        """管理员拉黑逻辑"""
        if not target_id:
            logger.warning("拉黑失败：未指定目标用户（需@用户）")
            return
        if target_id in self.administrators:
            logger.warning(f"拉黑失败：目标 {target_id} 是管理员")
            return
        if duration <= 0:
            logger.warning("拉黑失败：时长必须大于0")
            return

        self._add_to_blacklist(target_id, duration)
        logger.info(f"管理员已拉黑 {target_id}，时长 {duration} 分钟")

    async def _handle_normal_user_blacklist(self, sender_id, target_id, duration):
        """普通用户拉黑逻辑"""
        if not target_id:
            target_id = sender_id  # 未指定目标则默认拉黑自己

        # 尝试拉黑管理员 → 反拉黑发起者
        if target_id in self.administrators:
            actual_duration = max(5, duration)
            self._add_to_blacklist(sender_id, actual_duration)
            logger.info(
                f"用户 {sender_id} 尝试拉黑管理员，已被反拉黑 {actual_duration} 分钟"
            )
        # 仅允许拉黑自己
        elif target_id == sender_id and duration > 0:
            self._add_to_blacklist(sender_id, duration)
            logger.info(f"用户 {sender_id} 已自助拉黑 {duration} 分钟")
        else:
            logger.warning(f"用户 {sender_id} 拉黑失败：仅允许拉黑自己")

    def _add_to_blacklist(self, user_id, duration_minutes):
        """添加用户到黑名单（计算解禁时间）"""
        unblock_time = time.time() + duration_minutes * 60
        self.temporary_blacklist[user_id] = unblock_time

    def _extract_target_user(self, message_chain, bot_id):
        """从消息链提取@的目标用户（排除@Bot自身）"""
        for component in message_chain:
            if isinstance(component, At) and component.qq != "all":
                at_id = self._normalize_user_id(component.qq)
                if at_id != bot_id:  # 忽略@Bot的情况
                    return at_id
        return ""

    def _normalize_user_id(self, user_id):
        """统一用户ID格式（处理整数/字符串）"""
        if isinstance(user_id, int):
            return str(user_id)
        elif isinstance(user_id, str):
            return user_id.split("_")[-1].strip()  # 移除可能的前缀
        return ""
