"""Allow a queued proactive task to supply context for one reply attempt."""
from dataclasses import dataclass, field
from datetime import datetime
import re
import time
from uuid import uuid4


TASK = re.compile(r'^<plugin_proactive_task id="(proactive:local\.whale-speakup:\d+)" '
                  r'plugin_id="local\.whale-speakup">')
TICKET = re.compile(r"(?m)^内部任务标识：([a-f0-9]{32})\s*$")
TASK_TTL = 15 * 60
CONTINUE = {"action": "continue"}


def timestamp(item):
    try:
        return datetime.fromisoformat(item.get("meta", {}).get("timestamp") or "").timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


@dataclass
class ShareTurn:
    topic_prompt: str
    started_at: float = field(default_factory=time.time)
    ticket: str = field(default_factory=lambda: uuid4().hex)
    task_id: str = ""
    attempted: bool = False

    def live(self):
        return not self.attempted and time.time() - self.started_at <= TASK_TTL


class ShareTurns:
    def __init__(self):
        self.turns: dict[str, ShareTurn] = {}
        self.planned: dict[str, ShareTurn] = {}

    def enqueue(self, session, intent, topic_prompt):
        turn = ShareTurn(topic_prompt)
        self.turns[session] = turn
        self.planned.pop(session, None)
        return turn, intent + "\n内部任务标识：" + turn.ticket

    def discard(self, session, turn):
        if self.turns.get(session) is turn:
            self.turns.pop(session, None)

    def planner(self, kwargs):
        session = str(kwargs.get("session_id") or "")
        self.planned.pop(session, None)
        turn = self.turns.get(session)
        items = kwargs.get("items") or []
        if not session or not items:
            return CONTINUE
        latest_human = 0.0
        latest_other_task = 0.0
        for item in items:
            for part in item.get("parts") or ():
                text = str(part.get("text") or "")
                if text.startswith("<message ") and 'is_self_message="true"' not in text.split(">", 1)[0]:
                    latest_human = max(latest_human, timestamp(item))
                if text.startswith("<plugin_proactive_task ") and not TASK.match(text):
                    latest_other_task = max(latest_other_task, timestamp(item))
        result = []
        newest_own_task = 0.0
        changed = False
        for item in items:
            parts = []
            for part in item.get("parts") or ():
                text = str(part.get("text") or "")
                task = TASK.match(text)
                if not task:
                    parts.append(part)
                    continue
                changed = True
                stamp = timestamp(item)
                newest_own_task = max(newest_own_task, stamp)
                ticket = TICKET.search(text)
                if (turn and turn.live() and stamp > 0
                        and max(latest_human, latest_other_task) <= stamp
                        and (turn.task_id == task[1] or (ticket and ticket[1] == turn.ticket))):
                    turn.task_id = task[1]
                    self.planned[session] = turn
                    parts.append({**part, "text": TICKET.sub("", text)})
            if parts or not item.get("parts"):
                result.append({**item, "parts": parts} if parts else item)
        if not changed:
            return CONTINUE
        if session in self.planned:
            return {"action": "continue", "modified_kwargs": {**kwargs, "items": result}}
        if turn:
            self.discard(session, turn)
        stop = (turn is not None and turn.attempted and newest_own_task > 0
                and max(latest_human, latest_other_task) <= newest_own_task)
        instruction = ("主动聊天任务已结束，使用 stop 收尾。" if stop else
                       "此前的主动分享任务已结束。直接回应对方最新的消息，对方明确追问时再接着聊旧视频。")
        result.append({"item_type": "UserMessageItem",
                       "meta": {"item_id": uuid4().hex, "logical_turn_id": None,
                                "timestamp": datetime.now().isoformat()},
                       "parts": [{"type": "text", "text": instruction}]})
        modified = {**kwargs, "items": result}
        if stop:
            modified["tool_definitions"] = [
                tool for tool in kwargs.get("tool_definitions") or []
                if ((tool.get("function") or {}).get("name") or tool.get("name")) == "stop"
            ]
        return {"action": "continue", "modified_kwargs": modified}

    def reply_prompt(self, kwargs):
        turn = self.planned.get(str(kwargs.get("session_id") or ""))
        if not turn or not turn.live() or not turn.topic_prompt:
            return CONTINUE
        args = dict(kwargs.get("reply_tool_args") or {})
        args["reply_reference"] = "按本轮主动视频资料，先讲清视频内容，再说自己的看法。"
        prompt = str(kwargs.get("extra_prompt") or "") + "\n\n" + turn.topic_prompt
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": prompt,
                "reply_tool_args": args}}

    def reply_items(self, kwargs):
        turn = self.planned.get(str(kwargs.get("session_id") or ""))
        if not turn or not turn.live() or not turn.topic_prompt:
            return CONTINUE
        # Native proactive replies can otherwise look like an answer to an old
        # message. Preserve the persona and give this generation one subject.
        system_items = [item for item in kwargs.get("items") or []
                        if item.get("item_type") == "SystemMessageItem"]
        subject = {
            "item_type": "UserMessageItem",
            "meta": {"item_id": uuid4().hex, "logical_turn_id": None,
                     "timestamp": datetime.now().isoformat()},
            "parts": [{"type": "text", "text":
                       "你现在主动分享下面这一条视频。只输出一段能直接发出去的话，句子完整自然。\n"
                       + turn.topic_prompt}],
        }
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": [*system_items, subject]}}

    def before_post_process(self, kwargs):
        turn = self.planned.get(str(kwargs.get("session_id") or ""))
        if not turn:
            return CONTINUE
        if not turn.live():
            return {"action": "continue", "modified_kwargs": {**kwargs, "response": ""}}
        turn.attempted = True
        if not turn.topic_prompt:
            return CONTINUE
        return {"action": "continue", "modified_kwargs": {**kwargs, "enable_splitter": False}}
