from maibot_sdk import Field, PluginConfigBase


class Settings(PluginConfigBase):
    __ui_label__ = "主动搭话设置"
    enabled: bool = Field(default=False, description="开启后仅向授权会话发起主动任务")
    config_version: str = Field(default="0.1.0", description="配置结构版本")
    dry_run: bool = Field(default=True, description="试运行：只记录抽签结果，不调用模型、不发消息")
    bot_account_id: str = Field(default="", description="机器人QQ")
    private_user_ids: list[str] = Field(default_factory=list, description="允许主动搭话的私聊QQ号")
    group_ids: list[str] = Field(default_factory=list, description="允许主动搭话的群号")
    check_interval_minutes: int = Field(default=30, ge=5, le=1440, description="每多少分钟本地抽签一次；启动后先等待一个完整间隔")
    private_idle_minutes: int = Field(default=60, ge=5, le=10080, description="私聊至少安静多久")
    group_idle_minutes: int = Field(default=25, ge=5, le=10080, description="群聊至少安静多久")
    cooldown_minutes: int = Field(default=180, ge=30, le=10080, description="每个会话主动尝试之间至少间隔多久")
    quiet_start_hour: int = Field(default=23, ge=0, le=23, description="夜间静默开始，上海时间，包含该小时")
    quiet_end_hour: int = Field(default=8, ge=0, le=23, description="夜间静默结束，上海时间，不包含该小时")
    chance: float = Field(default=.15, ge=0, le=1, description="本地抽签基础概率；不代表API电价")
    miss_increment: float = Field(default=.05, ge=0, le=.25, description="每次没抽中后给该会话的概率加成")
    max_miss_bonus: float = Field(default=.20, ge=0, le=.5, description="连续没抽中的额外概率上限")
    private_daily_max: int = Field(default=2, ge=0, le=5, description="私聊每天最多主动任务次数（含失败或不发）")
    group_daily_max: int = Field(default=1, ge=0, le=3, description="每群每天最多主动任务次数（含失败或不发）")


class SpeakUpConfig(PluginConfigBase):
    plugin: Settings = Field(default_factory=Settings)
