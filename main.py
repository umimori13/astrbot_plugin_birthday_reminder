import asyncio
import datetime
import json

import croniter
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import \
    AiocqhttpMessageEvent
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType


class FriendBirthdayPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ---------- 读取配置 ----------
        # 私聊提醒目标：直接填 QQ 号列表
        self.notify_private_ids: list[str] = [
            str(x) for x in self.config.get("notify_private_ids", [])
        ]
        # 群聊提醒目标：直接填群号列表
        self.notify_group_ids: list[str] = [
            str(x) for x in self.config.get("notify_group_ids", [])
        ]
        # 提前几天发送提醒（0 表示仅当天提醒）
        self.advance_days: int = int(self.config.get("advance_days", 2))
        # 每天检查的时间，24h 格式，例如 "8:00"
        self.check_time: str = self.config.get("check_time", "8:00")

        # AstrBot 插件配置文件路径（用于持久化写回）
        self._config_file = StarTools.get_data_dir() / "friends_birthday.json"

        # ---------- 内部状态 ----------
        self._fetch_lock = asyncio.Lock()
        self._need_initial_fetch: bool = len(self.config.get("friends_birthday", [])) == 0
        self._fetching: bool = False
        self._daily_task: asyncio.Task | None = None

    async def initialize(self):
        if self._need_initial_fetch:
            logger.info(
                "[FriendBirthday] 好友生日数据文件不存在，"
                "将在收到第一个 AIOCQHTTP 事件时自动获取。"
            )
        else:
            data = self._load_data()
            valid = sum(
                1 for f in data if f.get("birthday_month") and f.get("birthday_day")
            )
            logger.info(
                f"[FriendBirthday] 已加载好友生日数据：{len(data)} 位好友，"
                f"其中 {valid} 位有生日信息。"
            )

        # 在 initialize 中启动定时任务，确保初始化完成后再运行
        self._daily_task = asyncio.create_task(self.daily_task())

    # ------------------------------------------------------------------
    # 首次事件触发 —— 当数据文件不存在时，利用第一个收到的 AIOCQHTTP 事件
    # 的 bot 客户端来拉取好友列表和生日信息
    # ------------------------------------------------------------------

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def _auto_fetch_trigger(self, event: AiocqhttpMessageEvent):
        """收到任意 AIOCQHTTP 事件时，若数据文件不存在则触发一次自动获取。"""
        if self._need_initial_fetch and not self._fetching:
            self._need_initial_fetch = False
            self._fetching = True
            asyncio.create_task(self.fetch_friends_birthday(event.bot))

    # ------------------------------------------------------------------
    # 核心：获取好友列表及生日信息
    # ------------------------------------------------------------------

    async def fetch_friends_birthday(self, bot) -> None:
        """通过 OneBot API 获取所有好友的生日信息并写入 JSON 文件。"""
        async with self._fetch_lock:
            try:
                logger.info("[FriendBirthday] 开始获取好友列表...")
                friend_list: list[dict] = await bot.get_friend_list()
                logger.info(
                    f"[FriendBirthday] 共 {len(friend_list)} 位好友，"
                    "开始逐一获取生日信息（可能需要较长时间）..."
                )

                friends_data: list[dict] = []
                for friend in friend_list:
                    qq = str(friend.get("user_id", ""))
                    nickname = (
                        friend.get("remark")
                        or friend.get("nickname")
                        or qq
                    )
                    if not qq:
                        continue
                    try:
                        info: dict = await bot.get_stranger_info(
                            user_id=int(qq), no_cache=True
                        )
                        bday_year = info.get("birthday_year")
                        bday_month = info.get("birthday_month")
                        bday_day = info.get("birthday_day")
                    except Exception as e:
                        logger.warning(
                            f"[FriendBirthday] 获取 {nickname}({qq}) 生日失败: {e}"
                        )
                        bday_year = bday_month = bday_day = None

                    friends_data.append(
                        {
                            "qq": qq,
                            "nickname": nickname,
                            "birthday_year": bday_year,
                            "birthday_month": bday_month,
                            "birthday_day": bday_day,
                        }
                    )

                self._save_data(friends_data)
                valid = sum(
                    1 for f in friends_data if f.get("birthday_month") and f.get("birthday_day")
                )
                logger.info(
                    f"[FriendBirthday] 好友生日数据已保存至插件配置，"
                    f"共 {len(friends_data)} 位好友，其中 {valid} 位有生日信息。"
                )
            except (OSError, ConnectionError, TimeoutError) as e:
                logger.error(f"[FriendBirthday] 获取好友生日数据失败（网络/IO）: {e}")
            except Exception as e:
                logger.error(f"[FriendBirthday] 获取好友生日数据时发生意外错误: {e}", exc_info=True)
            finally:
                self._fetching = False

    # ------------------------------------------------------------------
    # 平台 SID 解析
    # ------------------------------------------------------------------

    def _resolve_full_umo(self, target_id: str, msg_type: str) -> str:
        """
        动态推算完整的 UMO (Unified Message Origin)。
        从当前正在运行的平台实例中选取首个健康实例拼装 SID，
        避免用户手动填写 aiocqhttp:PrivateMessage:xxx 这种格式。
        """
        active_insts = {
            p.meta().id: p
            for p in self.context.platform_manager.get_insts()
            if p.meta().id and "webchat" not in p.meta().id.lower()
        }

        # 优先选取运行中的平台
        running = [
            p for p in active_insts.values() if p.status == PlatformStatus.RUNNING
        ]
        if running:
            return f"{running[0].meta().id}:{msg_type}:{target_id}"

        # 保底：取第一个已知平台或 default
        fallback = list(active_insts.keys())[0] if active_insts else "default"
        return f"{fallback}:{msg_type}:{target_id}"

    # ------------------------------------------------------------------
    # 数据读写
    # ------------------------------------------------------------------

    def _load_data(self) -> list[dict]:
        """从插件配置中读取好友生日列表。"""
        return list(self.config.get("friends_birthday", []))

    def _save_data(self, data: list[dict]) -> None:
        """将好友生日列表写回插件配置并持久化到磁盘。"""
        # 先持久化到磁盘，成功后再更新内存，避免不一致
        try:
            existing: dict = {}
            if self._config_file.exists():
                try:
                    with open(self._config_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except json.JSONDecodeError as e:
                    logger.warning(f"[FriendBirthday] 配置文件 JSON 解析失败，将覆盖写入: {e}")
            existing["friends_birthday"] = data
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=4)
        except OSError as e:
            logger.error(f"[FriendBirthday] 写入配置文件失败: {e}")
            return
        # 磁盘写入成功，更新内存中的配置
        self.config["friends_birthday"] = data

    # ------------------------------------------------------------------
    # 生日查询逻辑
    # ------------------------------------------------------------------

    def _birthdays_on(
        self,
        target_date: datetime.date,
        data: list[dict] | None = None,
    ) -> list[dict]:
        """返回在指定日期（忽略年份）生日的好友列表。

        Args:
            target_date: 要匹配的日期（仅看月/日）。
            data: 可选的好友数据列表，传入可避免重复调用 _load_data。
        """
        if data is None:
            data = self._load_data()
        result = []
        for friend in data:
            month = friend.get("birthday_month")
            day = friend.get("birthday_day")
            if not month or not day:
                continue
            try:
                if int(month) == target_date.month and int(day) == target_date.day:
                    result.append(friend)
            except (ValueError, TypeError):
                continue
        return result

    def _today_birthdays(self, data: list[dict] | None = None) -> list[dict]:
        return self._birthdays_on(datetime.date.today(), data=data)

    def _advance_birthdays(self, data: list[dict] | None = None) -> list[dict]:
        """返回 advance_days 天后生日的好友列表。"""
        if self.advance_days <= 0:
            return []
        target = datetime.date.today() + datetime.timedelta(days=self.advance_days)
        return self._birthdays_on(target, data=data)

    # ------------------------------------------------------------------
    # 发送提醒
    # ------------------------------------------------------------------

    async def _send_reminder(self, friends: list[dict], days_ahead: int) -> None:
        """向所有配置的目标（私聊 + 群聊）发送生日提醒。"""
        all_targets = [
            (uid, "FriendMessage") for uid in self.notify_private_ids
        ] + [
            (gid, "GroupMessage") for gid in self.notify_group_ids
        ]
        if not friends or not all_targets:
            return

        for friend in friends:
            name = friend.get("nickname") or friend.get("qq")
            qq = friend.get("qq")
            bday_month = friend.get("birthday_month")
            bday_day = friend.get("birthday_day")
            try:
                date_str = (
                    f"{int(bday_month):02d}月{int(bday_day):02d}日"
                    if bday_month and bday_day
                    else "未知日期"
                )
            except (ValueError, TypeError):
                date_str = "未知日期"

            if days_ahead == 0:
                text = (
                    f"🎂 今天是 {name}（QQ: {qq}）的生日！\n"
                    f"记得送上祝福哦！🎉"
                )
            else:
                text = (
                    f"🔔 生日提醒：{name}（QQ: {qq}）\n"
                    f"将在 {days_ahead} 天后（{date_str}）过生日，\n"
                    f"别忘了送上祝福！"
                )

            for raw_id, msg_type in all_targets:
                session_id = self._resolve_full_umo(raw_id, msg_type)
                try:
                    chain = MessageChain().message(text)
                    await self.context.send_message(session_id, chain)
                    logger.info(
                        f"[FriendBirthday] 已向 {session_id} 发送提醒: {name}（{qq}）"
                    )
                except Exception as e:
                    logger.error(
                        f"[FriendBirthday] 向 {session_id} 发送提醒失败: {e}"
                    )

    # ------------------------------------------------------------------
    # 每日定时任务
    # ------------------------------------------------------------------

    async def daily_task(self) -> None:
        """每天在配置的时间检查生日并发送提醒。"""
        try:
            hour, minute = map(int, self.check_time.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"时间超出范围: {hour}:{minute}")
        except ValueError:
            logger.error(
                f"[FriendBirthday] check_time 格式错误 '{self.check_time}'，使用默认 8:00"
            )
            hour, minute = 8, 0

        cron_expr = f"{minute} {hour} * * *"
        cron = croniter.croniter(cron_expr, datetime.datetime.now())

        while True:
            try:
                next_run = cron.get_next(datetime.datetime)
                now = datetime.datetime.now()
                sleep_sec = (next_run - now).total_seconds()
                logger.info(
                    f"[FriendBirthday] 下次生日检查时间: {next_run}，"
                    f"等待 {sleep_sec:.0f} 秒"
                )
                await asyncio.sleep(max(0, sleep_sec))
                await self._run_birthday_check()
                # 等待 60 秒防止在同一分钟内重复触发
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[FriendBirthday] 每日任务出错: {e}")
                await asyncio.sleep(300)

    async def _run_birthday_check(self) -> None:
        """执行一次完整的生日检查（当天 + 提前 N 天）并发送提醒。"""
        today_list = self._today_birthdays()
        advance_list = self._advance_birthdays()

        if today_list:
            await self._send_reminder(today_list, 0)
        if advance_list:
            await self._send_reminder(advance_list, self.advance_days)

        if not today_list and not advance_list:
            logger.debug("[FriendBirthday] 今日检查完成，无生日提醒。")

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("生日刷新")
    async def cmd_refresh(self, event: AstrMessageEvent):
        """重新从 QQ 拉取所有好友的生日信息并更新本地 JSON"""
        if self._fetching:
            yield event.plain_result("⏳ 正在获取中，请稍候...")
            return

        # 尝试从当前事件获取 bot 客户端
        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result(
                "❌ 当前平台不支持此操作，请在 AIOCQHTTP（NapCat/LLOneBot）环境下使用。"
            )
            return

        self._fetching = True
        asyncio.create_task(self.fetch_friends_birthday(bot))
        yield event.plain_result(
            "🔄 已在后台开始刷新好友生日数据，完成后将自动写入插件配置。\n"
            f"可在 AstrBot 配置面板 → 插件配置 → 本插件 → friends_birthday 中查看和编辑。"
        )

    @filter.command("生日列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """查看未来 7 天内有生日的好友"""
        data = self._load_data()
        if not data:
            yield event.plain_result(
                "📭 暂无好友生日数据，请先使用 /生日刷新 获取。"
            )
            return

        today = datetime.date.today()
        lines: list[str] = []

        # 一次加载，复用数据
        cached_data = data

        # 今天生日
        today_list = self._birthdays_on(today, data=cached_data)
        for f in today_list:
            lines.append(f"🎂 今天：{f.get('nickname')}（{f.get('qq')}）")

        # 未来 7 天
        for days in range(1, 8):
            target = today + datetime.timedelta(days=days)
            for f in self._birthdays_on(target, data=cached_data):
                lines.append(
                    f"📅 {target.strftime('%m/%d')}（{days}天后）："
                    f"{f.get('nickname')}（{f.get('qq')}）"
                )

        if lines:
            yield event.plain_result("🎉 近 7 天好友生日：\n" + "\n".join(lines))
        else:
            yield event.plain_result("📭 近 7 天内无好友生日。")

    @filter.command("生日检查")
    async def cmd_check(self, event: AstrMessageEvent):
        """立即执行一次生日检查，并向配置的目标发送提醒"""
        today_list = self._today_birthdays()
        advance_list = self._advance_birthdays()

        if not today_list and not advance_list:
            yield event.plain_result(
                f"✅ 检查完成：今天及 {self.advance_days} 天后均无好友生日。"
            )
            return

        await self._run_birthday_check()
        total = len(today_list) + len(advance_list)
        total_targets = len(self.notify_private_ids) + len(self.notify_group_ids)
        yield event.plain_result(
            f"✅ 检查完成，已发送 {total} 条生日提醒至 {total_targets} 个目标。"
        )

    @filter.command("生日状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件当前配置与数据状态"""
        data = self._load_data()
        valid = sum(
            1 for f in data if f.get("birthday_month") and f.get("birthday_day")
        )
        private_str = (
            "、".join(self.notify_private_ids) if self.notify_private_ids else "（未配置）"
        )
        group_str = (
            "、".join(self.notify_group_ids) if self.notify_group_ids else "（未配置）"
        )
        status = (
            f"📊 好友生日提醒插件状态\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💾 数据存储：插件配置（{self._config_file}）\n"
            f"👥 好友总数：{len(data)}，含生日：{valid}\n"
            f"⏰ 每日检查时间：{self.check_time}\n"
            f"📅 提前提醒天数：{self.advance_days} 天\n"
            f"📨 私聊提醒目标：{private_str}\n"
            f"📨 群聊提醒目标：{group_str}"
        )
        yield event.plain_result(status)

    async def terminate(self):
        """插件卸载时取消后台任务。"""
        if self._daily_task is not None:
            self._daily_task.cancel()
            try:
                await self._daily_task
            except asyncio.CancelledError:
                pass
