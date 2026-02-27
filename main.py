import asyncio
import datetime
import json
import time
import zoneinfo

import aiofiles
import aiofiles.os as aio_os
import croniter
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import \
    AiocqhttpMessageEvent
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

# 并发拉取参数：同时进行的最大请求数
_FETCH_CONCURRENCY: int = 5
# 每条请求完成后的最小间隔（秒），避免服务端频繁接口返回 null
_FETCH_DELAY: float = 0.3
# 单条请求失败后的重试等待序列（秒）：第1次失败等300s，第2次失败等1800s
_FETCH_RETRY_WAITS: tuple[float, ...] = (300, 1800)
# 高成本指令（刷新/检查）的触发冷却时间（秒），防止频繁调用
_CMD_COOLDOWN: float = 60.0


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
        try:
            self.advance_days: int = int(self.config.get("advance_days", 2))
        except (ValueError, TypeError):
            logger.warning(
                f"[FriendBirthday] advance_days 配置值 "
                f"'{self.config.get('advance_days')}' 无效，使用默认值 2。"
            )
            self.advance_days = 2
        # 每天检查的时间，24h 格式，例如 "8:00"
        self.check_time: str = self.config.get("check_time", "8:00")

        # AstrBot 插件配置文件路径（用于持久化写回）
        self._config_file = StarTools.get_data_dir() / "friends_birthday.json"

        # ---------- 内部状态 ----------
        self._fetch_lock = asyncio.Lock()
        self._need_initial_fetch: bool = len(self.config.get("friends_birthday", [])) == 0
        self._fetching: bool = False
        self._daily_task: asyncio.Task | None = None
        # 托管所有后台任务，确保插件卸载时能统一回收
        self._bg_tasks: set[asyncio.Task] = set()
        # 高成本指令的上次触发时间戳，用于冷却控制
        self._last_cmd_time: float = 0.0
        # 文件写入锁（在 initialize 中创建，asyncio.Lock 必须在事件循环内创建）
        self._data_lock: asyncio.Lock | None = None
        # 时区（从 AstrBot 全局配置读取，用于定时任务调度）
        self._timezone: zoneinfo.ZoneInfo | None = None

    async def initialize(self):
        self._data_lock = asyncio.Lock()

        # 加载时区配置，确保定时任务调度时间与用户时区一致
        try:
            self._timezone = zoneinfo.ZoneInfo(self.context.get_config().get("timezone"))
        except (zoneinfo.ZoneInfoNotFoundError, TypeError, KeyError, ValueError) as e:
            logger.warning(
                f"[FriendBirthday] 时区配置无效或未配置 ({e})，将使用服务器系统时区。"
            )
            self._timezone = None

        # 若内存配置为空，尝试从 JSON 文件回灌数据（兼容手动编辑 JSON 的场景）
        if self._need_initial_fetch and await aio_os.path.exists(self._config_file):
            try:
                async with aiofiles.open(self._config_file, "r", encoding="utf-8") as f:
                    content = await f.read()
                file_data = await asyncio.to_thread(json.loads, content)
                saved: list = file_data.get("friends_birthday", [])
                if saved:
                    deduped, removed = self._deduplicate_data(saved)
                    if removed:
                        logger.warning(
                            f"[FriendBirthday] 检测到 {removed} 条重复 QQ 记录，"
                            "已自动去重并将回写文件。"
                        )
                        await self._save_data(deduped)
                    else:
                        self.config["friends_birthday"] = deduped
                    self._need_initial_fetch = False
                    logger.info(
                        f"[FriendBirthday] 从 JSON 文件回灌 {len(deduped)} 条生日数据至配置。"
                    )
            except (OSError, json.JSONDecodeError) as e:
                logger.warning(f"[FriendBirthday] 读取 JSON 文件失败，将重新拉取: {e}")

        if self._need_initial_fetch:
            logger.info(
                "[FriendBirthday] 配置中暂无好友生日数据，"
                "将在收到第一个 AIOCQHTTP 事件时自动获取。"
            )
        else:
            data = self._load_data()
            # 若原始配置中存在字符串形式的条目（AstrBot UI 粘贴导入的副作用），
            # 则将解析还原后的结果回写，修正文件格式，避免每次重启都触发解析警告
            raw_entries: list = list(self.config.get("friends_birthday", []))
            has_str_entries = any(isinstance(e, str) for e in raw_entries)
            if has_str_entries:
                logger.info("[FriendBirthday] 检测到字符串条目，正在自动修正并回写文件...")
                await self._save_data(data)
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
        """收到任意 AIOCQHTTP 事件时，若配置中无生日数据则触发一次自动获取。"""
        if self._need_initial_fetch and not self._fetching:
            # 注意：_need_initial_fetch 不在此处置 False，
            # 只有拉取成功后才置 False，失败时保留自动重试能力
            self._fetching = True
            self._spawn_task(self.fetch_friends_birthday(event.bot))

    # ------------------------------------------------------------------
    # 后台任务托管
    # ------------------------------------------------------------------

    def _spawn_task(self, coro) -> asyncio.Task:
        """创建后台任务并托管到 _bg_tasks，任务结束后自动从集合中移除。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # ------------------------------------------------------------------
    # 核心：获取好友列表及生日信息
    # ------------------------------------------------------------------

    async def _fetch_one_birthday(
        self, bot, friend: dict, sem: asyncio.Semaphore
    ) -> dict | None:
        """受限并发地获取单个好友的生日信息，内置请求间隔和自动重试。"""
        qq = str(friend.get("user_id", ""))
        if not qq:
            return None
        nickname = friend.get("remark") or friend.get("nickname") or qq
        bday_year = bday_month = bday_day = None

        for attempt in range(1 + len(_FETCH_RETRY_WAITS)):
            last_exc: Exception | None = None

            # 信号量仅包裹实际网络请求，绝不在持锁期间等待重试或节流
            async with sem:
                try:
                    info: dict = await bot.get_stranger_info(user_id=int(qq), no_cache=True)
                    bday_year = info.get("birthday_year")
                    bday_month = info.get("birthday_month")
                    bday_day = info.get("birthday_day")
                except Exception as e:
                    last_exc = e
            # 节流间隔在信号量外执行，释放槽后再等待，不影响其他协程获取槽
            await asyncio.sleep(_FETCH_DELAY)

            if last_exc is None:
                break  # 请求成功，退出重试循环

            # ---- 以下代码在信号量外执行，不占用并发槽 ----
            if attempt < len(_FETCH_RETRY_WAITS):
                wait = _FETCH_RETRY_WAITS[attempt]
                logger.warning(
                    f"[FriendBirthday] 获取 {nickname}({qq}) 失败"
                    f"（第 {attempt + 1} 次），等待 {wait:.0f}s 后重试: {last_exc}"
                )
                await asyncio.sleep(wait)
            else:
                logger.warning(
                    f"[FriendBirthday] 获取 {nickname}({qq}) 生日失败"
                    f"（已重试 {len(_FETCH_RETRY_WAITS)} 次，放弃）: {last_exc}"
                )

        return {
            "qq": qq,
            "nickname": nickname,
            "birthday_year": bday_year,
            "birthday_month": bday_month,
            "birthday_day": bday_day,
        }

    async def fetch_friends_birthday(self, bot) -> None:
        """通过 OneBot API 并发获取所有好友的生日信息并写入 JSON 文件。"""
        async with self._fetch_lock:
            try:
                logger.info("[FriendBirthday] 开始获取好友列表...")
                friend_list: list[dict] = await bot.get_friend_list()
                logger.info(
                    f"[FriendBirthday] 共 {len(friend_list)} 位好友，"
                    f"开始并发获取生日信息（并发度 {_FETCH_CONCURRENCY}）..."
                )

                sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
                raw_results = await asyncio.gather(
                    *[self._fetch_one_birthday(bot, f, sem) for f in friend_list],
                    return_exceptions=True,
                )
                friends_data: list[dict] = [
                    r for r in raw_results
                    if isinstance(r, dict)
                ]

                await self._save_data(friends_data)
                # 仅在成功保存后置 False，失败时保留自动重试能力
                self._need_initial_fetch = False
                valid = sum(
                    1 for f in friends_data if f.get("birthday_month") and f.get("birthday_day")
                )
                logger.info(
                    f"[FriendBirthday] 好友生日数据已保存，"
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
        仅在 AIOCQHTTP 平台实例中选取，避免多平台场景下误发到其他平台。
        """
        # 仅选取 AIOCQHTTP 实例，防止多平台环境下消息误投其他账号/租户
        cqhttp_insts = [
            p for p in self.context.platform_manager.get_insts()
            if p.meta().id
            and "aiocqhttp" in p.meta().id.lower()
            and "webchat" not in p.meta().id.lower()
        ]

        running = [p for p in cqhttp_insts if p.status == PlatformStatus.RUNNING]
        if running:
            if len(running) > 1:
                logger.warning(
                    f"[FriendBirthday] 检测到 {len(running)} 个运行中的 AIOCQHTTP 实例，"
                    f"将使用首个实例（{running[0].meta().id}）发送消息。"
                    "多实例场景下建议在配置中通过完整 SID 精确指定目标。"
                )
            return f"{running[0].meta().id}:{msg_type}:{target_id}"

        # 保底：取第一个已知 AIOCQHTTP 实例或 default
        fallback = cqhttp_insts[0].meta().id if cqhttp_insts else "default"
        return f"{fallback}:{msg_type}:{target_id}"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否在 AstrBot 管理员列表中。"""
        sender_id = str(event.get_sender_id())
        admin_ids = [str(x) for x in self.context.config.get("admins_id", [])]
        return sender_id in admin_ids

    # ------------------------------------------------------------------
    # 数据读写
    # ------------------------------------------------------------------

    @staticmethod
    def _deduplicate_data(data: list[dict]) -> tuple[list[dict], int]:
        """按 qq 字段对好友列表去重，保留每个 QQ 号最后一条记录。

        Returns:
            (deduped, removed_count): 去重后的列表及被移除的重复条目数量。
        """
        seen: dict[str, dict] = {}
        for entry in data:
            qq = str(entry.get("qq", "")).strip()
            if qq:
                seen[qq] = entry  # 后出现的覆盖前面的，保留最新值
        deduped = list(seen.values())
        return deduped, len(data) - len(deduped)

    def _load_data(self) -> list[dict]:
        """从插件配置中读取好友生日列表。

        兼容 AstrBot 配置 UI 粘贴导入时将对象序列化为字符串的情况：
        若列表元素为 str，则尝试 json.loads 还原为 dict；解析失败的条目记警告后跳过。
        """
        raw: list = list(self.config.get("friends_birthday", []))
        result: list[dict] = []
        str_count = 0
        for entry in raw:
            if isinstance(entry, dict):
                result.append(entry)
            elif isinstance(entry, str):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    parsed = json.loads(entry)
                    if isinstance(parsed, dict):
                        result.append(parsed)
                        str_count += 1
                    else:
                        logger.warning(
                            f"[FriendBirthday] 跳过无法解析为对象的生日条目（解析结果非 dict）: {entry!r}"
                        )
                except json.JSONDecodeError:
                    logger.warning(
                        f"[FriendBirthday] 跳过无法解析为 JSON 的生日条目: {entry!r}"
                    )
            else:
                logger.warning(
                    f"[FriendBirthday] 跳过类型异常的生日条目（{type(entry).__name__}）: {entry!r}"
                )
        if str_count:
            logger.info(
                f"[FriendBirthday] 检测到 {str_count} 条字符串形式的生日条目，"
                "已自动解析为对象，建议通过保存操作将数据格式修正至文件。"
            )
        return result

    async def _save_data(self, data: list[dict]) -> None:
        """将好友生日列表异步写回插件配置并持久化到磁盘（带锁，防并发写冲突）。"""
        # 写入前去重，兜底防止手动批量编辑时引入重复 QQ
        data, removed = self._deduplicate_data(data)
        if removed:
            logger.info(f"[FriendBirthday] 保存时发现并移除 {removed} 条重复 QQ 记录。")
        async with self._data_lock:
            # 先持久化到磁盘，成功后再更新内存，避免不一致
            try:
                existing: dict = {}
                if await aio_os.path.exists(self._config_file):
                    try:
                        async with aiofiles.open(self._config_file, "r", encoding="utf-8") as f:
                            content = await f.read()
                        existing = await asyncio.to_thread(json.loads, content)
                    except json.JSONDecodeError as e:
                        logger.warning(f"[FriendBirthday] 配置文件 JSON 解析失败，将覆盖写入: {e}")
                existing["friends_birthday"] = data
                await aio_os.makedirs(self._config_file.parent, exist_ok=True)
                serialized = await asyncio.to_thread(
                    json.dumps, existing, ensure_ascii=False, indent=4
                )
                async with aiofiles.open(self._config_file, "w", encoding="utf-8") as f:
                    await f.write(serialized)
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

    def _today(self) -> datetime.date:
        """返回当前时区下的今天日期，与定时任务调度时区保持一致。"""
        if self._timezone:
            return datetime.datetime.now(self._timezone).date()
        return datetime.date.today()

    def _today_birthdays(self, data: list[dict] | None = None) -> list[dict]:
        return self._birthdays_on(self._today(), data=data)

    def _advance_birthdays(self, data: list[dict] | None = None) -> list[dict]:
        """返回 advance_days 天后生日的好友列表。"""
        if self.advance_days <= 0:
            return []
        target = self._today() + datetime.timedelta(days=self.advance_days)
        return self._birthdays_on(target, data=data)

    # ------------------------------------------------------------------
    # 发送提醒
    # ------------------------------------------------------------------

    async def _send_reminder(
        self, friends: list[dict], days_ahead: int
    ) -> tuple[int, int]:
        """向所有配置的目标（私聊 + 群聊）发送聚合生日提醒。

        所有好友合并为单条消息发送，避免好友数量多时造成消息洪泛。

        Returns:
            (sent, failed): 实际投递成功条数与失败条数（按目标数计）。
        """
        all_targets = [
            (uid, "FriendMessage") for uid in self.notify_private_ids
        ] + [
            (gid, "GroupMessage") for gid in self.notify_group_ids
        ]
        if not friends or not all_targets:
            return 0, 0

        # 聚合所有好友信息为单条消息，每个目标只收到一条提醒
        friend_lines = [
            f"• {f.get('nickname') or f.get('qq')}（QQ: {f.get('qq')}）"
            for f in friends
        ]
        if days_ahead == 0:
            text = "🎂 今日好友生日提醒\n" + "\n".join(friend_lines) + "\n\n记得送上祝福哦！🎉"
        else:
            advance_date = self._today() + datetime.timedelta(days=days_ahead)
            date_str = advance_date.strftime("%m月%d日")
            text = (
                f"🔔 生日提醒：以下好友将在 {days_ahead} 天后（{date_str}）过生日\n"
                + "\n".join(friend_lines)
                + "\n\n别忘了送上祝福！"
            )

        sent, failed = 0, 0
        for raw_id, msg_type in all_targets:
            session_id = self._resolve_full_umo(raw_id, msg_type)
            # 发送前验证平台是否仍在运行，防止平台断连时引发异常
            parts = session_id.split(":", 2)
            if len(parts) == 3:
                p_id = parts[0]
                insts = {
                    p.meta().id: p
                    for p in self.context.platform_manager.get_insts()
                    if p.meta().id
                }
                platform_inst = insts.get(p_id)
                if not platform_inst or platform_inst.status != PlatformStatus.RUNNING:
                    logger.warning(
                        f"[FriendBirthday] 平台 {p_id} 不存在或未运行，跳过发送至 {session_id}"
                    )
                    failed += 1
                    continue
            try:
                chain = MessageChain().message(text)
                await self.context.send_message(session_id, chain)
                logger.info(
                    f"[FriendBirthday] 已向 {session_id} 发送提醒"
                    f"（{len(friends)} 位好友，{'今日' if days_ahead == 0 else f'{days_ahead}天后'}）"
                )
                sent += 1
            except Exception as e:
                logger.error(f"[FriendBirthday] 向 {session_id} 发送提醒失败: {e}")
                failed += 1
        return sent, failed

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
        _now = datetime.datetime.now(self._timezone) if self._timezone else datetime.datetime.now()
        cron = croniter.croniter(cron_expr, _now)

        while True:
            try:
                next_run = cron.get_next(datetime.datetime)
                now = datetime.datetime.now(self._timezone) if self._timezone else datetime.datetime.now()
                # next_run 来自 croniter，与 now 同为带时区或同为无时区，可直接相减
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
                # 重新初始化 croniter，避免因状态已推进而跳过当天的检查
                _now = datetime.datetime.now(self._timezone) if self._timezone else datetime.datetime.now()
                cron = croniter.croniter(cron_expr, _now)

    async def _run_birthday_check(self) -> tuple[int, int]:
        """执行一次完整的生日检查（当天 + 提前 N 天）并发送提醒。

        Returns:
            (sent, failed): 本次检查累计投递成功与失败条数。
        """
        today_list = self._today_birthdays()
        advance_list = self._advance_birthdays()

        total_sent, total_failed = 0, 0
        if today_list:
            s, f = await self._send_reminder(today_list, 0)
            total_sent += s
            total_failed += f
        if advance_list:
            s, f = await self._send_reminder(advance_list, self.advance_days)
            total_sent += s
            total_failed += f

        if not today_list and not advance_list:
            logger.debug("[FriendBirthday] 今日检查完成，无生日提醒。")

        return total_sent, total_failed

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("生日刷新")
    async def cmd_refresh(self, event: AstrMessageEvent):
        """重新从 QQ 拉取所有好友的生日信息并更新本地 JSON（仅管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 该指令仅管理员可用。")
            return

        now = time.monotonic()
        remaining = _CMD_COOLDOWN - (now - self._last_cmd_time)
        if remaining > 0:
            yield event.plain_result(f"⏱️ 冷却中，请 {remaining:.0f}s 后再试。")
            return

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

        self._last_cmd_time = now
        self._fetching = True
        self._spawn_task(self.fetch_friends_birthday(bot))
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

        today = self._today()
        lines: list[str] = []

        # 今天生日
        today_list = self._birthdays_on(today, data=data)
        for f in today_list:
            lines.append(f"🎂 今天：{f.get('nickname')}（{f.get('qq')}）")

        # 未来 7 天
        for days in range(1, 8):
            target = today + datetime.timedelta(days=days)
            for f in self._birthdays_on(target, data=data):
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
        """立即执行一次生日检查，并向配置的目标发送提醒（仅管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 该指令仅管理员可用。")
            return

        now = time.monotonic()
        remaining = _CMD_COOLDOWN - (now - self._last_cmd_time)
        if remaining > 0:
            yield event.plain_result(f"⏱️ 冷却中，请 {remaining:.0f}s 后再试。")
            return

        self._last_cmd_time = now
        today_list = self._today_birthdays()
        advance_list = self._advance_birthdays()

        if not today_list and not advance_list:
            yield event.plain_result(
                f"✅ 检查完成：今天及 {self.advance_days} 天后均无好友生日。"
            )
            return
        sent, failed = await self._run_birthday_check()
        result_msg = f"✅ 检查完成，共投递 {sent} 条生日提醒"
        if failed:
            result_msg += f"，{failed} 条发送失败（详见日志）"
        result_msg += "。"
        yield event.plain_result(result_msg)

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
        """插件卸载时取消所有后台任务。"""
        # 取消每日定时任务
        if self._daily_task is not None:
            self._daily_task.cancel()
            try:
                await self._daily_task
            except (asyncio.CancelledError, Exception):
                pass

        # 取消所有托管的后台抓取任务
        for task in list(self._bg_tasks):
            task.cancel()
        for task in list(self._bg_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_tasks.clear()
