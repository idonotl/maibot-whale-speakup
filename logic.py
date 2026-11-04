"""Small deterministic scheduling decisions; no network and no model calls."""
from datetime import datetime
from zoneinfo import ZoneInfo

CHINA=ZoneInfo('Asia/Shanghai')


def in_quiet_hours(now, start, end):
    hour=datetime.fromtimestamp(now,CHINA).hour
    if start==end:return False
    if start<end:return start<=hour<end
    return hour>=start or hour<end


def inspect_messages(messages, bot_id, now):
    """Extract latest real activity and latest human message for one session."""
    if not isinstance(messages,list):raise ValueError('message_query_unavailable')
    last_activity=last_human=0.0
    for m in messages:
        if not isinstance(m,dict) or m.get('is_notify') or m.get('is_command'):
            continue
        info=m.get('message_info') or {}
        user=str((info.get('user_info') or {}).get('user_id') or '')
        try: ts=float(m.get('timestamp') or 0)
        except (TypeError,ValueError):continue
        if not user or ts<=0 or ts>now+60:continue
        last_activity=max(last_activity,ts)
        if user!=str(bot_id):last_human=max(last_human,ts)
    return last_activity,last_human


def eligibility(cfg, kind, state, now, last_activity, last_human):
    if in_quiet_hours(now,cfg.quiet_start_hour,cfg.quiet_end_hour):return 'quiet_hours'
    if not last_human or not last_activity:return 'no_chat_history'
    idle=cfg.private_idle_minutes if kind=='private' else cfg.group_idle_minutes
    if now-last_activity<idle*60:return 'recent_activity'
    if state['last_attempt'] and now-state['last_attempt']<cfg.cooldown_minutes*60:return 'cooldown'
    if state['last_queued'] and state['last_outcome'] in ('reserved','queued','uncertain') and last_human<=state['last_queued']:
        return 'waiting_for_human'
    day=datetime.fromtimestamp(now,CHINA).date().isoformat()
    limit=cfg.private_daily_max if kind=='private' else cfg.group_daily_max
    if (state['attempts'] if state['day']==day else 0)>=limit:return 'daily_limit'
    return ''


def probability(cfg, misses):
    return min(1.0,cfg.chance+min(cfg.max_miss_bonus,max(0,misses)*cfg.miss_increment))
