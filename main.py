import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_LLMTempBan", "长安某", "llm临时拉黑屏蔽工具", "1.0.2")
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
        logger.info(f"初始管理员列表: {self.administrators}")
        logger.info(f"默认拉黑时长: {self.default_blacklist_duration} 分钟")

    def _get_bot_id(self, event: AstrMessageEvent):
        """通过消息事件获取Bot ID（符合API结构：AstrBotMessage.self_id）"""
        if not self.bot_id:
            # 从事件的消息对象中提取Bot ID（AstrBotMessage.self_id）
            raw_bot_id = event.message_obj.self_id
            self.bot_id = self._normalize_user_id(raw_bot_id)
            logger.info(f"获取到Bot ID: 原始={raw_bot_id}, 规范化后={self.bot_id}")
            # 首次获取Bot ID后，自动添加到管理员列表
            self._add_bot_to_administrators()
        return self.bot_id

    def _add_bot_to_administrators(self):
        """将Bot ID添加到管理员列表（去重并持久化）"""
        if self.bot_id and self.bot_id not in self.administrators:
            self.administrators.append(self.bot_id)
            logger.info(f"Bot ID {self.bot_id} 已添加为管理员，更新后管理员列表: {self.administrators}")
            # 保存到配置文件
            self.config["administrators"] = self.administrators
            self.config.save_config()
        elif self.bot_id:
            logger.info(f"Bot ID {self.bot_id} 已在管理员列表中")

    @filter.on_llm_request()
    async def check_blacklist_before_llm(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        '''拦截黑名单用户的LLM请求'''
        self._get_bot_id(event)

        raw_user_id = event.message_obj.sender.user_id
        user_id = self._normalize_user_id(raw_user_id)
        logger.debug(f"检查用户LLM请求权限: 原始ID={raw_user_id}, 规范化ID={user_id}")

        # 管理员不受限制
        if user_id in self.administrators:
            logger.debug(f"用户 {user_id} 是管理员，允许LLM请求")
            return

        # 拦截黑名单用户（未到解禁时间）
        if user_id in self.temporary_blacklist:
            unblock_time = self.temporary_blacklist[user_id]
            current_time = time.time()
            if current_time < unblock_time:
                event.stop_event()
                logger.info(f"已拦截黑名单用户 {user_id} 的LLM请求（解禁时间：{time.ctime(unblock_time)}）")
            else:
                # 自动移除已过期的黑名单记录
                del self.temporary_blacklist[user_id]
                logger.info(f"用户 {user_id} 的拉黑已过期，自动移除黑名单")

    @filter.llm_tool(name="add_temporary_blacklist")
    async def handle_blacklist_request(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """处理拉黑请求（通过事件获取Bot ID）"""
        logger.info("收到拉黑请求，开始处理...")
        # 获取Bot ID（确保已初始化）
        bot_id = self._get_bot_id(event)
        
        # 解析发送者ID
        raw_sender_id = event.message_obj.sender.user_id
        sender_id = self._normalize_user_id(raw_sender_id)
        logger.info(f"拉黑请求发送者: 原始ID={raw_sender_id}, 规范化ID={sender_id}")
        
        # 解析目标用户ID
        target_id = self._extract_target_user(event.message_obj.message, bot_id)
        logger.info(f"拉黑请求目标用户: {target_id if target_id else '未指定'}")
        
        # 处理时长
        if duration_minutes is None:
            duration_minutes = self.default_blacklist_duration
            logger.info(f"未指定拉黑时长，使用默认值: {duration_minutes} 分钟")
        else:
            logger.info(f"指定拉黑时长: {duration_minutes} 分钟")

        # 按发送者权限执行逻辑
        if sender_id in self.administrators:
            logger.info(f"发送者 {sender_id} 是管理员，执行管理员拉黑逻辑")
            await self._handle_admin_blacklist(target_id, duration_minutes)
        else:
            logger.info(f"发送者 {sender_id} 是普通用户，执行普通用户拉黑逻辑")
            await self._handle_normal_user_blacklist(
                sender_id, target_id, duration_minutes
            )

    async def auto_blacklist_by_bot(
        self, event: AstrMessageEvent, duration_minutes: int = None
    ):
        """Bot自动拉黑违规用户（需传入事件对象）"""
        logger.info("触发Bot自动拉黑逻辑...")
        # 获取Bot ID
        self._get_bot_id(event)

        raw_target_id = event.message_obj.sender.user_id
        target_id = self._normalize_user_id(raw_target_id)
        logger.info(f"自动拉黑目标用户: 原始ID={raw_target_id}, 规范化ID={target_id}")

        if target_id in self.administrators:
            logger.warning(f"拒绝自动拉黑管理员 {target_id}（管理员不受自动拉黑限制）")
            return

        if duration_minutes is None:
            duration_minutes = self.default_blacklist_duration
            logger.info(f"未指定自动拉黑时长，使用默认值: {duration_minutes} 分钟")

        self._add_to_blacklist(target_id, duration_minutes)
        logger.info(f"已自动拉黑违规用户 {target_id}，时长 {duration_minutes} 分钟（解禁时间：{time.ctime(self.temporary_blacklist[target_id])}）")

    async def _handle_admin_blacklist(self, target_id, duration):
        """管理员拉黑逻辑"""
        # 校验目标用户
        if not target_id:
            logger.warning("管理员拉黑失败：未指定目标用户（需@用户）")
            return
        
        # 校验目标是否为管理员
        if target_id in self.administrators:
            logger.warning(f"管理员拉黑失败：目标用户 {target_id} 是管理员（不能拉黑管理员）")
            return
        
        # 校验时长
        if duration <= 0:
            logger.warning(f"管理员拉黑失败：时长 {duration} 分钟无效（必须大于0）")
            return

        # 执行拉黑
        self._add_to_blacklist(target_id, duration)
        logger.info(f"管理员操作成功：用户 {target_id} 已被拉黑 {duration} 分钟（解禁时间：{time.ctime(self.temporary_blacklist[target_id])}）")

    async def _handle_normal_user_blacklist(self, sender_id, target_id, duration):
        """普通用户拉黑逻辑"""
        # 未指定目标时默认拉黑自己
        if not target_id:
            target_id = sender_id
            logger.info(f"普通用户 {sender_id} 未指定拉黑目标，默认处理为拉黑自己")

        # 校验时长
        if duration <= 0:
            logger.warning(f"普通用户 {sender_id} 拉黑失败：时长 {duration} 分钟无效（必须大于0）")
            return

        # 尝试拉黑管理员 → 反拉黑发起者
        if target_id in self.administrators:
            actual_duration = max(5, duration)  # 反拉黑时长至少5分钟
            self._add_to_blacklist(sender_id, actual_duration)
            logger.info(
                f"普通用户 {sender_id} 尝试拉黑管理员 {target_id}，已被反拉黑 {actual_duration} 分钟（解禁时间：{time.ctime(self.temporary_blacklist[sender_id])}）"
            )
        # 仅允许拉黑自己
        elif target_id == sender_id:
            self._add_to_blacklist(sender_id, duration)
            logger.info(f"普通用户自助拉黑成功：{sender_id} 已拉黑自己 {duration} 分钟（解禁时间：{time.ctime(self.temporary_blacklist[sender_id])}）")
        else:
            logger.warning(f"普通用户 {sender_id} 拉黑失败：仅允许拉黑自己（尝试拉黑他人 {target_id} 被拒绝）")

    def _add_to_blacklist(self, user_id, duration_minutes):
        """添加用户到黑名单（计算解禁时间）"""
        unblock_time = time.time() + duration_minutes * 60
        self.temporary_blacklist[user_id] = unblock_time
        logger.debug(f"黑名单更新：{user_id} → 解禁时间戳={unblock_time}")

    def _extract_target_user(self, message_chain, bot_id):
        """从消息链提取@的目标用户（排除@Bot自身）"""
        logger.debug("开始从消息链提取目标用户...")
        for component in message_chain:
            if isinstance(component, At):
                logger.debug(f"发现@组件：qq={component.qq}")
                if component.qq == "all":
                    logger.debug("跳过@全体成员")
                    continue
                at_id = self._normalize_user_id(component.qq)
                if at_id != bot_id:  # 忽略@Bot的情况
                    logger.debug(f"提取到目标用户：{at_id}（排除Bot自身 {bot_id}）")
                    return at_id
        logger.debug("未从消息链中提取到有效目标用户（未@任何人或仅@了Bot）")
        return ""

    def _normalize_user_id(self, user_id):
        """统一用户ID格式（处理整数/字符串）"""
        original = user_id
        if isinstance(user_id, int):
            normalized = str(user_id)
        elif isinstance(user_id, str):
            # 移除可能的前缀（如"qq_"）
            normalized = user_id.split("_")[-1].strip()
        else:
            normalized = str(user_id)
        logger.debug(f"用户ID规范化：原始={original} → 规范化后={normalized}")
        return normalized
