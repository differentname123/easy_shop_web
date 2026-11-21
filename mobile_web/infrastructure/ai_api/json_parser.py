# -*- coding: utf-8 -*-
"""统一 LLM 输出 JSON 解析器（SSOT）：剥离思考链与围栏，String-Aware 容错提取。"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

_THINK_BLOCK_RE = re.compile(r"<(?:think|thought)\b[^>]*>[\s\S]*?</(?:think|thought)>", flags=re.IGNORECASE)
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", flags=re.IGNORECASE)
_STRING_OR_COMMENT_RE = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|(/\*[\s\S]*?\*/|//[^\r\n]*)')
_STRING_OR_TRAILING_COMMA_RE = re.compile(r'("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')|(,\s*(?=[\]}]))')


def _bounded_preview(text: str, limit: int = 500) -> str:
    return (text[:limit] + "...") if len(text) > limit else text


def _strip_comments_and_trailing_commas(text: str) -> str:
    def _keep_string(match: re.Match[str]) -> str:
        return match.group(1) if match.group(1) else ""

    cleaned = _STRING_OR_COMMENT_RE.sub(_keep_string, text)
    return _STRING_OR_TRAILING_COMMA_RE.sub(_keep_string, cleaned).strip()


def _try_strict_json(candidate: str) -> Any:
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, (dict, list)):
            return parsed
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(candidate):
        if ch not in "{[":
            continue
        try:
            obj, _ = decoder.raw_decode(candidate, idx)
            if isinstance(obj, (dict, list)):
                return obj
        except Exception:
            continue
    return None


def _try_tolerant_parse(candidate: str) -> Any:
    first_bracket = candidate.find("[")
    first_curly = candidate.find("{")
    if first_bracket == -1 and first_curly == -1:
        return None
    if first_bracket == -1:
        start_pos = first_curly
    elif first_curly == -1:
        start_pos = first_bracket
    else:
        start_pos = min(first_bracket, first_curly)
    end_pos = max(candidate.rfind("]"), candidate.rfind("}"))
    if end_pos <= start_pos:
        return None
    sliced = candidate[start_pos : end_pos + 1]
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(sliced)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass
    cleaned = _strip_comments_and_trailing_commas(sliced)
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(cleaned)
            if isinstance(parsed, (dict, list)):
                return parsed
        except Exception:
            pass
    return None


def parse_llm_json(raw_text: str) -> Any:
    """统一提取并解析大模型返回的 JSON 对象或数组。"""
    if not isinstance(raw_text, str):
        raise TypeError(f"输入必须是字符串，但收到了 {type(raw_text).__name__}。")
    if not raw_text.strip():
        raise ValueError("模型返回内容为空")

    cleaned = _THINK_BLOCK_RE.sub("", raw_text).strip()
    if not cleaned:
        raise ValueError("模型返回内容为空")

    candidates = [m.group(1).strip() for m in _FENCED_JSON_RE.finditer(cleaned) if m.group(1).strip()]
    candidates.append(cleaned)

    for cand in candidates:
        strict_res = _try_strict_json(cand)
        if strict_res is not None:
            return strict_res

    for cand in candidates:
        tolerant_res = _try_tolerant_parse(cand)
        if tolerant_res is not None:
            return tolerant_res

    raise ValueError(f"无法从模型输出中解析合法 JSON 数据 | 输入内容预览: '{_bounded_preview(cleaned)}'")


extract_llm_json = parse_llm_json
string_to_object = parse_llm_json

__all__ = ["parse_llm_json", "extract_llm_json", "string_to_object"]
