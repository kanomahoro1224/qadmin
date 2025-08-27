#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OneBot V11 协议的群聊申请交互式审核机器人 (生产环境优化版 - 回复式交互)。

功能:
1. 监听指定群聊的加群申请事件。
2. 如果申请者的验证消息匹配预设的“暗号”，则自动通过。
3. 支持两种验证消息模式：普通模式和问题模式（自动提取答案）。
4. 如果不匹配，则将申请详情转发到指定的管理员群。
5. 管理员通过【回复/引用】审核通知消息，并使用精简指令 (`/a` 接受, `/d [理由]` 拒绝) 来处理申请。
   (兼容 `reply` 对象和 `[CQ:reply]` 两种事件格式)
6. 可配置是否在操作后发送“操作成功”的反馈消息。
7. 定期自动同步待处理请求的状态，清理已被处理的申请，防止状态不一致。
8. 强大的自动重试机制和全异步I/O。
"""

import asyncio
import json
import logging
import os
import sys
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

import httpx
import websockets
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# --- 1. 配置模块 ---
@dataclass
class Config:
    """应用配置类，通过环境变量加载"""
    websocket_uri: str = os.getenv("WEBSOCKET_URI", "ws://127.0.0.1:3001")
    onebot_http_api_url: str = os.getenv("ONEBOT_HTTP_API_URL", "http://127.0.0.1:3000")
    
    target_group_ids_str: str = os.getenv("TARGET_GROUP_IDS", "00000000")
    admin_group_ids_str: str = os.getenv("ADMIN_GROUP_IDS", "00000000")
    allowed_comments_str: str = os.getenv("ALLOWED_COMMENTS", "我是natsuki,v我50")
    
    question_mode_enabled_str: str = os.getenv("QUESTION_MODE_ENABLED", "true")
    send_feedback_on_command_str: str = os.getenv("SEND_FEEDBACK_ON_COMMAND", "true")

    target_group_ids: List[int] = field(default_factory=list, init=False)
    admin_group_ids: List[int] = field(default_factory=list, init=False)
    allowed_comments: List[str] = field(default_factory=list, init=False)
    question_mode_enabled: bool = field(default=False, init=False)
    send_feedback_on_command: bool = field(default=True, init=False)

    state_sync_interval_seconds: int = int(os.getenv("STATE_SYNC_INTERVAL_SECONDS", "600"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    def __post_init__(self):
        self.target_group_ids = self._parse_int_list(self.target_group_ids_str)
        self.admin_group_ids = self._parse_int_list(self.admin_group_ids_str)
        self.allowed_comments = [item.strip() for item in self.allowed_comments_str.split(',') if item.strip()]
        
        self.question_mode_enabled = self._parse_bool(self.question_mode_enabled_str)
        self.send_feedback_on_command = self._parse_bool(self.send_feedback_on_command_str)
        
        if not self.target_group_ids:
            logging.warning("警告: 未配置任何目标群组 (TARGET_GROUP_IDS)。")
        if not self.admin_group_ids:
            logging.warning("警告: 未配置任何管理员群组 (ADMIN_GROUP_IDS)。")

    @staticmethod
    def _parse_int_list(s: str) -> List[int]:
        return [int(gid.strip()) for gid in s.split(',') if gid.strip().isdigit()]

    @staticmethod
    def _parse_bool(s: str) -> bool:
        return s.lower() in ('true', '1', 'yes', 'y')

# --- 2. 全局状态与锁 ---
PENDING_REQUESTS: Dict[int, Dict[str, Any]] = {}
MESSAGE_ID_TO_USER_ID: Dict[int, int] = {}
_state_lock = asyncio.Lock()
BOT_QQ_ID: Optional[int] = None

# --- 3. 日志设置 ---
config = Config()
logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# --- 4. API 客户端 ---
class APIClient:
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip('/')
        self._session = httpx.AsyncClient(timeout=10.0,
                                          # 在 DEBUG 模式下启用 HTTPX 日志
                                          event_hooks={'request': [self._log_request], 'response': [self._log_response]} if config.log_level == 'DEBUG' else {})

    async def _log_request(self, request: httpx.Request):
        logger.debug(f"HTTP Request: {request.method} {request.url}")

    async def _log_response(self, response: httpx.Response):
        await response.aread()
        logger.debug(f"HTTP Response: {response.status_code} {response.request.url}")

    async def close(self):
        await self._session.aclose()

    _retry_decorator = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )

    @_retry_decorator
    async def _send_api_request(self, endpoint: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{self._base_url}/{endpoint}"
        try:
            response = await self._session.post(url, json=payload)
            response.raise_for_status()
            response_data = response.json()
            if response_data.get("status") != "ok":
                logger.warning(f"API 请求 '{endpoint}' 业务状态异常: {response_data.get('wording', '无详细信息')}")
            return response_data
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            logger.error(f"API 请求 '{endpoint}' 失败: {e}. Payload: {payload}")
            raise
        except json.JSONDecodeError:
            logger.error(f"解析 API 响应失败，收到的不是有效的 JSON。Endpoint: {endpoint}")
            return None

    async def handle_group_add_request(self, flag: str, approve: bool, reason: str = "") -> None:
        payload = {"flag": flag, "approve": approve}
        if not approve and reason:
            payload["reason"] = reason
        await self._send_api_request("set_group_add_request", payload)

    async def send_group_message(self, group_id: int, message: str) -> Optional[int]:
        response = await self._send_api_request("send_group_msg", {"group_id": group_id, "message": message})
        if response and response.get("status") == "ok" and response.get("data"):
            return response["data"].get("message_id")
        return None

    async def get_user_nickname(self, user_id: int) -> str:
        response = await self._send_api_request("get_stranger_info", {"user_id": user_id})
        return response["data"].get("nickname", str(user_id)) if response and response.get("data") else str(user_id)

    async def get_group_member_list(self, group_id: int) -> Optional[List[Dict[str, Any]]]:
        response = await self._send_api_request("get_group_member_list", {"group_id": group_id})
        return response.get("data") if response else None

api_client = APIClient(config.onebot_http_api_url)

# --- 5. 业务逻辑 ---

def extract_replied_message_id(data: Dict[str, Any]) -> Optional[int]:
    """
    从事件数据中提取被回复/引用的消息ID。
    兼容 `reply` 对象和 `[CQ:reply,id=...]` 两种格式。
    """
    # 方式一：检查独立的 reply 对象
    if 'reply' in data and data['reply'] and 'message_id' in data['reply']:
        return data['reply']['message_id']
    
    # 方式二：检查 raw_message 中的 [CQ:reply]
    raw_message = data.get('raw_message', '')
    match = re.search(r'\[CQ:reply,id=(-?\d+)\]', raw_message)
    if match:
        return int(match.group(1))
        
    return None

def parse_reply_command(text: str) -> Optional[Tuple[str, str]]:
    """
    解析回复式指令，清理所有前置CQ码。
    格式: /a 或 /d [理由]
    """
    # 移除所有起始的CQ码（如reply, at）和前导空格
    cleaned_text = re.sub(r'^(?:\[CQ:[^\]]+\]\s*)+', '', text).strip()
    
    if cleaned_text.startswith('/a'):
        # 确保 /a 后面没有多余的字符
        parts = cleaned_text.split(maxsplit=1)
        if parts[0] == '/a':
            return 'a', ''
    elif cleaned_text.startswith('/d'):
        reason = cleaned_text[2:].lstrip()
        return 'd', reason
        
    return None

def extract_answer_from_comment(comment: str, is_question_mode: bool) -> str:
    if not is_question_mode: return comment
    answer_separator = "答案："
    if answer_separator in comment:
        try: return comment.split(answer_separator, 1)[1].strip()
        except IndexError: return ""
    return comment

async def process_join_request(data: Dict[str, Any]):
    group_id, user_id, flag = data.get('group_id'), data.get('user_id'), data.get('flag')
    if not all([group_id, user_id, flag]): return

    if group_id not in config.target_group_ids: return

    comment = data.get("comment", "")
    logger.info(f"处理用户 {user_id} 对目标群 {group_id} 的申请。原始验证消息: '{comment}'")

    effective_answer = extract_answer_from_comment(comment, config.question_mode_enabled)
    if effective_answer != comment:
        logger.info(f"问题模式已启用，提取出的答案为: '{effective_answer}'")

    if effective_answer in config.allowed_comments:
        logger.info(f"用户 {user_id} 使用了暗号 '{effective_answer}'，自动通过。")
        await api_client.handle_group_add_request(flag, True)
        return

    logger.info(f"用户 {user_id} 的申请需要人工审核，正在通知管理员...")
    nickname = await api_client.get_user_nickname(user_id)
    
    notification_message = (
        f"【加群待审核】\n"
        f"申请群聊: {group_id}\n"
        f"申请人:「{nickname}」({user_id})\n"
        f"验证信息: {comment or '无'}\n"
        f"--------------------\n"
        f"请管理员【回复/引用】此消息审核：\n"
        f"接受: /a\n"
        f"拒绝: /d [可选理由]"
    )
    
    sent_message_ids = []
    for admin_group_id in config.admin_group_ids:
        message_id = await api_client.send_group_message(admin_group_id, notification_message)
        if message_id:
            sent_message_ids.append(message_id)
    
    if sent_message_ids:
        async with _state_lock:
            PENDING_REQUESTS[user_id] = {"flag": flag, "group_id": group_id, "nickname": nickname, "message_ids": sent_message_ids}
            for msg_id in sent_message_ids:
                MESSAGE_ID_TO_USER_ID[msg_id] = user_id
        logger.info(f"已为用户 {user_id} 创建待处理请求，并关联了 {len(sent_message_ids)} 条通知消息。")
    else:
        logger.error(f"无法发送任何审核通知，用户 {user_id} 的请求可能无法被处理。")

async def execute_command(command: str, reason: str, target_user_id: int, admin_group_id: int):
    async with _state_lock:
        request_info = PENDING_REQUESTS.pop(target_user_id, None)
        if request_info:
            for msg_id in request_info.get("message_ids", []):
                MESSAGE_ID_TO_USER_ID.pop(msg_id, None)

    if not request_info:
        logger.warning(f"操作失败: 在群 {admin_group_id} 中收到对用户 {target_user_id} 的指令，但未找到其待处理请求。")
        if config.send_feedback_on_command:
            await api_client.send_group_message(admin_group_id, f"操作失败: 未找到用户 {target_user_id} 的待处理请求，可能已被处理或过期。")
        return

    nickname = request_info.get('nickname', str(target_user_id))
    
    if command == 'a':
        await api_client.handle_group_add_request(request_info["flag"], True)
        logger.info(f"管理员已同意用户 {target_user_id} 的申请。")
        if config.send_feedback_on_command:
            await api_client.send_group_message(admin_group_id, f"操作成功: 已同意「{nickname}」({target_user_id}) 的加群请求。")
    elif command == 'd':
        rejection_reason = reason or "管理员拒绝了你的请求。"
        await api_client.handle_group_add_request(request_info["flag"], False, rejection_reason)
        logger.info(f"管理员已拒绝用户 {target_user_id} 的申请，理由: {rejection_reason}")
        if config.send_feedback_on_command:
            await api_client.send_group_message(admin_group_id, f"操作成功: 已拒绝「{nickname}」({target_user_id}) 的加群请求。")

async def process_command_message(data: Dict[str, Any]):
    logger.debug(f"收到群消息事件: {data}")

    if data.get('sender', {}).get('role') not in ('owner', 'admin'):
        #logger.debug("消息发送者非管理员，忽略。")
        #return
        logger.debug("消息发送者非管理员，但仍继续执行（なつき也不知道为什么反正产品要求是这样）")
    
    sender_group_id = data.get("group_id")
    if sender_group_id not in config.admin_group_ids:
        logger.debug(f"消息来自非管理员群 {sender_group_id}，忽略。")
        return

    replied_message_id = extract_replied_message_id(data)
    if not replied_message_id:
        logger.debug("消息非引用/回复，忽略。")
        return
        
    async with _state_lock:
        target_user_id = MESSAGE_ID_TO_USER_ID.get(replied_message_id)

    if not target_user_id:
        logger.debug(f"收到对消息 {replied_message_id} 的回复，但它不对应任何待处理请求。")
        return

    command_tuple = parse_reply_command(data.get("raw_message", ""))
    if not command_tuple:
        logger.debug("回复内容未匹配任何有效指令，忽略。")
        return

    command, reason = command_tuple
    logger.info(f"在管理员群 {sender_group_id} 中收到对用户 {target_user_id} 的回复指令: '{command}', 理由: '{reason}'")
    await execute_command(command, reason, target_user_id, sender_group_id)

async def sync_pending_requests_status():
    while True:
        await asyncio.sleep(config.state_sync_interval_seconds)
        async with _state_lock:
            if not PENDING_REQUESTS: continue
            pending_copy = dict(PENDING_REQUESTS)
        logger.info(f"开始状态同步，当前有 {len(pending_copy)} 个待处理请求...")
        requests_by_group: Dict[int, List[int]] = {}
        for user_id, info in pending_copy.items():
            requests_by_group.setdefault(info["group_id"], []).append(user_id)
        cleared_users = []
        for group_id, user_ids in requests_by_group.items():
            member_list = await api_client.get_group_member_list(group_id)
            if member_list is None: continue
            current_member_ids = {member['user_id'] for member in member_list}
            for user_id in user_ids:
                if user_id in current_member_ids:
                    logger.info(f"状态同步: 用户 {user_id} 已在群 {group_id} 中，清理其待处理请求。")
                    cleared_users.append(user_id)
        if cleared_users:
            async with _state_lock:
                for user_id in cleared_users:
                    request_info = PENDING_REQUESTS.pop(user_id, None)
                    if request_info:
                        for msg_id in request_info.get("message_ids", []):
                            MESSAGE_ID_TO_USER_ID.pop(msg_id, None)
            logger.info(f"状态同步完成，清理了 {len(cleared_users)} 个已处理的请求。")
        else:
            logger.info("状态同步完成，未发现可清理的请求。")

# --- 6. 主程序循环 ---
@retry(
    wait=wait_exponential(multiplier=1, min=5, max=30),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.ERROR),
)
async def main_loop():
    global BOT_QQ_ID
    logger.info(f"正在连接到 WebSocket: {config.websocket_uri}")
    async with websockets.connect(config.websocket_uri) as websocket:
        logger.info("WebSocket 连接成功！开始监听事件...")
        async for message in websocket:
            try:
                data = json.loads(message)
                if not BOT_QQ_ID and "self_id" in data:
                    BOT_QQ_ID = data["self_id"]
                    logger.info(f"成功获取机器人QQ号: {BOT_QQ_ID}")
                post_type = data.get("post_type")
                if post_type == "meta_event": continue
                if post_type == "request" and data.get("request_type") == "group" and data.get("sub_type") == "add":
                    asyncio.create_task(process_join_request(data))
                elif post_type == "message" and data.get("message_type") == "group":
                    asyncio.create_task(process_command_message(data))
            except json.JSONDecodeError:
                logger.warning(f"收到非 JSON 格式的消息: {message}")
            except Exception:
                logger.exception("处理单个事件时发生未知错误。")

async def main():
    logger.info("机器人交互式审核程序已启动...")
    logger.info(f"HTTP API 地址: {config.onebot_http_api_url}")
    logger.info(f"目标处理群组: {config.target_group_ids or '未配置'}")
    logger.info(f"管理员通知群组: {config.admin_group_ids or '未配置'}")
    logger.info(f"问题回答模式: {'已启用' if config.question_mode_enabled else '已禁用'}")
    logger.info(f"指令执行后发送反馈: {'是' if config.send_feedback_on_command else '否'}")

    sync_task = asyncio.create_task(sync_pending_requests_status())
    logger.info("后台状态同步任务已启动。")
    try:
        await main_loop()
    except Exception:
        logger.critical("主循环重试多次后仍然失败，程序退出。")
    finally:
        sync_task.cancel()
        await api_client.close()
        logger.info("资源已清理，程序退出。")

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError: pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已由用户手动停止。")
    except Exception as e:
        logger.critical(f"程序启动时发生致命错误: {e}")
        sys.exit(1)
