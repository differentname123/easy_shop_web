# -*- coding: utf-8 -*-
"""AI 网关单元测试：统一 JSON 解析、懒加载边界、单 Key 模型熔断、云端分层模型与分片统计。"""
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_parse_llm_json_extracts_think_fences_comments_urls_and_literals():
    from infrastructure.ai_api.json_parser import extract_llm_json, parse_llm_json, string_to_object

    assert extract_llm_json is parse_llm_json
    assert string_to_object is parse_llm_json

    raw = """
    <think>先分析 {"fake": 1} 再输出</think>
    以下是清洗结果：
    ```json
    {
      // 单行注释不能误伤字符串内 URL
      "url": "https://u.jd.com/abc//test",
      "price": 19.9,
      /* 块级注释 */
      "tags": ["好价", "包邮",],
    }
    ```
    """
    parsed = parse_llm_json(raw)
    assert parsed == {
        "url": "https://u.jd.com/abc//test",
        "price": 19.9,
        "tags": ["好价", "包邮"],
    }

    py_literal = "前置文本 [{'a': 1, 'b': 'https://a.com/x',}] 尾缀"
    assert parse_llm_json(py_literal) == [{"a": 1, "b": "https://a.com/x"}]

    with pytest.raises(TypeError):
        parse_llm_json(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="为空"):
        parse_llm_json("   <thought>only thinking</thought>   ")
    with pytest.raises(ValueError):
        parse_llm_json("完全没有结构化对象的纯文本")


def test_ai_api_exports_remain_clean_without_antigravity_client(monkeypatch):
    for mod_name in list(sys.modules):
        if mod_name == "infrastructure.ai_api" or mod_name.startswith("infrastructure.ai_api."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)

    import infrastructure.ai_api as ai_api
    import infrastructure.ai_api.model_api as model_api

    assert "infrastructure.ai_api.antigravity_client" not in sys.modules
    assert not (Path(__file__).resolve().parents[1] / "infrastructure" / "ai_api" / "antigravity_client.py").exists()
    assert callable(ai_api.generate_content)
    assert callable(ai_api.parse_llm_json)
    assert callable(model_api.generate_content)
    assert not hasattr(ai_api, "AntigravityClient")


def _install_fake_openai(monkeypatch, model_api_mod, handler):
    created = []

    class FakeHttpError(Exception):
        def __init__(self, msg, status_code=None):
            super().__init__(msg)
            self.status_code = status_code

    class FakeCompletions:
        def create(self, **kwargs):
            created.append(dict(kwargs))
            outcome = handler(kwargs)
            if isinstance(outcome, Exception):
                raise outcome
            model_used, content = outcome
            resp_obj = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                model=model_used,
                usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            )
            return SimpleNamespace(status_code=200, parse=lambda: resp_obj)

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=FakeCompletions()))

        def close(self):
            pass

    monkeypatch.setattr(model_api_mod, "OpenAI", FakeClient)
    return created, FakeHttpError


def test_single_key_429_triggers_model_cooldown_and_skips_in_subsequent_calls(monkeypatch, tmp_path):
    import infrastructure.ai_api.model_api as model_api

    model_api.reset_ai_cooldowns()
    cfg_path = tmp_path / "ai_cfg.json"
    cfg_path.write_text(
        json.dumps({
            "local_api_url": "http://127.0.0.1:8317/v1",
            "local_api_key": "sk-local-single-key",
            "ai_models": ["bad-rate-limited-model", "healthy-fallback-model"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("AI_RUNTIME_MODE", "local")
    monkeypatch.setattr(model_api.time, "sleep", lambda _s: None)

    calls, FakeHttpError = _install_fake_openai(
        monkeypatch,
        model_api,
        lambda kw: (
            FakeHttpError("429 quota exceeded reset_seconds: 180", status_code=429)
            if kw["model"] == "bad-rate-limited-model"
            else (kw["model"], '{"ok": true}')
        ),
    )

    res1 = model_api.generate_content("hello 1", max_retries_per_model=3, preset_model_group="low")
    assert res1["status"] == "✅ 成功"
    assert res1["metrics"]["model_used"] == "healthy-fallback-model"
    # 单 Key 429 触发模型级熔断，不浪费同模型第 2/3 次重试
    assert [c["model"] for c in calls] == ["bad-rate-limited-model", "healthy-fallback-model"]

    calls.clear()
    res2 = model_api.generate_content("hello 2", max_retries_per_model=3, preset_model_group="low")
    assert res2["status"] == "✅ 成功"
    assert res2["metrics"]["model_used"] == "healthy-fallback-model"
    # 冷却期内直接选中健康模型，0 次浪费调用
    assert [c["model"] for c in calls] == ["healthy-fallback-model"]


def test_consecutive_failures_trigger_model_cooldown(monkeypatch, tmp_path):
    import infrastructure.ai_api.model_api as model_api

    model_api.reset_ai_cooldowns()
    cfg_path = tmp_path / "ai_cfg_consec.json"
    cfg_path.write_text(
        json.dumps({
            "local_api_url": "http://127.0.0.1:8317/v1",
            "local_api_key": "sk-local-key",
            "ai_models": ["flaky-model", "backup-model"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("AI_RUNTIME_MODE", "local")
    monkeypatch.setattr(model_api.time, "sleep", lambda _s: None)

    calls, FakeHttpError = _install_fake_openai(
        monkeypatch,
        model_api,
        lambda kw: (
            FakeHttpError("500 internal error", status_code=500)
            if kw["model"] == "flaky-model"
            else (kw["model"], "ok")
        ),
    )

    res1 = model_api.generate_content("test", max_retries_per_model=3)
    assert res1["status"] == "✅ 成功"
    # 连续失败 3 次触发熔断
    assert [c["model"] for c in calls].count("flaky-model") == 3

    calls.clear()
    res2 = model_api.generate_content("test 2", max_retries_per_model=3)
    assert res2["status"] == "✅ 成功"
    assert [c["model"] for c in calls] == ["backup-model"]


def test_linux_cloud_models_dict_tiers_and_full_timeout_and_accurate_503_log(monkeypatch, tmp_path):
    import infrastructure.ai_api.model_api as model_api

    model_api.reset_ai_cooldowns()
    secret_key = "AIzaSyTestSecretKeyForRedactionCheck123"
    cfg_path = tmp_path / "cloud_cfg.json"
    cfg_path.write_text(
        json.dumps({
            "local_api_key": "local-unused",
            "cloud_ai": {
                "api_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "api_keys": [secret_key],
                "models": {
                    "high": ["gemini-2.5-pro"],
                    "medium": ["gemini-2.5-flash", "gemini-2.5-flash-lite"],
                    "low": ["gemini-2.5-flash-lite"],
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("AI_RUNTIME_MODE", "cloud")
    monkeypatch.setattr(model_api.time, "sleep", lambda _s: None)

    calls, FakeHttpError = _install_fake_openai(
        monkeypatch,
        model_api,
        lambda kw: (
            FakeHttpError(f"503 high demand error with key {secret_key}", status_code=503)
            if kw["model"] == "gemini-2.5-flash"
            else (kw["model"], "cloud ok")
        ),
    )

    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    capture_handler = _ListHandler(level=logging.WARNING)
    capture_handler.setFormatter(model_api._RedactingFormatter())
    model_api.logger.addHandler(capture_handler)
    try:
        # 验证 503 日志语义正确且不触发本地槽位繁忙误报，且 timeout 不会被静默压成 25s
        res_med = model_api.generate_content("cloud prompt", preset_model_group="medium", timeout=180)
        assert res_med["status"] == "✅ 成功"
        assert res_med["metrics"]["runtime_mode"] == "linux_cloud"
        assert calls[0]["model"] == "gemini-2.5-flash"
        assert calls[0]["timeout"] == 180.0
        assert calls[1]["model"] == "gemini-2.5-flash-lite"

        log_text = "\n".join(records)
        assert "503" in log_text
        assert "本地槽位繁忙" not in log_text
        assert secret_key not in log_text
        assert secret_key not in " ".join(res_med["error_history"])

        # 验证 high / low 分层路由命中
        calls.clear()
        res_low = model_api.generate_content("low prompt", preset_model_group="low", timeout=90)
        assert res_low["status"] == "✅ 成功"
        assert calls[0]["model"] == "gemini-2.5-flash-lite"
    finally:
        model_api.logger.removeHandler(capture_handler)


def test_redact_does_not_re_stat_config_per_log_line(monkeypatch, tmp_path):
    import infrastructure.ai_api.model_api as model_api

    cfg_path = tmp_path / "stat_cfg.json"
    cfg_path.write_text(json.dumps({"local_api_key": "my-cache-secret-xyz"}), encoding="utf-8")
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg_path))
    model_api._CONFIG_FILE_CACHE["key"] = None
    model_api._CONFIG_FILE_CACHE["secrets"] = None

    assert model_api.get_config("local_api_key") == "my-cache-secret-xyz"
    assert model_api._redact("secret=my-cache-secret-xyz") == "secret=***"

    stat_calls = []
    orig_stat = Path.stat

    def counting_stat(self, *args, **kwargs):
        stat_calls.append(str(self))
        return orig_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)
    for _ in range(10):
        out = model_api._redact("key=my-cache-secret-xyz https://user:pw@example.com sk-abc12345")
        assert "my-cache-secret-xyz" not in out
        assert "user:pw@" not in out
        assert "sk-abc12345" not in out
    assert stat_calls == []


def test_usage_tracker_sharding_memory_mode_and_combined_aggregation(monkeypatch, tmp_path):
    from infrastructure.ai_api import usage_tracker
    import infrastructure.api_governance as api_gov

    usage_tracker.reset_usage_tracker()
    monkeypatch.setattr(usage_tracker, "_DEFAULT_RUNTIME_DIR", tmp_path)
    monkeypatch.delenv("MODEL_USAGE_JSON_PATH", raising=False)

    # 1. WEB_LOG_DIR='-' 下不写默认磁盘文件
    monkeypatch.setenv("WEB_LOG_DIR", "-")
    api_gov.set_process_role("web")
    usage_tracker.record_usage_events([
        {
            "model_name": "m-web",
            "status": "✅ 成功",
            "call_time_bj": "2026-11-21T10:00:00.000000+08:00",
            "finished_time_bj": "2026-11-21T10:00:01.000000+08:00",
            "duration_seconds": 1.0,
            "error": None,
            "http_status": 200,
            "response_model": "m-web",
            "trace_id": "t1",
        }
    ])
    assert list(tmp_path.glob("*.json")) == []
    combined_mem = usage_tracker.get_combined_model_usage()
    assert combined_mem["m-web"]["成功次数"] == 1

    # 2. 启用磁盘日志时按进程角色分片落盘，并能聚合多个进程与遗留文件
    usage_tracker.reset_usage_tracker()
    monkeypatch.setenv("WEB_LOG_DIR", str(tmp_path))
    api_gov.set_process_role("deal_pool")
    usage_tracker.record_usage_events([
        {
            "model_name": "shared-model",
            "status": "✅ 成功",
            "call_time_bj": "2026-11-21T10:01:00.000000+08:00",
            "finished_time_bj": "2026-11-21T10:01:02.000000+08:00",
            "duration_seconds": 2.0,
            "error": None,
            "http_status": 200,
            "response_model": "shared-model",
            "trace_id": "t2",
        }
    ])
    usage_tracker.flush_usage()
    shard_file = tmp_path / "model_usage_deal_pool.json"
    assert shard_file.is_file()

    # 写入另一进程分片
    (tmp_path / "model_usage_xianyu_discovery.json").write_text(
        json.dumps({
            "shared-model": {
                "总共调用次数": 2,
                "成功次数": 1,
                "失败次数": 1,
                "中断次数": 0,
                "准备失败次数": 0,
                "累计请求耗时秒": 4.0,
                "最近调用时间（北京时间）": "2026-11-21T10:05:00.000000+08:00",
                "最近完成时间（北京时间）": "2026-11-21T10:05:03.000000+08:00",
                "最近成功时间（北京时间）": "2026-11-21T10:05:03.000000+08:00",
                "最近报错信息": "503",
                "最近报错信息时间（北京时间）": "2026-11-21T10:04:00.000000+08:00",
                "status": "✅ 成功",
            }
        }),
        encoding="utf-8",
    )
    combined = usage_tracker.get_combined_model_usage(tmp_path)
    assert combined["shared-model"]["总共调用次数"] == 3
    assert combined["shared-model"]["成功次数"] == 2
    assert combined["shared-model"]["失败次数"] == 1
    assert combined["shared-model"]["累计请求耗时秒"] == 6.0
    assert combined["shared-model"]["平均请求耗时秒"] == 2.0
    assert combined["shared-model"]["成功率"] == round(2 / 3, 6)


def test_http_200_empty_choices_and_502_throttled_fast_switch_and_cool(monkeypatch, tmp_path):
    import infrastructure.ai_api.model_api as model_api

    model_api.reset_ai_cooldowns()
    cfg_path = tmp_path / "ai_cfg_fast_switch.json"
    cfg_path.write_text(
        json.dumps({
            "local_api_url": "http://127.0.0.1:8317/v1",
            "local_api_key": "sk-local-single-key",
            "ai_models": ["bad-empty-model", "healthy-fallback-model"],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("WEB_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("AI_RUNTIME_MODE", "local")
    monkeypatch.setattr(model_api.time, "sleep", lambda _s: None)

    created = []

    class FakeHttpError(Exception):
        def __init__(self, msg, status_code=None):
            super().__init__(msg)
            self.status_code = status_code

    def _run_with(handler):
        class FakeCompletions:
            def create(self, **kwargs):
                created.append(dict(kwargs))
                outcome = handler(kwargs)
                if isinstance(outcome, Exception):
                    raise outcome
                model_used, content = outcome
                choices = [] if content is None else [SimpleNamespace(message=SimpleNamespace(content=content))]
                resp_obj = SimpleNamespace(
                    choices=choices,
                    model=model_used,
                    usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
                )
                return SimpleNamespace(status_code=200, parse=lambda: resp_obj)

        class FakeClient:
            def __init__(self, **kwargs):
                self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=FakeCompletions()))

            def close(self):
                pass

        monkeypatch.setattr(model_api, "OpenAI", FakeClient)

    # 1. HTTP 200 empty choices fast-switches on attempt 1 and cools model
    _run_with(lambda kw: (kw["model"], None) if kw["model"] == "bad-empty-model" else (kw["model"], "ok"))
    res1 = model_api.generate_content("hello", max_retries_per_model=3, preset_model_group="low")
    assert res1["status"] == "✅ 成功"
    assert [c["model"] for c in created] == ["bad-empty-model", "healthy-fallback-model"]

    created.clear()
    res2 = model_api.generate_content("hello 2", max_retries_per_model=3, preset_model_group="low")
    assert res2["status"] == "✅ 成功"
    assert [c["model"] for c in created] == ["healthy-fallback-model"]

    # 2. HTTP 502 wrapping 429/throttled fast-switches on attempt 1 and cools model
    model_api.reset_ai_cooldowns()
    created.clear()
    _run_with(
        lambda kw: (
            FakeHttpError("HTTP 502: 上游429上传上限 throttled", status_code=502)
            if kw["model"] == "bad-empty-model"
            else (kw["model"], "ok")
        )
    )
    res3 = model_api.generate_content("hello 3", max_retries_per_model=3, preset_model_group="low")
    assert res3["status"] == "✅ 成功"
    assert [c["model"] for c in created] == ["bad-empty-model", "healthy-fallback-model"]

    created.clear()
    res4 = model_api.generate_content("hello 4", max_retries_per_model=3, preset_model_group="low")
    assert res4["status"] == "✅ 成功"
    assert [c["model"] for c in created] == ["healthy-fallback-model"]


def test_encode_image_for_llm_preserves_dimensions_and_edge_cases(tmp_path):
    import base64
    import io
    import random
    from PIL import Image, ImageChops, ImageDraw
    import infrastructure.ai_api.model_api as model_api

    def _noisy_png(p: Path, size, mode="RGB") -> bytes:
        base = Image.effect_noise(size, 16.0)
        grad = Image.linear_gradient("L").resize(size)
        r = ImageChops.blend(base, grad, 0.7)
        g = ImageChops.blend(base, grad.transpose(Image.FLIP_TOP_BOTTOM), 0.7)
        if mode == "RGBA":
            alpha = Image.new("L", size, 255)
            alpha.paste(0, (0, 0, size[0], 60))
            img = Image.merge("RGBA", (r, g, grad, alpha))
        else:
            img = Image.merge("RGB", (r, g, grad))
        img.save(p, format="PNG")
        return p.read_bytes()

    # Tall screenshot PNG -> smaller JPEG with exact (800, 2400) dimensions, disk untouched
    tall = tmp_path / "tall.png"
    tall_raw = _noisy_png(tall, (800, 2400), mode="RGB")
    assert len(tall_raw) > model_api._IMAGE_COMPRESS_MIN_BYTES
    enc_bytes, enc_mime = model_api._encode_image_for_llm(tall, "image/png")
    assert enc_mime == "image/jpeg"
    assert len(enc_bytes) < len(tall_raw)
    assert tall.read_bytes() == tall_raw
    with Image.open(io.BytesIO(enc_bytes)) as img:
        assert img.size == (800, 2400)

    blocks = model_api._build_content("inspect", [str(tall)])
    img_block = next(b for b in blocks if b.get("type") == "image_url")
    assert img_block["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(img_block["image_url"]["url"].split(",", 1)[1]) == enc_bytes

    # RGBA PNG -> RGB JPEG without errors
    rgba = tmp_path / "rgba.png"
    _noisy_png(rgba, (800, 1200), mode="RGBA")
    rgba_bytes, rgba_mime = model_api._encode_image_for_llm(rgba, "image/png")
    assert rgba_mime == "image/jpeg"
    with Image.open(io.BytesIO(rgba_bytes)) as img_rgba:
        assert img_rgba.size == (800, 1200)
        assert img_rgba.mode == "RGB"
        px = img_rgba.getpixel((10, 10))
        assert all(ch >= 245 for ch in px)

    # Animated GIF (> 8192 bytes) -> untouched image/gif
    gif = tmp_path / "anim.gif"
    rng = random.Random(9)
    frames = []
    for f_idx in range(2):
        fr = Image.new("RGB", (256, 256), (255, 255, 255))
        d = ImageDraw.Draw(fr)
        for y in range(0, 256, 4):
            d.line([(0, y), (255, (y + f_idx * 20) % 256)], fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
        frames.append(fr)
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    gif_raw = gif.read_bytes()
    assert len(gif_raw) > model_api._IMAGE_COMPRESS_MIN_BYTES
    gif_out, gif_mime = model_api._encode_image_for_llm(gif, "image/gif")
    assert gif_mime == "image/gif"
    assert gif_out == gif_raw

    # Tiny PNG (<= 8192 bytes) -> untouched image/png
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(tiny, format="PNG")
    tiny_raw = tiny.read_bytes()
    assert len(tiny_raw) <= model_api._IMAGE_COMPRESS_MIN_BYTES
    tiny_out, tiny_mime = model_api._encode_image_for_llm(tiny, "image/png")
    assert tiny_mime == "image/png"
    assert tiny_out == tiny_raw
