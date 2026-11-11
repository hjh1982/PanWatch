"""Bark 直连推送（支持副标题/分组/点击链接，apprise 渠道不支持这些逐条参数）。"""

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_SERVER = "https://api.day.app"
MAX_BODY = 4800


async def send(
    key: str,
    title: str,
    body: str,
    *,
    server: str = DEFAULT_SERVER,
    subtitle: str = "",
    url: str = "",
    group: str = "",
) -> dict:
    """向 Bark 发送一条通知，返回 {"success": bool, "error"?: str}。"""
    if not key:
        return {"success": False, "error": "未配置 Bark 密钥"}
    if len(body.encode("utf-8")) > MAX_BODY:
        body = body.encode("utf-8")[:MAX_BODY].decode("utf-8", "ignore") + "\n…(内容过长已截断)"
    payload = {"title": title, "body": body}
    if subtitle:
        payload["subtitle"] = subtitle
    if url:
        payload["url"] = url
    if group:
        payload["group"] = group
    endpoint = f"{(server or DEFAULT_SERVER).rstrip('/')}/{key}"

    async def _post(trust_env: bool):
        async with httpx.AsyncClient(timeout=15, trust_env=trust_env) as client:
            return await client.post(endpoint, json=payload)

    try:
        try:
            resp = await _post(trust_env=True)
        except Exception as e:
            # 环境 NO_PROXY 含 "[::1]" 等方括号 IPv6 时 httpx 解析代理会失败，改直连重试
            if "Invalid port" not in str(e):
                raise
            logger.warning(f"环境代理配置无效，Bark 改直连: {e}")
            resp = await _post(trust_env=False)
        data = resp.json()
        if resp.status_code == 200 and data.get("code") == 200:
            return {"success": True}
        return {"success": False, "error": f"Bark 返回 {resp.status_code}: {data}"}
    except Exception as e:
        return {"success": False, "error": f"Bark 推送失败: {e}"}