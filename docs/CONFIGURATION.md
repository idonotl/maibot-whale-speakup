# 配置字段参考

由本次发布的 config.py 提取；最终校验以代码与宿主配置 UI 为准。不要把实际配置提交到仓库。

| 配置类.字段 | 类型 | 默认值/工厂 | 说明 |
|---|---|---|---|
| Settings.enabled | `bool` | `False` | 开启后仅向授权会话发起主动任务 |
| Settings.config_version | `str` | `'0.1.0'` | 配置结构版本 |
| Settings.dry_run | `bool` | `True` | 试运行：只记录抽签结果，不调用模型、不发消息 |
| Settings.bot_account_id | `str` | `''` | 机器人QQ |
| Settings.private_user_ids | `list[str]` | `list` | 允许主动搭话的私聊QQ号 |
| Settings.group_ids | `list[str]` | `list` | 允许主动搭话的群号 |
| Settings.check_interval_minutes | `int` | `30` | 每多少分钟本地抽签一次；启动后先等待一个完整间隔 |
| Settings.private_idle_minutes | `int` | `60` | 私聊至少安静多久 |
| Settings.group_idle_minutes | `int` | `25` | 群聊至少安静多久 |
| Settings.cooldown_minutes | `int` | `180` | 每个会话主动尝试之间至少间隔多久 |
| Settings.quiet_start_hour | `int` | `23` | 夜间静默开始，上海时间，包含该小时 |
| Settings.quiet_end_hour | `int` | `8` | 夜间静默结束，上海时间，不包含该小时 |
| Settings.chance | `float` | `0.15` | 本地抽签基础概率；不代表API电价 |
| Settings.miss_increment | `float` | `0.05` | 每次没抽中后给该会话的概率加成 |
| Settings.max_miss_bonus | `float` | `0.2` | 连续没抽中的额外概率上限 |
| Settings.private_daily_max | `int` | `2` | 私聊每天最多主动任务次数（含失败或不发） |
| Settings.group_daily_max | `int` | `1` | 每群每天最多主动任务次数（含失败或不发） |
| SpeakUpConfig.plugin | `Settings` | `Settings` |  |
