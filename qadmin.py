#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OneBot V11 协议的群聊申请交互式审核机器人 (生产环境优化版)。

功能:
1. 监听指定群聊的加群申请事件。
2. 如果申请者的验证消息匹配预设的“暗号”，则自动通过。
3. 支持两种验证消息模式：
   - 普通模式：直接匹配验证消息全文。
   - 问题模式：当QQ群设置为“回答问题”时，自动提取“答案：”后的内容进行匹配。
4. 如果不匹配，则将申请详情转发到指定的管理员群，由管理员通过指令进行审核。
5. 管理员可以在群内通过 @机器人 并使用指令 (`/a` 接受, `/d` 拒绝) 来处理申请。
6. 定期自动同步待处理请求的状态，清理已被手动处理或已入群的申请，防止状态不一致。
7. 强大的自动重试机制，应对网络波动，极大减少人工干预。
8. 全异步I/O，性能更佳。

"""

import asyncio
import json
import logging
import os
import sys
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

# --- 1. 配置模块 (使用 Dataclass 封装) ---
@dataclass
class Config:
    """应用配置类，通过环境变量加载"""
    websocket_uri: str = os.getenv("WEBSOCKET_URI", "ws://127.0.0.1:3001")
    onebot_http_api_url: str = os.getenv("ONEBOT_HTTP_API_URL", "http://127.0.0.1:3000")
    
    # 使用 post_init 处理列表和布尔类型的环境变量
    target_group_ids_str: str = os.getenv("TARGET_GROUP_IDS", "00000000")
    admin_group_ids_str: str = os.getenv("ADMIN_GROUP_IDS", "00000000")
    allowed_comments_str: str = os.getenv("ALLOWED_COMMENTS", "喵喵喵,v我50,我是natsuki")
    
    # 问题模式开关
    question_mode_enabled_str: str = os.getenv("QUESTION_MODE_ENABLED", "false")

    target_group_ids: List[int] = field(default_factory=list, init=False)
    admin_group_ids: List[int] = field(default_factory=list, init=False)
    allowed_comments: List[str] = field(default_factory=list, init=False)
    question_mode_enabled: bool = field(default=False, init=False)

    state_sync_interval_seconds: int = int(os.getenv("STATE_SYNC_INTERVAL_SECONDS", "600"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    def __post_init__(self):
        """在初始化后处理环境变量字符串，转换为正确的类型。"""
        self.target_group_ids = self._parse_int_list(self.target_group_ids_str)
        self.admin_group_ids = self._parse_int_list(self.admin_group_ids_str)
        self.allowed_comments = [item.strip() for item in self.allowed_comments_str.split(',') if item.strip()]
        
        # 将字符串 'true', '1', 'yes' (不区分大小写) 解析为布尔值 True
        self.question_mode_enabled = self.question_mode_enabled_str.lower() in ('true', '1', 'yes')
        
        # 启动前进行配置检查，提供明确的警告
        if not self.target_group_ids:
            logging.warning("警告: 未配置任何目标群组 (TARGET_GROUP_IDS)，程序将不会处理任何加群申请。")
        if not self.admin_group_ids:
            logging.warning("警告: 未配置任何管理员群组 (ADMIN_GROUP_IDS)，需要人工审核的申请将无法被通知。")

    @staticmethod
    def _parse_int_list(s: str) -> List[int]:
        return [int(gid.strip()) for gid in s.split(',') if gid.strip().isdigit()]

# --- 2. 全局状态与锁 ---
PENDING_REQUESTS: Dict[int, Dict[str, Any]] = {}
_pending_requests_lock = asyncio.Lock()
BOT_QQ_ID: Optional[int] = None

# --- 3. 日志设置 ---
config = Config()
logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# --- 4. API 客户端 (核心优化) ---
class APIClient:
    """
    封装 OneBot HTTP API 调用，包含自动重试和异步 I/O。
    """
    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip('/')
        # 使用 session 来复用连接，提升性能
        self._session = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        """关闭 HTTP 客户端会话。"""
        await self._session.aclose()

    # 定义重试策略：指数退避，最多尝试3次，只针对网络相关异常
    _retry_decorator = retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )

    @_retry_decorator
    async def _send_api_request(self, endpoint: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        向 OneBot HTTP API 发送一个 POST 请求的通用函数 (异步 + 重试)。
        """
        url = f"{self._base_url}/{endpoint}"
        logger.debug(f"  - API URL: {url}")
        logger.debug(f"  - API Payload: {payload}")
        try:
            response = await self._session.post(url, json=payload)
            response.raise_for_status()  # 对 4xx/5xx 状态码抛出异常
            response_data = response.json()
            if response_data.get("status") != "ok":
                logger.warning(
                    f"API 请求 '{endpoint}' 业务状态异常: {response_data.get('wording', '无详细信息')}"
                )
            return response_data
        except httpx.RequestError as e:
            logger.error(f"API 请求 '{endpoint}' 网络错误: {e}. Payload: {payload}")
            raise  # 重新抛出，让 tenacity 捕获并重试
        except httpx.HTTPStatusError as e:
            logger.error(f"API 请求 '{endpoint}' HTTP 状态码错误: {e.response.status_code}. Payload: {payload}")
            # 对于客户端错误(4xx)，通常不应重试，这里可以根据需要细化
            if 400 <= e.response.status_code < 500:
                return None # 不重试，直接返回失败
            raise # 对于服务端错误(5xx)，重新抛出以重试
        except json.JSONDecodeError:
            logger.error(f"解析 API 响应失败，收到的不是有效的 JSON。Endpoint: {endpoint}")
            return None

    async def handle_group_add_request(self, flag: str, approve: bool, reason: str = "") -> None:
        payload = {"flag": flag, "approve": approve}
        if not approve and reason:
            payload["reason"] = reason
        await self._send_api_request("set_group_add_request", payload)

    async def send_group_message(self, group_id: int, message: str) -> None:
        await self._send_api_request("send_group_msg", {"group_id": group_id, "message": message})

    async def get_user_nickname(self, user_id: int) -> str:
        response = await self._send_api_request("get_stranger_info", {"user_id": user_id})
        return response["data"].get("nickname", str(user_id)) if response and response.get("data") else str(user_id)

    async def get_group_member_list(self, group_id: int) -> Optional[List[Dict[str, Any]]]:
        response = await self._send_api_request("get_group_member_list", {"group_id": group_id})
        return response.get("data") if response else None

# 初始化 API 客户端
api_client = APIClient(config.onebot_http_api_url)

# --- 5. 业务逻辑 (全异步化) ---

def parse_command(text: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """解析管理员指令。指令格式: /command /key1=value1 /key2=value with spaces"""
    text = text.strip()
    if not text.startswith('/'):
        return None
    
    parts = text.split('/')
    if len(parts) < 2:
        return None
    
    command = parts[1].strip()
    if not command:
        return None
        
    params = {}
    for part in parts[2:]:
        part = part.strip()
        if '=' in part:
            key, value = part.split('=', 1)
            params[key.strip()] = value.strip()
            
    return command, params

# 新增：辅助函数，用于从验证消息中提取有效答案
def extract_answer_from_comment(comment: str, is_question_mode: bool) -> str:
    """
    根据是否启用问题模式，从验证消息中提取用于匹配的答案。

    Args:
        comment: 原始的验证消息字符串。
        is_question_mode: 是否启用了问题模式的布尔标志。

    Returns:
        用于关键词匹配的有效答案字符串。
    """
    if not is_question_mode:
        return comment  # 普通模式，直接返回原文

    # 问题模式下，尝试解析
    answer_separator = "答案："
    if answer_separator in comment:
        try:
            # 分割字符串，并取“答案：”后面的部分
            answer = comment.split(answer_separator, 1)[1].strip()
            return answer
        except IndexError:
            # "答案：" 存在但后面没有内容，返回空字符串
            return ""
    
    # 如果问题模式开启，但消息格式不符，则返回原始消息作为兜底
    return comment


async def process_join_request(data: Dict[str, Any]):
    """核心业务：处理加群申请事件。"""
    group_id = data.get('group_id')
    user_id = data.get('user_id')
    flag = data.get('flag')
    if not all([group_id, user_id, flag]):
        logger.warning(f"收到不完整的加群请求事件，缺少关键字段: {data}")
        return

    if group_id not in config.target_group_ids:
        logger.debug(f"忽略来自非目标群 {group_id} 的申请。")
        return

    # 获取原始验证消息
    comment = data.get("comment", "")
    logger.info(f"处理用户 {user_id} 对目标群 {group_id} 的申请。原始验证消息: '{comment}'")

    # 根据配置提取有效答案用于匹配
    effective_answer = extract_answer_from_comment(comment, config.question_mode_enabled)
    
    # 如果提取出的答案与原文不同，打印日志以供调试
    if effective_answer != comment:
        logger.info(f"问题模式已启用，提取出的答案为: '{effective_answer}'")

    if effective_answer in config.allowed_comments:
        logger.info(f"用户 {user_id} 使用了暗号 '{effective_answer}'，自动通过。")
        await api_client.handle_group_add_request(flag, True)
        return

    logger.info(f"用户 {user_id} 的申请需要人工审核，正在通知管理员...")
    nickname = await api_client.get_user_nickname(user_id)
    
    async with _pending_requests_lock:
        PENDING_REQUESTS[user_id] = {"flag": flag, "group_id": group_id, "nickname": nickname}
    
    # 通知管理员时，发送完整的原始验证消息，保留上下文
    notification_message = (
        f"【加群待审核】\n"
        f"申请群聊: {group_id}\n"
        f"申请人:「{nickname}」({user_id})\n"
        f"验证信息: {comment or '无'}\n"
        f"--------------------\n"
        f"请管理员使用指令审核：\n"
        f"接受: @我 /a /qq={user_id}\n"
        f"拒绝: @我 /d /qq={user_id} /msg=可选理由"
    )
    
    for admin_group_id in config.admin_group_ids:
        await api_client.send_group_message(admin_group_id, notification_message)

async def execute_command(command: str, params: Dict[str, str], admin_group_id: int):
    """执行管理员从群聊中发出的指令。"""
    if 'qq' not in params or not params['qq'].isdigit():
        await api_client.send_group_message(admin_group_id, "指令错误: 缺少有效的 `/qq=<用户ID>` 参数。")
        return
        
    target_user_id = int(params['qq'])

    async with _pending_requests_lock:
        request_info = PENDING_REQUESTS.pop(target_user_id, None)
    
    if not request_info:
        await api_client.send_group_message(admin_group_id, f"操作失败: 未找到用户 {target_user_id} 的待处理请求，可能已被处理或过期。")
        return

    nickname = request_info.get('nickname', str(target_user_id))
    
    if command == 'a':
        await api_client.handle_group_add_request(request_info["flag"], True)
        await api_client.send_group_message(admin_group_id, f"操作成功: 已同意「{nickname}」({target_user_id}) 的加群请求。")
    elif command == 'd':
        reason = params.get('msg', "管理员拒绝了你的请求。")
        await api_client.handle_group_add_request(request_info["flag"], False, reason)
        await api_client.send_group_message(admin_group_id, f"操作成功: 已拒绝「{nickname}」({target_user_id}) 的加群请求。")
    else:
        async with _pending_requests_lock:
            PENDING_REQUESTS[target_user_id] = request_info
        await api_client.send_group_message(admin_group_id, f"未知指令 `/{command}`。可用指令: /a, /d。")

async def process_command_message(data: Dict[str, Any]):
    """处理群消息，判断是否为发给自己的、有权限的指令。"""
    if data.get('sender', {}).get('role') not in ('owner', 'admin'):
        return
    
    if not BOT_QQ_ID or f"[CQ:at,qq={BOT_QQ_ID}]" not in data.get("raw_message", ""):
        return
    sender_group_id = data.get("group_id")
    if sender_group_id not in config.admin_group_ids:
        return
    
    message_text = data.get("raw_message", "").replace(f"[CQ:at,qq={BOT_QQ_ID}]", "").strip()
    parsed = parse_command(message_text)
    if not parsed:
        return

    command, params = parsed
    logger.info(f"在管理员群 {sender_group_id} 中收到指令: '{command}' with params {params}")
    await execute_command(command, params, sender_group_id)

async def sync_pending_requests_status():
    """后台定时任务：同步并清理已失效的待处理请求。"""
    while True:
        await asyncio.sleep(config.state_sync_interval_seconds)
        
        if not PENDING_REQUESTS:
            continue
        
        logger.info("开始执行待处理请求的状态同步...")
        
        async with _pending_requests_lock:
            pending_copy = dict(PENDING_REQUESTS)

        requests_by_group: Dict[int, List[int]] = {}
        for user_id, info in pending_copy.items():
            requests_by_group.setdefault(info["group_id"], []).append(user_id)

        cleared_users = []
        for group_id, user_ids in requests_by_group.items():
            member_list = await api_client.get_group_member_list(group_id)
            if member_list is None:
                logger.warning(f"状态同步：获取群 {group_id} 成员列表失败，跳过该群（可能因API重试失败）。")
                continue
            
            current_member_ids = {member['user_id'] for member in member_list}
            for user_id in user_ids:
                if user_id in current_member_ids:
                    logger.info(f"状态同步: 用户 {user_id} 已在群 {group_id} 中，清理其待处理请求。")
                    cleared_users.append(user_id)
        
        if cleared_users:
            async with _pending_requests_lock:
                for user_id in cleared_users:
                    PENDING_REQUESTS.pop(user_id, None)
            logger.info(f"状态同步完成，清理了 {len(cleared_users)} 个已处理的请求。")
        else:
            logger.info("状态同步完成，未发现可清理的请求。")

# --- 6. 主程序循环 (增加 WebSocket 重连装饰器) ---
@retry(
    wait=wait_exponential(multiplier=1, min=5, max=30),
    retry=retry_if_exception_type(Exception),
    before_sleep=before_sleep_log(logger, logging.ERROR),
)
async def main_loop():
    """主循环，负责 WebSocket 连接、事件分发和异常重连。"""
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
                
                if post_type == "meta_event" and data.get("meta_event_type") == "heartbeat":
                    logger.debug("收到 OneBot 心跳事件。")
                    continue # 心跳事件，无需处理

                if post_type == "request" and data.get("request_type") == "group" and data.get("sub_type") == "add":
                    asyncio.create_task(process_join_request(data))
                elif post_type == "message" and data.get("message_type") == "group":
                    asyncio.create_task(process_command_message(data))

            except json.JSONDecodeError:
                logger.warning(f"收到非 JSON 格式的消息: {message}")
            except Exception:
                logger.exception("处理单个事件时发生未知错误。")

async def main():
    """程序主入口"""
    logger.info("机器人交互式审核程序已启动...")
    logger.info(f"HTTP API 地址: {config.onebot_http_api_url}")
    logger.info(f"目标处理群组: {config.target_group_ids or '未配置'}")
    logger.info(f"管理员通知群组: {config.admin_group_ids or '未配置'}")
    # 新增：在启动时打印问题模式的状态
    logger.info(f"问题回答模式: {'已启用' if config.question_mode_enabled else '已禁用'}")

    # 启动后台状态同步任务
    sync_task = asyncio.create_task(sync_pending_requests_status())
    logger.info("后台状态同步任务已启动。")

    try:
        await main_loop()
    except Exception:
        logger.critical("主循环重试多次后仍然失败，程序退出。请检查网络或 OneBot 框架状态。")
    finally:
        # 优雅地关闭资源
        sync_task.cancel()
        await api_client.close()
        logger.info("资源已清理，程序退出。")


if __name__ == "__main__":
    try:
        # 如果安装了 python-dotenv, 可以自动从 .env 文件加载环境变量
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass # 没有安装也无妨

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已由用户手动停止。")
    except Exception as e:
        logger.critical(f"程序启动时发生致命错误: {e}")
        sys.exit(1)
