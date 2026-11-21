# -*- coding: utf-8 -*-
"""线报双阶段 AI 客户端：显式路径注入、提示词热加载缓存、单次 LLM 调用并发信号量控制与 JSON 解析。"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from infrastructure.ai_api.json_parser import parse_llm_json
from infrastructure.ai_api.model_api import generate_content
from infrastructure.runtime_io import load_prompt_cached
from models.deal_constants import (
    DEFAULT_AI_TIMEOUT_SECONDS,
    PROMPT_STAGE1_PATH,
    PROMPT_STAGE2_PATH,
)

logger = logging.getLogger("deal_pool_scheduler")


class DealAIClient:
    """封装好价清洗 AI-1 / AI-2 的提示词热读取、单次调用并发闸门、JSON 解析与半批重试。"""

    def __init__(
        self,
        *,
        stage1_prompt_path: str = PROMPT_STAGE1_PATH,
        stage2_prompt_path: str = PROMPT_STAGE2_PATH,
        ai_timeout: float = DEFAULT_AI_TIMEOUT_SECONDS,
        ai_semaphore: Optional[threading.BoundedSemaphore] = None,
        allow_bisect: bool = True,
        ai_caller: Optional[Callable[..., Any]] = None,
    ):
        self.stage1_prompt_path = str(stage1_prompt_path or PROMPT_STAGE1_PATH)
        self.stage2_prompt_path = str(stage2_prompt_path or PROMPT_STAGE2_PATH)
        self.ai_timeout = max(10.0, float(ai_timeout))
        self.ai_semaphore = ai_semaphore
        self.allow_bisect = bool(allow_bisect)
        self.ai_caller = ai_caller

    def set_ai_semaphore(self, sem: Optional[threading.BoundedSemaphore]) -> None:
        self.ai_semaphore = sem

    @staticmethod
    def load_prompt_text(path: str) -> str:
        try:
            return load_prompt_cached(path).strip()
        except Exception as e:
            logger.error("读取 AI 提示词文件 [%s] 失败: %s", path, e)
            return ""

    def get_stage1_prompt(self) -> str:
        return self.load_prompt_text(self.stage1_prompt_path)

    def get_stage2_prompt(self) -> str:
        return self.load_prompt_text(self.stage2_prompt_path)

    def _invoke_model(self, full_prompt: str) -> Any:
        caller = self.ai_caller or generate_content
        kwargs = {
            "prompt": full_prompt,
            "preset_model_group": "low",
            "max_retries_per_model": 1,
            "timeout": self.ai_timeout,
            "stop_on_timeout": True,
            "slot_acquire_timeout": 5.0,
        }
        if self.ai_semaphore is not None:
            with self.ai_semaphore:
                return caller(**kwargs)
        return caller(**kwargs)

    def call_json_array(
        self,
        prompt_template: str,
        batch_payload: List[Dict[str, Any]],
        stage_label: str,
        *,
        _allow_bisect: bool = True,
    ) -> List[Dict[str, Any]]:
        """调用大模型并解析为字典列表；多条超时或截断空返回时按设置对半切分重试一次。"""
        if not batch_payload or not prompt_template:
            return []
        payload_json = json.dumps(batch_payload, ensure_ascii=False, indent=2)
        if stage_label == "AI-Stage1":
            full_prompt = f"{prompt_template}\n<deal_stage1_input>\n{payload_json}\n</deal_stage1_input>"
        else:
            full_prompt = (
                f"{prompt_template}\n\n"
                f"==================== 本批次待处理输入 JSON 数据 ====================\n"
                f"{payload_json}"
            )

        out_items: List[Dict[str, Any]] = []
        try:
            resp = self._invoke_model(full_prompt)
            resp_text = ""
            if isinstance(resp, dict):
                if resp.get("status") != "✅ 成功":
                    detail = "；".join(str(err) for err in (resp.get("error_history") or []))
                    if resp.get("timed_out"):
                        logger.debug("[%s] 批次达到 AI 超时阈值 (%.0fs)，留待下一轮或切分重试", stage_label, self.ai_timeout)
                    else:
                        raise RuntimeError(detail or str(resp.get("content") or "模型调用失败"))
                else:
                    resp_text = str(resp.get("content") or "")
            elif resp:
                resp_text = str(resp)

            cleaned_text = resp_text.strip()
            if cleaned_text:
                parsed = parse_llm_json(cleaned_text)
                if isinstance(parsed, list):
                    out_items = [item for item in parsed if isinstance(item, dict)]
                elif isinstance(parsed, dict):
                    out_items = [parsed]
        except Exception as e:
            err_brief = str(e).splitlines()[0][:200]
            logger.debug("[%s] 调用模型组解析 JSON 偶发异常 (留待重试): %s", stage_label, err_brief)

        if not out_items and _allow_bisect and self.allow_bisect and len(batch_payload) >= 3:
            half = len(batch_payload) // 2
            left_out = self.call_json_array(prompt_template, batch_payload[:half], stage_label, _allow_bisect=False)
            right_out = self.call_json_array(prompt_template, batch_payload[half:], stage_label, _allow_bisect=False)
            return left_out + right_out
        return out_items
