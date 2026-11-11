from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import asyncio
import importlib
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
package=ModuleType('speakup_under_test');package.__path__=[str(ROOT)];sys.modules[package.__name__]=package
logic=importlib.import_module('speakup_under_test.logic')
store=importlib.import_module('speakup_under_test.store')
plugin=importlib.import_module('speakup_under_test.plugin')
NOW=datetime(2026,10,27,12,tzinfo=timezone(timedelta(hours=8))).timestamp()


def settings(**changes):
    values=dict(quiet_start_hour=23,quiet_end_hour=8,private_idle_minutes=60,group_idle_minutes=25,
                cooldown_minutes=180,private_daily_max=2,group_daily_max=1,chance=.15,miss_increment=.05,max_miss_bonus=.20)
    values.update(changes);return SimpleNamespace(**values)


def message(ts,user='user',**extra):
    return {'timestamp':str(ts),'message_info':{'user_info':{'user_id':user}},**extra}


class LogicTest(unittest.TestCase):
    def test_quiet_hours_cross_midnight(self):
        for hour,expected in ((22,False),(23,True),(0,True),(7,True),(8,False)):
            stamp=datetime(2026,10,27,hour,tzinfo=timezone(timedelta(hours=8))).timestamp()
            self.assertEqual(logic.in_quiet_hours(stamp,23,8),expected)
        self.assertFalse(logic.in_quiet_hours(NOW,8,8))

    def test_real_activity_only_and_unknown_history(self):
        messages=[message(NOW-100,'bot'),message(NOW-300,'u'),message(NOW-10,'u',is_notify=True),message(NOW+300,'u')]
        self.assertEqual(logic.inspect_messages(messages,'bot',NOW),(NOW-100,NOW-300))
        self.assertEqual(logic.inspect_messages([], 'bot', NOW),(0,0))
        with self.assertRaises(ValueError):logic.inspect_messages({'error':'offline'},'bot',NOW)

    def test_eligibility_idle_cooldown_and_unanswered(self):
        state={'last_attempt':0,'last_queued':0,'last_outcome':'','day':'','attempts':0}
        self.assertEqual(logic.eligibility(settings(),'private',state,NOW,NOW-1200,NOW-1200),'recent_activity')
        self.assertEqual(logic.eligibility(settings(),'private',state,NOW,NOW-4000,NOW-4000),'')
        state.update(last_attempt=NOW-11000,last_queued=NOW-11000,last_outcome='queued')
        self.assertEqual(logic.eligibility(settings(),'private',state,NOW,NOW-4000,NOW-12000),'waiting_for_human')
        self.assertEqual(logic.eligibility(settings(),'private',state,NOW,NOW-4000,NOW-4000),'')
        state['last_attempt']=NOW-100
        self.assertEqual(logic.eligibility(settings(),'private',state,NOW,NOW-4000,NOW-4000),'cooldown')

    def test_probability_is_bounded(self):
        self.assertAlmostEqual(logic.probability(settings(),0),.15)
        self.assertAlmostEqual(logic.probability(settings(),2),.25)
        self.assertAlmostEqual(logic.probability(settings(),100),.35)


    def test_old_proactive_task_cannot_replay_after_one_reply_or_a_new_message(self):
        plans = plugin.ShareTurns()
        _turn, intent = plans.enqueue("room", "share video", "具体视频内容与自己的看法")
        stamp = datetime.now().astimezone()

        def item(text, when):
            return {"item_type": "UserMessageItem", "meta": {"timestamp": when.isoformat()},
                    "parts": [{"type": "text", "text": text}]}

        task = item('<plugin_proactive_task id="proactive:local.whale-speakup:1" '
                    'plugin_id="local.whale-speakup">\n' + intent + '\n</plugin_proactive_task>', stamp)
        tools = [{"function": {"name": k}} for k in ("reply", "stop")]
        plan = {"session_id": "room", "items": [task], "tool_definitions": tools}
        first = plans.planner(plan)["modified_kwargs"]
        self.assertIn("share video", str(first["items"]))
        self.assertNotIn("内部任务标识", str(first["items"]))
        self.assertIn("具体视频内容", plans.reply_prompt(plan)["modified_kwargs"]["extra_prompt"])
        system = {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "小鲸的人设"}]}
        drafted = plans.reply_items({**plan, "items": [system, item("下午好，允许句子残缺", stamp)]})
        self.assertEqual(drafted["modified_kwargs"]["items"][0], system)
        self.assertIn("具体视频内容", str(drafted["modified_kwargs"]["items"]))
        self.assertNotIn("下午好", str(drafted["modified_kwargs"]["items"]))
        self.assertNotIn("允许句子残缺", str(drafted["modified_kwargs"]["items"]))
        response = {"session_id": "room", "response": "先讲内容，再说感受", "enable_splitter": True}
        self.assertFalse(plans.before_post_process(response)["modified_kwargs"]["enable_splitter"])
        self.assertEqual(plans.before_post_process(response)["modified_kwargs"]["response"], "")
        finished = plans.planner(plan)["modified_kwargs"]
        self.assertEqual(finished["tool_definitions"], [tools[-1]])
        latest = item('<message msg_id="2">\n晚饭吃什么</message>', stamp + timedelta(seconds=1))
        new_turn = plans.planner({**plan, "items": [task, latest]})["modified_kwargs"]
        self.assertEqual(new_turn["tool_definitions"], tools)
        self.assertNotIn("share video", str(new_turn["items"]))
        self.assertIn("晚饭吃什么", str(new_turn["items"]))


class LedgerTest(unittest.TestCase):
    def test_reservations_survive_restart_and_never_bypass_limits(self):
        with TemporaryDirectory() as tmp:
            l=store.Ledger(Path(tmp)/'state.sqlite');l.start();d='2026-10-27';target='private:12345'
            self.assertTrue(l.reserve(NOW,d,target,'stream',2,10800,NOW-5000))
            l=store.Ledger(Path(tmp)/'state.sqlite')
            self.assertFalse(l.reserve(NOW+12000,d,target,'stream',2,10800,NOW-5000))
            self.assertTrue(l.reserve(NOW+12000,d,target,'stream',2,10800,NOW+100))
            self.assertFalse(l.reserve(NOW+24000,d,target,'stream',2,10800,NOW+12100))
            self.assertTrue(l.reserve(NOW+86400,'2026-10-28',target,'stream',2,10800,NOW+24100))


class StubRandom:
    def __init__(self,value):self.value=value
    def random(self):return self.value
    def shuffle(self,targets):pass


class FakeChat:
    async def get_stream_by_user_id(self,target,platform):
        return {'platform':'qq','account_id':'10001','user_id':target,'group_id':'',
                'is_group_session':False,'session_id':'private-stream'}
    async def get_stream_by_group_id(self,target,platform):
        return {'platform':'qq','account_id':'10001','group_id':target,'user_id':'',
                'is_group_session':True,'session_id':'group-stream'}
    async def get_all_streams(self,platform):return []


class FakeMessage:
    async def get_by_time_in_chat(self,**kwargs):return [message(NOW-7200,'10002')]


class FakeAPI:
    def __init__(self,online=True):self.online=online;self.calls=[]
    async def call(self,name,**kwargs):
        self.calls.append(name)
        if name.endswith('get_status'):return {'status':'ok','data':{'online':self.online}}
        return {'user_id':10001}


class FakeProactive:
    def __init__(self,fail=None):self.calls=[];self.fail=fail
    async def trigger(self,stream,intent,**kwargs):
        self.calls.append((stream,intent))
        if self.fail:raise self.fail
        return {'success':True,'queued':True,'task_id':'synthetic'}


class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.proactive=FakeProactive();self.api=FakeAPI()
        self.instance=plugin.create_plugin()
        self.instance.set_plugin_config({'plugin':{'config_version':'0.1.0','enabled':True,'dry_run':False,
            'bot_account_id':'10001','private_user_ids':['10002'],'group_ids':['10003']}})
        self.instance._ctx=SimpleNamespace(paths=SimpleNamespace(data_dir=Path(self.tmp.name)),chat=FakeChat(),
            message=FakeMessage(),api=self.api,maisaka=SimpleNamespace(proactive=self.proactive),
            logger=__import__('logging').getLogger('speakup-test'))
        await self.instance.on_load()

    async def asyncTearDown(self):await self.instance.on_unload()

    async def test_miss_costs_no_model_and_sends_no_task(self):
        self.instance.rng=StubRandom(.99)
        with patch.object(plugin.time,'time',return_value=NOW):await self.instance.run_cycle()
        self.assertEqual(self.proactive.calls,[])
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.instance.stats['misses'],1)

    async def test_hit_submits_one_native_task_then_waits_for_human(self):
        self.instance.rng=StubRandom(.01)
        with patch.object(plugin.time,'time',return_value=NOW):await self.instance.run_cycle()
        self.assertEqual(len(self.proactive.calls),1)
        self.assertEqual(self.proactive.calls[0][0],'private-stream')
        row=self.instance.ledger.get('private:10002')
        self.assertEqual(row['last_outcome'],'queued')
        with patch.object(plugin.time,'time',return_value=NOW+12000):await self.instance.run_cycle()
        self.assertTrue(all(call[0]!='private-stream' for call in self.proactive.calls[1:]))

    async def test_topic_is_used_only_after_hit(self):
        original=self.api.call
        async def with_topic(name,**kwargs):
            if name=='local.bili-topics.pick_topic':
                return {'ok':True,'topic':{'title':'电竞新闻','description':'公开简介','category':'游戏','url':'https://www.bilibili.com/video/BV1234567890','evidence':'仅标题和简介'}}
            return await original(name,**kwargs)
        self.api.call=with_topic;self.instance.rng=StubRandom(.01)
        with patch.object(plugin.time,'time',return_value=NOW):await self.instance.run_cycle()
        intent=self.proactive.calls[0][1]
        self.assertIn('电竞新闻',intent);self.assertIn('不要编造画面细节',intent)

    async def test_offline_does_not_queue(self):
        self.api.online=False;self.instance.rng=StubRandom(.01)
        with patch.object(plugin.time,'time',return_value=NOW):await self.instance.run_cycle()
        self.assertEqual(self.proactive.calls,[])
        self.assertEqual(self.instance.ledger.get('private:10002')['attempts'],0)

    async def test_online_check_rejects_wrong_bot(self):
        original=self.api.call
        async def wrong_account(name,**kwargs):
            return {'user_id':999999} if name.endswith('get_login_info') else await original(name,**kwargs)
        self.api.call=wrong_account
        self.assertFalse(await self.instance.qq_is_online())

    async def test_timeout_blocks_duplicate_after_restart(self):
        self.instance.rng=StubRandom(.01);self.proactive.fail=asyncio.TimeoutError()
        with patch.object(plugin.time,'time',return_value=NOW):await self.instance.run_cycle()
        self.assertEqual(self.instance.ledger.get('private:10002')['last_outcome'],'uncertain')
        self.assertFalse(self.instance.ledger.reserve(NOW+12000,'2026-10-27','private:10002','private-stream',2,10800,NOW-7200))

    async def test_probe_refuses_qq_and_arbitrary_webui_users(self):
        for user in ('10002','webui_user_10002','webui_user_proactive-acceptance-not-hex'):
            result=await self.instance.probe_isolated_webui(user)
            self.assertFalse(result['ok'])
        self.assertEqual(self.proactive.calls,[])


if __name__=='__main__':unittest.main()
