# astrbot_plugin_friend_birthday

QQ 好友生日提醒 AstrBot 插件。自动拉取好友列表的生日信息并本地持久化，每天定时检查，在生日当天或提前 N 天向指定的私聊 / 群聊发送提醒。

---

## 功能

- **自动获取好友生日**：首次启动或执行 `/生日刷新` 时，通过 OneBot API 批量拉取所有好友的生日信息并保存至插件配置
- **每日定时检查**：在配置的时间自动检查当天及提前 N 天的生日，满足条件则主动发送提醒
- **多目标推送**：支持同时向多个私聊（QQ 号）和多个群聊（群号）发送提醒，无需手动填写完整 SID
- **数据可视化编辑**：生日数据直接存储在 AstrBot 插件配置中，可在配置面板直接增删改条目
- **灵活配置**：提前提醒天数、检查时间、推送目标均可按需调整

---

## 环境要求

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)
- 平台适配器：**NapCat / LLOneBot（AIOCQHTTP）**，插件依赖 OneBot API 获取好友生日

---

## 安装

在 AstrBot 插件市场搜索 `astrbot_plugin_friend_birthday` 安装，或将本仓库克隆至 AstrBot 插件目录后重启。

---

## 配置

在 AstrBot 管理面板 → 插件配置 → 本插件 中进行设置。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `notify_private_ids` | 列表 | `[]` | 私聊提醒目标，**直接填 QQ 号**，如 `123456789` |
| `notify_group_ids` | 列表 | `[]` | 群聊提醒目标，**直接填群号**，如 `987654321` |
| `advance_days` | 整数 | `2` | 提前几天发送提醒，`0` 表示仅当天提醒 |
| `check_time` | 字符串 | `"8:00"` | 每日检查时间，24 小时制，如 `8:00`、`13:30` |
| `friends_birthday` | 列表 | `[]` | 好友生日数据（由插件自动填充，也可手动编辑） |

> **注意**：`notify_private_ids` / `notify_group_ids` 只需填数字，插件会自动识别当前运行的平台并拼装完整的消息路由，无需手动写 `aiocqhttp:PrivateMessage:xxx` 这种格式。

### 好友生日数据格式

`friends_birthday` 中每条记录的结构如下，可在配置面板手动增删改：

```json
{
  "qq": "123456789",
  "nickname": "张三",
  "birthday_year": 2000,
  "birthday_month": 3,
  "birthday_day": 15
}
```

| 字段 | 说明 |
|------|------|
| `qq` | QQ 号（字符串） |
| `nickname` | 备注名或昵称 |
| `birthday_year` | 出生年份，可填 `null` |
| `birthday_month` | 出生月份（1 ~ 12） |
| `birthday_day` | 出生日（1 ~ 31） |

自行批量导入的时候，由于是astrbot按每行导入，因此需要为一行写入完整的 JSON 对象，如：
```
{ "qq":11234567, "nickname":"233","birthday_year":2333,"birthday_month":2, "birthday_day":23 }
{ "qq":11234567, "nickname":"233","birthday_year":2333,"birthday_month":2, "birthday_day":23 }
```

---

## 指令

所有指令均以 `/` 触发。

| 指令 | 说明 |
|------|------|
| `/生日刷新` | 重新从 QQ 拉取全部好友生日并写入配置（后台执行，好友多时耗时较长） |
| `/生日列表` | 查看今天及未来 7 天内有生日的好友 |
| `/生日检查` | 立即执行一次生日检查，并向配置的目标发送提醒 |
| `/生日状态` | 查看当前插件配置、数据概况及推送目标 |

---

## 工作流程

```
插件启动
  └─ friends_birthday 为空？
       ├─ 是 → 等待收到第一条 AIOCQHTTP 消息后自动拉取好友生日
       └─ 否 → 直接加载已有数据

每日 check_time
  ├─ 检查「今天」生日的好友 → 发送当天生日提醒
  └─ 检查「今天 + advance_days」生日的好友 → 发送提前提醒
```

---

## 常见问题

**Q：拉取好友生日需要多长时间？**  
A：取决于好友数量，每位好友需要单独请求一次 API。几十人约需数秒，几百人可能需要数分钟，期间不阻塞 Bot 的其他功能。

**Q：好友没有填写生日怎么办？**  
A：`birthday_month` / `birthday_day` 将为 `null`，该好友不会触发任何提醒。可在配置面板手动补充。

**Q：可以只提醒自己吗？**  
A：在 `notify_private_ids` 中填入自己的 QQ 号即可，机器人会给你发私聊提醒。

**Q：数据存储在哪里？**  
A：存储在 AstrBot 插件配置文件 `data/config/astrbot_plugin_friend_birthday.json` 的 `friends_birthday` 字段中，配置面板和文件均可直接编辑。

