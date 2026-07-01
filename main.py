# -*- coding: utf-8 -*-
"""
astrbot_plugin_affection - 棱镜娘好感度系统

对齐类脑娘好感度核心功能：
- /好感度 查询当前好感度等级
- /投喂  给棱镜娘送食物（AI 评价 + 结构化标签解析 + 好感度奖励）
- /忏悔  向棱镜娘忏悔（AI 回应 + <affection:+/-N> 标签解析）
- 被动聊天好感度增长（每日上限）
- 每日好感度浮动（凌晨随机变化）
- LLM 人设注入（根据好感度等级调整语气）
"""

import os
import re
import random
import sqlite3
import asyncio
import httpx
from datetime import datetime, timezone, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Image as ImageComp

BEIJING_TZ = timezone(timedelta(hours=8))

# --- 好感度等级表 ---
AFFECTION_LEVELS = [
    {"min": 0,   "name": "陌生",   "prompt": "你对这个用户还很陌生，保持礼貌和距离感。称呼对方为「你」。",
     "response_style": "礼貌但疏离"},
    {"min": 20,  "name": "认识",   "prompt": "你开始认识这个用户了，可以稍微放松一些，语气温和。",
     "response_style": "温和友善"},
    {"min": 50,  "name": "友好",   "prompt": "你们已经是朋友了，可以亲切地聊天，适当使用昵称。",
     "response_style": "亲切友好"},
    {"min": 100, "name": "信赖",   "prompt": "你非常信任这个用户，可以敞开心扉，分享更多真实想法。",
     "response_style": "信任亲密"},
    {"min": 200, "name": "亲密",   "prompt": "这个用户是你最亲近的人之一，语气可以非常亲密。",
     "response_style": "温暖亲密"},
    {"min": 500, "name": "挚友",   "prompt": "这个用户是你无可替代的挚友，可以用最真诚自然的方式交流。",
     "response_style": "极度亲密真诚"},
]

# 匹配类脑娘风格的结构化标签 <affection:5;is_food:是;food_desc:草莓蛋糕;scene_desc:下午茶>
_TAG_PATTERN = re.compile(r"`?\s*<([^>]*:[^>]*;[^>]*)>\s*`?", re.DOTALL)


def _parse_feeding_response(response_text: str):
    """解析 AI 投喂回复，提取评价文本和结构化数据"""
    matches = _TAG_PATTERN.findall(response_text)
    if not matches:
        return {"evaluation": response_text, "affection_gain": 1, "is_food": False,
                "food_desc": "", "scene_desc": ""}

    tag_content = matches[-1]
    # 标签之前的部分是评价文本
    start = response_text.rfind(f"<{tag_content}>")
    if start == -1:
        start = response_text.rfind(f"<{tag_content}")
    evaluation = response_text[:start].strip()

    fields = {}
    for pair in tag_content.split(";"):
        pair = pair.strip()
        if ":" in pair:
            key, value = pair.split(":", 1)
            fields[key.strip().lower()] = value.strip()

    try:
        affection_gain = int(fields.get("affection", "1"))
    except ValueError:
        affection_gain = 1

    is_food = fields.get("is_food", "否") == "是"
    food_desc = fields.get("food_desc", "").strip()
    if food_desc in ("", "无"):
        food_desc = ""
    scene_desc = fields.get("scene_desc", "").strip()
    if scene_desc in ("", "无"):
        scene_desc = ""

    return {
        "evaluation": evaluation,
        "affection_gain": affection_gain,
        "is_food": is_food,
        "food_desc": food_desc,
        "scene_desc": scene_desc,
    }


def get_affection_level(points: int) -> dict:
    """根据好感度点数返回等级信息"""
    current = AFFECTION_LEVELS[0]
    for level in AFFECTION_LEVELS:
        if points >= level["min"]:
            current = level
    return current


def get_next_level(points: int) -> dict | None:
    """获取下一等级信息"""
    for level in AFFECTION_LEVELS:
        if points < level["min"]:
            return level
    return None


class AffectionDB:
    """好感度 SQLite 数据库管理"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS affection (
                    user_id TEXT PRIMARY KEY,
                    affection_points INTEGER DEFAULT 0,
                    daily_gain INTEGER DEFAULT 0,
                    last_date TEXT DEFAULT '',
                    last_interact TEXT DEFAULT '',
                    last_gift_date TEXT DEFAULT '',
                    last_confession TEXT DEFAULT ''
                )
            """)
            conn.commit()

    def get(self, user_id: str) -> dict:
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM affection WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                if row["last_date"] != today:
                    conn.execute(
                        "UPDATE affection SET daily_gain = 0, last_date = ? WHERE user_id = ?",
                        (today, user_id),
                    )
                    conn.commit()
                return dict(row)
            else:
                conn.execute(
                    "INSERT INTO affection (user_id, affection_points, daily_gain, last_date, last_interact) "
                    "VALUES (?, 0, 0, ?, ?)",
                    (user_id, today, today),
                )
                conn.commit()
                return {
                    "user_id": user_id,
                    "affection_points": 0,
                    "daily_gain": 0,
                    "last_date": today,
                    "last_interact": today,
                    "last_gift_date": "",
                    "last_confession": "",
                }

    def update(self, user_id: str, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [user_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE affection SET {sets} WHERE user_id = ?", values
            )
            conn.commit()


class AffectionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        data_dir = os.path.join(os.path.dirname(__file__), "data")
        self.db = AffectionDB(os.path.join(data_dir, "affection.db"))
        self.db.init()

        # 从 WebUI 配置面板读取（带默认值）
        cfg = config or {}
        self.daily_cap = int(cfg.get("daily_cap", 50))
        self.chat_chance = float(cfg.get("chat_chance", 0.15))
        self.chat_amount = int(cfg.get("chat_amount", 1))
        self.confession_cooldown_hours = int(cfg.get("confession_cooldown_hours", 6))
        self.fluctuation_min = int(cfg.get("fluctuation_min", -5))
        self.fluctuation_max = int(cfg.get("fluctuation_max", 5))

        # 启动每日浮动后台任务
        asyncio.create_task(self._daily_fluctuation_loop())

    # ==================== 被动聊天监听 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_chat_affection(self, event: AstrMessageEvent):
        """监听所有消息，概率触发好感度增长"""
        uid = event.get_sender_id()
        if not uid:
            return

        data = self.db.get(uid)

        # 超过每日上限
        if data["daily_gain"] >= self.daily_cap:
            return

        # 概率触发
        if random.random() > self.chat_chance:
            return

        gain = min(self.chat_amount, self.daily_cap - data["daily_gain"])
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

        self.db.update(
            uid,
            affection_points=data["affection_points"] + gain,
            daily_gain=data["daily_gain"] + gain,
            last_interact=today,
        )

    # ==================== 命令注册 ====================

    @filter.command("haogandu")
    async def cmd_affection(self, event: AstrMessageEvent):
        """查询你与棱镜娘的好感度状态"""
        uid = event.get_sender_id()
        data = self.db.get(uid)
        points = data["affection_points"]
        level = get_affection_level(points)
        next_lv = get_next_level(points)

        lines = [
            f" 好感度状态",
            f"当前等级: {level['name']}",
            f"好感度点数: {points}",
            f"今日已获得: {data['daily_gain']} / {self.daily_cap}",
        ]
        if next_lv:
            remaining = next_lv["min"] - points
            lines.append(f"距离「{next_lv['name']}」还差 {remaining} 点")
        else:
            lines.append("已满级！你是棱镜娘最重要的人 ✨")

        yield event.plain_result("\n".join(lines))

    @filter.command("投喂")
    async def cmd_feed(self, event: AstrMessageEvent, food: str = ""):
        """给棱镜娘送食物，支持图片或文字描述。AI 评价并决定好感度奖励"""
        uid = event.get_sender_id()
        uname = event.get_sender_name()
        data = self.db.get(uid)

        # 从消息链中提取图片
        image_url = None
        if event.message_obj and event.message_obj.message:
            for comp in event.message_obj.message:
                if isinstance(comp, ImageComp):
                    image_url = getattr(comp, "url", None) or getattr(comp, "file", None)
                    break

        # 必须有文字或图片
        if not food.strip() and not image_url:
            yield event.plain_result(
                "你想给我吃什么呀？拍张照片或者告诉我名字嘛～\n例如: /投喂 草莓蛋糕\n或者直接 /投喂 然后上传一张食物图片！"
            )
            return

        # 每日投喂限制
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        if data.get("last_gift_date") == today:
            yield event.plain_result(
                "今天已经投喂过啦，明天再给我带好吃的吧！ "
            )
            return

        level_name = get_affection_level(data["affection_points"])["name"]

        # AI 评价食物（图片优先）
        try:
            if image_url:
                result = await self._call_llm_vision(event, image_url, uname, level_name)
            else:
                prompt = _feeding_prompt(uname, food, level_name)
                result = await self._call_llm(event, prompt)

            if result:
                parsed = _parse_feeding_response(result)
            else:
                parsed = {
                    "evaluation": _feeding_fallback(food or "这个"),
                    "affection_gain": random.randint(1, 10),
                    "is_food": True,
                    "food_desc": food or "美食",
                    "scene_desc": "",
                }
        except Exception:
            parsed = {
                "evaluation": _feeding_fallback(food or "这个"),
                "affection_gain": random.randint(1, 10),
                "is_food": True,
                "food_desc": food or "美食",
                "scene_desc": "",
            }

        evaluation = parsed["evaluation"]
        affection_gain = max(1, min(parsed["affection_gain"], 20))
        is_food = parsed["is_food"]
        food_desc = parsed.get("food_desc", food or "美食")
        scene_desc = parsed.get("scene_desc", "")

        # 更新数据库
        new_points = data["affection_points"] + affection_gain
        new_level = get_affection_level(new_points)
        self.db.update(
            uid,
            affection_points=new_points,
            last_gift_date=today,
            last_interact=today,
        )

        # 构建回复
        result_text = evaluation
        if is_food and food_desc:
            result_text += f"\n  尝出来了，是「{food_desc}」呢！"
        if scene_desc:
            result_text += f"\n  {scene_desc}"

        if is_food:
            result_text += f"\n\n 好感度 +{affection_gain} | 当前: {new_points} ({new_level['name']})"
        else:
            result_text += f"\n\n  这个好像不太像食物呢...不过还是谢谢你！好感度 +{affection_gain}"

        yield event.plain_result(result_text)

    @filter.command("忏悔")
    async def cmd_confess(self, event: AstrMessageEvent, content: str = ""):
        """向棱镜娘忏悔，AI 回应并决定好感度变化"""
        uid = event.get_sender_id()
        uname = event.get_sender_name()
        data = self.db.get(uid)
        level = get_affection_level(data["affection_points"])

        if not content.strip():
            yield event.plain_result(
                "你想忏悔什么？告诉我吧...\n例如: /忏悔 我今天偷吃了你的零食"
            )
            return

        # 冷却检查（用独立字段）
        last_conf = data.get("last_confession", "")
        if last_conf:
            try:
                last_dt = datetime.fromisoformat(last_conf)
                cooldown = timedelta(hours=self.confession_cooldown_hours)
                now = datetime.now(BEIJING_TZ)
                if now - last_dt < cooldown:
                    remain = cooldown - (now - last_dt)
                    hours = remain.seconds // 3600
                    mins = (remain.seconds % 3600) // 60
                    yield event.plain_result(
                        f"你才刚刚忏悔过呢...让我消化一下，{hours}小时{mins}分钟后再来吧。"
                    )
                    return
            except ValueError:
                pass

        # AI 生成忏悔回应（要求输出 <affection:+N> 或 <affection:-N> 标签）
        try:
            prompt = _confession_prompt(uname, content, level["name"], data["affection_points"])
            result = await self._call_llm(event, prompt)
            if result:
                response, change = _parse_confession_response(result)
            else:
                response = _confession_fallback(uname, content)
                change = 0
        except Exception:
            response = _confession_fallback(uname, content)
            change = 0

        new_points = max(0, data["affection_points"] + change)
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        self.db.update(uid, affection_points=new_points, last_confession=today)

        if change != 0:
            sign = "+" if change >= 0 else ""
            yield event.plain_result(
                f"{response}\n\n 好感度 {sign}{change} | 当前: {new_points}"
            )
        else:
            yield event.plain_result(response)

    # ==================== LLM 人设注入 ====================

    @filter.on_llm_request()
    async def inject_affection_persona(self, event: AstrMessageEvent, req):
        """根据好感度等级注入人设提示"""
        uid = event.get_sender_id()
        if not uid:
            return
        data = self.db.get(uid)
        level = get_affection_level(data["affection_points"])
        persona = (
            f"\n[好感度系统] 当前与用户的好感度等级: {level['name']}\n"
            f"{level['prompt']}\n"
        )
        if hasattr(req, "system_prompt"):
            req.system_prompt += persona

    # ==================== 每日浮动（后台任务） ====================

    async def _daily_fluctuation_loop(self):
        """每日凌晨随机浮动所有用户好感度"""
        while True:
            now = datetime.now(BEIJING_TZ)
            next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            wait_seconds = (next_midnight - now).total_seconds()
            await asyncio.sleep(wait_seconds)

            try:
                with self.db._connect() as conn:
                    rows = conn.execute(
                        "SELECT user_id, affection_points FROM affection"
                    ).fetchall()
                    for row in rows:
                        fluctuation = random.randint(self.fluctuation_min, self.fluctuation_max)
                        new_pts = max(0, row["affection_points"] + fluctuation)
                        conn.execute(
                            "UPDATE affection SET affection_points = ? WHERE user_id = ?",
                            (new_pts, row["user_id"]),
                        )
                    conn.commit()
                    logger.info(
                        f"[Affection] 每日好感度浮动完成，共 {len(rows)} 位用户"
                    )
            except Exception as e:
                logger.error(f"[Affection] 每日浮动失败: {e}")

    # ==================== 辅助方法 ====================

    async def _call_llm(self, event: AstrMessageEvent, prompt: str) -> str | None:
        """调用 AstrBot 内置 LLM"""
        try:
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning("[Affection] 无法获取 LLM provider ID")
                return None
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            return resp.completion_text if resp else None
        except Exception as e:
            logger.error(f"[Affection] LLM 调用失败: {e}")
            return None

    async def _call_llm_vision(
        self, event: AstrMessageEvent, image_url: str, user_name: str, level: str
    ) -> str | None:
        """调用 AstrBot LLM 进行图片识别（投喂食物分析）"""
        try:
            # 下载图片
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                image_bytes = resp.content

            # 构建带图片的消息
            from astrbot.api.provider import ProviderRequest
            umo = event.unified_msg_origin
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return None

            provider = await self.context.get_provider_by_id(provider_id)
            if not provider:
                return None

            req = ProviderRequest(
                prompt=_feeding_vision_prompt(user_name, level),
                image_bytes=image_bytes,
            )
            llm_resp = await provider.text_chat(req)
            return llm_resp.completion_text if llm_resp else None
        except Exception as e:
            logger.error(f"[Affection] 图片投喂识别失败: {e}")
            return None

    async def terminate(self):
        logger.info("[Affection] 插件已卸载")


# ==================== Prompt 模板 ====================

def _feeding_prompt(user_name: str, food: str, level: str) -> str:
    return f"""你是一个有鲜明性格的AI助手，名叫"棱镜娘"。用户{user_name}说他给你带来了「{food}」。

请用你的角色性格，对这个投喂行为写一段简短有趣的评价（40字以内）。

【重要】你必须在评价文本之后，以以下格式输出结构化标签：
<affection:数字;is_food:是/否;food_desc:食物简短描述;scene_desc:场景描述或留空>

规则：
- affection: 好感度增加值（1~20），越好吃越用心数值越高。明显不是食物的（如石头、作业）给1~3。
- is_food: 描述的东西是食物就填"是"，否则填"否"。
- food_desc: 如果是食物，用简短名词描述（5字以内）。不是就填"无"。
- scene_desc: 这个食物让你联想到什么场景（10字以内，没有填"无"）。

当前好感度等级: {level}

示例：哇！草莓蛋糕！你怎么知道我最爱这个～<affection:15;is_food:是;food_desc:草莓蛋糕;scene_desc:温馨下午茶>

直接输出评价和标签，不要任何额外前缀。"""


def _confession_prompt(user_name: str, content: str, level: str, points: int) -> str:
    return f"""你是一个有鲜明性格的AI助手，名叫"棱镜娘"。用户{user_name}向你忏悔，内容是：

"{content}"

你和该用户当前的好感度: {points} 点 ({level})

请用你的角色性格回应这个忏悔。语气要符合{level}的关系程度。

【重要】你必须在回应之后，输出一个标签来表明好感度变化：
<affection:+数字> 表示好感度增加，<affection:-数字> 表示好感度减少。

规则：
- 真诚的忏悔、道歉给 +1~+8
- 敷衍或搞笑的忏悔给 -3~0
- 特别感人的忏悔可以给 +10

示例：我明白了...谢谢你愿意告诉我这些。<affection:+5>

50字以内，直接输出回应和标签，不要任何额外前缀。"""


def _parse_confession_response(text: str) -> tuple[str, int]:
    """解析忏悔 AI 回复中的 <affection:+/-N> 标签"""
    match = re.search(r"<affection:([+-]?\d+)>", text)
    if match:
        change = int(match.group(1))
        response = text[:match.start()].strip() + text[match.end():].strip()
        return response, change
    return text, 0


def _feeding_vision_prompt(user_name: str, level: str) -> str:
    return f"""你是一个有鲜明性格的AI助手，名叫"棱镜娘"。用户{user_name}给你投喂了一张食物图片。

请仔细看这张图片，用你的角色性格评价这个食物（40字以内）。

【重要】你必须在评价文本之后，以以下格式输出结构化标签：
<affection:数字;is_food:是/否;food_desc:食物简短描述;scene_desc:场景描述或留空>

规则：
- affection: 好感度增加值（1~20），看起来越好吃/越精致，数值越高。如果不是食物给1~3。
- is_food: 图片内容是食物就填"是"，否则填"否"。
- food_desc: 简短描述这是什么食物（5字以内）。不是食物填"无"。
- scene_desc: 这个食物让你联想到什么场景（10字以内，没有填"无"）。

当前好感度等级: {level}

示例：哇！是草莓蛋糕！看起来好好吃～<affection:15;is_food:是;food_desc:草莓蛋糕;scene_desc:温馨下午茶>

直接输出评价和标签，不要任何额外前缀。"""


def _feeding_fallback(food: str) -> str:
    """投喂备用回复（LLM不可用时）"""
    responses = [
        f"哇，{food}！你怎么知道我喜欢吃这个的～",
        f"嗯嗯...{food}好好吃！谢谢投喂！",
        f"嘿嘿，{food}！今天是什么好日子呀？",
        f"嗷呜～一口吃掉{food}！满足～",
    ]
    return random.choice(responses)


def _confession_fallback(user_name: str, content: str) -> str:
    """忏悔备用回复"""
    responses = [
        f"{user_name}...我知道了，没关系的。",
        f"嗯，我听着呢。{user_name}，谢谢你愿意告诉我。",
        f"这样啊...我理解你的心情。",
    ]
    return random.choice(responses)
