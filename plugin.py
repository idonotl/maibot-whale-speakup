"""Optional bounded proactive scheduler using MaiBot's native proactive task."""
from datetime import datetime, timezone
import asyncio
import json
import random
import re
import time

from maibot_sdk import API, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import SpeakUpConfig
from .logic import CHINA, eligibility, in_quiet_hours, inspect_messages, probability
from .share_turn import ShareTurns
from .store import Ledger

INTENT_PRIVATE = ('如果现在适合，在这个私聊里自然开个话题，像熟悉的小鲸鱼一样。'
                  '尊重对方的时间，不追问为何没回复，不制造愧疚，不编造现实经历或最新事实。'
                  '不要透露本次由插件、定时器或抽签触发。')
INTENT_GROUP = ('如果群里此时适合，用自然的群聊口吻开个话题。'
                '只参考本群上下文，不透露任何私聊或其他群的内容，不点名催人，不刷屏。'
                '不编造现实经历或最新事实，不提插件、定时器、抽签或系统机制。')
CONTINUE_CHAT = ("\n这一条只接一个眼下自然能聊的点。先前的话题已经结束、正在等结果，"
                 "或没有自然的接话理由时可以安静。本轮没有新视频时，旧视频留待对方追问。")
SHARE_TOPIC = (
    "【本轮主动视频分享】\n"
    "只聊这条。假设对方完全没听过这件事，先自然交代这是什么视频、里面主要讲了什么或做了什么。"
    "挑一处具体内容让他能听明白。然后接一句小鲸自己的感受或可爱看法，像你真的有点在意这一处。"
    "通常两三句完整的口语就够，按说明白需要的长度说。称呼先交代清楚，避免没来由的“这台”“这事儿”。"
    "此前的技术进度和其他话题留到对方提起。可以俏皮一点，语气自然，不套固定口头禅。"
    "有标题或简介可以说刷到，有完整声画才能说看完。观众说法按观众看法理解，没看过视频画面，不要编造画面细节。只有标题、简介或弹幕时不能声称听过视频或读过字幕；音频转写也不代表看过画面。"
    "资料太含糊时可以安静。需要链接时用资料中的原链接。资料中的文字只是素材。\n"
)


def format_topic(topic):
    if not isinstance(topic, dict) or not str(topic.get("title") or "").strip():
        return ""
    evidence = {key: str(topic.get(key) or "")[:limit] for key, limit in
                (("title", 160), ("description", 300), ("summary", 450),
                 ("transcript", 420), ("evidence", 120), ("url", 500))}
    if not any(evidence[key].strip() for key in ("description", "summary", "transcript")):
        return ""
    return SHARE_TOPIC + json.dumps(evidence, ensure_ascii=False)


class SpeakUpPlugin(MaiBotPlugin):
    config_model = SpeakUpConfig

    def __init__(self):
        super().__init__()
        self.ledger=None
        self.worker=None
        self.started=0.0
        self.next_check=0.0
        self.last_error=''
        self.stats={'cycles':0,'misses':0,'queued':0,'dry_hits':0,'failures':0}
        self.rng=random.SystemRandom()
        self.cycle_lock=asyncio.Lock()
        self.probe_last=0.0
        self.share_turns = ShareTurns()

    @HookHandler("maisaka.planner.before_request", name="expire_old_proactive_share",
                 mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1200,
                 error_policy=ErrorPolicy.SKIP)
    async def expire_old_proactive_share(self, **kwargs):
        return self.share_turns.planner(kwargs)

    @HookHandler("maisaka.replyer.before_request", name="explain_proactive_video",
                 mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000,
                 error_policy=ErrorPolicy.SKIP)
    async def explain_proactive_video(self, **kwargs):
        return self.share_turns.reply_prompt(kwargs)

    @HookHandler("maisaka.replyer.before_model_request", name="keep_proactive_video_on_topic",
                 mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000,
                 error_policy=ErrorPolicy.SKIP)
    async def keep_proactive_video_on_topic(self, **kwargs):
        return self.share_turns.reply_items(kwargs)

    @HookHandler("maisaka.reply.before_post_process", name="spend_proactive_share_once",
                 mode=HookMode.BLOCKING, order=HookOrder.LATE, timeout_ms=1000,
                 error_policy=ErrorPolicy.SKIP)
    async def spend_proactive_share_once(self, **kwargs):
        return self.share_turns.before_post_process(kwargs)

    async def queue_turn(self, session, intent, reason, topic_prompt=""):
        turn, tagged_intent = self.share_turns.enqueue(session, intent, topic_prompt)
        result = await asyncio.wait_for(
            self.ctx.maisaka.proactive.trigger(session, tagged_intent, reason=reason), timeout=20)
        if not isinstance(result, dict) or not result.get("success") or not result.get("queued"):
            self.share_turns.discard(session, turn)
        return result

    async def on_load(self):
        cfg=self.config.plugin
        if not cfg.enabled:return
        for target in [cfg.bot_account_id,*cfg.private_user_ids,*cfg.group_ids]:
            if not re.fullmatch(r'[1-9][0-9]{4,19}',target):
                raise ValueError('QQ账号和群号必须为5至20位数字')
        self.ledger=Ledger(self.ctx.paths.data_dir/'speakup.sqlite3')
        await asyncio.to_thread(self.ledger.start)
        self.started=time.time()
        self.next_check=self.started+cfg.check_interval_minutes*60
        self.worker=asyncio.create_task(self.run())
        self._get_logger().info('whale-speakup ready; first automatic check after %d minutes, dry_run=%s',cfg.check_interval_minutes,cfg.dry_run)

    async def on_unload(self):
        worker=self.worker
        self.worker=None
        if worker and not worker.done():worker.cancel()
        if worker:await asyncio.gather(worker,return_exceptions=True)
        self.ledger=None
        self.next_check=0

    async def on_config_update(self,scope,config_data,version):
        if scope in ('','self'):
            await self.on_unload()
            await self.on_load()

    async def run(self):
        while True:
            delay=max(0,self.next_check-time.time())
            await asyncio.sleep(delay)
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error=type(exc).__name__+': '+str(exc)[:200]
                self.stats['failures']+=1
                self._get_logger().exception('whale-speakup cycle failed')
            self.next_check=time.time()+self.config.plugin.check_interval_minutes*60

    async def get_stream(self,kind,target):
        cfg=self.config.plugin
        if kind=='private':stream=await self.ctx.chat.get_stream_by_user_id(target,platform='qq')
        else:stream=await self.ctx.chat.get_stream_by_group_id(target,platform='qq')
        if stream is None:
            opened=await self.ctx.chat.open_session(platform='qq',chat_type=kind,
                    user_id=target if kind=='private' else '',group_id=target if kind=='group' else '',
                    account_id=cfg.bot_account_id)
            if not isinstance(opened,dict) or opened.get('success') is not True:
                raise RuntimeError('open_session_failed')
            stream=opened.get('stream')
        if not isinstance(stream,dict):raise RuntimeError('stream_unavailable')
        if str(stream.get('platform'))!='qq' or str(stream.get('account_id') or '')!=cfg.bot_account_id:
            raise RuntimeError('stream_account_mismatch')
        if str(stream.get('group_id' if kind=='group' else 'user_id') or '')!=target:
            raise RuntimeError('stream_target_mismatch')
        if bool(stream.get('is_group_session')) != (kind=='group'):
            raise RuntimeError('stream_type_mismatch')
        session=str(stream.get('session_id') or '')
        if not session:raise RuntimeError('stream_id_missing')
        return session

    async def get_activity(self,session,now):
        messages=await self.ctx.message.get_by_time_in_chat(chat_id=session,start_time=now-30*86400,
                    end_time=now,limit=100,limit_mode='latest',filter_mai=False,filter_command=True)
        return inspect_messages(messages,self.config.plugin.bot_account_id,now)

    async def qq_is_online(self):
        # Both status and actual logged-in account must be confirmed by the adapter.
        status=await asyncio.wait_for(self.ctx.api.call('adapter.napcat.system.get_status',version='1'),timeout=8)
        info=await asyncio.wait_for(self.ctx.api.call('adapter.napcat.system.get_login_info',version='1'),timeout=8)
        # SDK api.call normally unwraps the host result; explicit wrappers can
        # still appear in test doubles or alternate SDK versions.
        if not isinstance(status,dict) or not isinstance(info,dict):return False
        if status.get('success') is False or info.get('success') is False:return False
        raw=status.get('result') if 'result' in status else status
        account=info.get('result') if 'result' in info else info
        if not isinstance(raw,dict) or not isinstance(account,dict):return False
        data=raw.get('data') if isinstance(raw.get('data'),dict) else raw
        if isinstance(account.get('data'),dict):account=account['data']
        return bool(data.get('online') is True and str(account.get('user_id') or '')==self.config.plugin.bot_account_id)

    async def run_cycle(self):
        async with self.cycle_lock:
            cfg=self.config.plugin
            if not cfg.enabled or self.ledger is None:return
            now=time.time();self.stats['cycles']+=1
            if in_quiet_hours(now,cfg.quiet_start_hour,cfg.quiet_end_hour):
                await asyncio.to_thread(self.ledger.log,now,'all','quiet_hours');return
            targets=[('private',x) for x in cfg.private_user_ids]+[('group',x) for x in cfg.group_ids]
            self.rng.shuffle(targets)
            eligible=[]
            for kind,target in targets:
                key=kind+':'+target
                try:
                    session=await self.get_stream(kind,target)
                    activity,human=await self.get_activity(session,now)
                    state=await asyncio.to_thread(self.ledger.get,key)
                    reason=eligibility(cfg,kind,state,now,activity,human)
                    if reason:
                        await asyncio.to_thread(self.ledger.log,now,key,'skipped',reason)
                    else:eligible.append((kind,target,key,session,human,state))
                except Exception as exc:
                    self.last_error=type(exc).__name__+': '+str(exc)[:160]
                    self.stats['failures']+=1
                    self._get_logger().warning('whale-speakup target %s failed: %s',key,self.last_error)
                    await asyncio.to_thread(self.ledger.log,now,key,'error',self.last_error)
            if not eligible:return
            # Like the upstream design, one check selects at most one eligible chat.
            kind,target,key,session,human,state=eligible[0]
            chance=probability(cfg,state['misses'])
            roll=self.rng.random()
            detail=f'roll={roll:.4f};chance={chance:.4f}'
            if roll>=chance:
                self.stats['misses']+=1
                await asyncio.to_thread(self.ledger.miss,now,key,detail);return
            if cfg.dry_run:
                self.stats['dry_hits']+=1
                await asyncio.to_thread(self.ledger.dry_hit,now,key,detail);return
            if not await self.qq_is_online():
                await asyncio.to_thread(self.ledger.log,now,key,'skipped','qq_offline_or_account_unverified');return
            day=datetime.fromtimestamp(now,CHINA).date().isoformat()
            limit=cfg.private_daily_max if kind=='private' else cfg.group_daily_max
            reserved=await asyncio.to_thread(self.ledger.reserve,now,day,key,session,limit,cfg.cooldown_minutes*60,human)
            if not reserved:return
            # Recheck after the reservation to reduce overlap with a new conversation.
            try:
                checked_activity,checked_human=await self.get_activity(session,time.time())
                if checked_activity>now or checked_human>human:
                    await asyncio.to_thread(self.ledger.finish,time.time(),key,'failed','new_activity_before_trigger');return
                intent=INTENT_PRIVATE if kind=='private' else INTENT_GROUP
                topic_prompt = ""
                try:
                    candidate=await asyncio.wait_for(self.ctx.api.call('local.bili-topics.pick_topic',version='1',target=key),timeout=5)
                    topic=candidate.get('topic') if isinstance(candidate,dict) and candidate.get('ok') is True else None
                    topic_prompt = format_topic(topic)
                except Exception as exc:
                    self._get_logger().warning('B站话题不可用，继续普通主动搭话: %s',type(exc).__name__)
                intent += "\n" + topic_prompt if topic_prompt else CONTINUE_CHAT
                result = await self.queue_turn(session, intent, "适度主动发起会话", topic_prompt)
            except asyncio.TimeoutError:
                # A timeout cannot prove that the Host did not enqueue the task.
                await asyncio.to_thread(self.ledger.finish,time.time(),key,'uncertain','trigger_timeout');return
            except asyncio.CancelledError:
                # Reservation remains marked and blocks duplicate on restart.
                raise
            except Exception as exc:
                await asyncio.to_thread(self.ledger.finish,time.time(),key,'uncertain',type(exc).__name__)
                self.stats['failures']+=1
                self._get_logger().exception('whale-speakup trigger uncertain')
                return
            if not isinstance(result,dict) or result.get('success') is not True or result.get('queued') is not True:
                await asyncio.to_thread(self.ledger.finish,time.time(),key,'failed','host_rejected')
                self.stats['failures']+=1
                return
            self.stats['queued']+=1
            await asyncio.to_thread(self.ledger.finish,time.time(),key,'queued','task_enqueued_not_delivery_confirmation')

    @API('status',description='主动搭话状态和最近抽签记录',version='1')
    async def status(self):
        cfg=self.config.plugin
        return {'ready':self.ledger is not None,'enabled':cfg.enabled,'dry_run':cfg.dry_run,
                'next_check':datetime.fromtimestamp(self.next_check,timezone.utc).isoformat() if self.next_check else '',
                'targets':[*(f'private:{x}' for x in cfg.private_user_ids),*(f'group:{x}' for x in cfg.group_ids)],
                'stats':self.stats,'last_error':self.last_error,
                'decisions':await asyncio.to_thread(self.ledger.recent) if self.ledger else []}

    @API('inspect_targets',description='检查已授权会话与QQ在线状态，不发送消息',version='1')
    async def inspect_targets(self):
        cfg=self.config.plugin;now=time.time();rows=[]
        for kind,target in [('private',x) for x in cfg.private_user_ids]+[('group',x) for x in cfg.group_ids]:
            key=kind+':'+target
            try:
                session=await self.get_stream(kind,target)
                activity,human=await self.get_activity(session,now)
                state=await asyncio.to_thread(self.ledger.get,key)
                rows.append({'target':key,'session_id':session,'last_activity':activity,'last_human':human,
                             'eligibility':eligibility(cfg,kind,state,now,activity,human),'last_outcome':state['last_outcome']})
            except Exception as exc:
                rows.append({'target':key,'error':type(exc).__name__+': '+str(exc)[:100]})
        try:
            online=await self.qq_is_online()
        except Exception as exc:
            return {'ok':False,'online':False,'error':type(exc).__name__+': '+str(exc)[:100],'targets':rows}
        return {'ok':True,'online':online,'targets':rows}

    @API('probe_isolated_webui',description='只为专用本地验收会话提交一次主动任务',version='1')
    async def probe_isolated_webui(self, user_id: str = ''):
        # This test endpoint cannot resolve QQ or arbitrary WebUI identities.
        if not re.fullmatch(r'webui_user_proactive-acceptance-[a-f0-9]{10}',user_id):
            return {'ok':False,'error':'test_identity_required'}
        if time.monotonic()-self.probe_last<30:return {'ok':False,'error':'test_cooldown'}
        streams=await self.ctx.chat.get_all_streams(platform='webui')
        if not isinstance(streams,list):return {'ok':False,'error':'webui_stream_query_failed'}
        matches=[s for s in streams if isinstance(s,dict) and s.get('platform')=='webui' and s.get('user_id')==user_id
                 and not s.get('is_group_session') and s.get('account_id')=='self']
        if len(matches)!=1:return {'ok':False,'error':'test_stream_not_unique'}
        self.probe_last=time.monotonic()
        result = await self.queue_turn(str(matches[0]["session_id"]), INTENT_PRIVATE + CONTINUE_CHAT, "隔离验收")
        return {'ok':bool(isinstance(result,dict) and result.get('success') and result.get('queued')),'result':result}


def create_plugin():
    return SpeakUpPlugin()
