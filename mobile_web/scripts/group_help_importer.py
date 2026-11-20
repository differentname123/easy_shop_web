# -*- coding: utf-8 -*-
"""
拼多多多人团互助——外部高质量源定时采集守护脚本（完全解耦外挂）。

定位：
- 独立于主站 FastAPI 进程运行，按固定间隔轮询已验证的高可用多人团数据源：
  1) 闪助力 (https://acdog.cn/api/pin/deals)
  2) 秒助力 (http://bz.5138zhuan.com/zd/api/list.php)
- 所有外部条目统一经过 `models.group_help.parse_pdd_group_share_fields` 唯一校验真源清洗：
  严格拦截外站域名、违规引流广告与异常价格，使用较短外部有效期，并同步上游满员/失效状态。
- 本地团友已标记满员或过期的记录绝不被上游复活；若不想使用外部采集，直接停止本脚本即可，零架构入侵。

用法：
  python mobile_web/scripts/group_help_importer.py --once
  python mobile_web/scripts/group_help_importer.py --interval 90 --pages 2
"""
import argparse
import logging
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import requests

CURRENT_DIR = Path(__file__).resolve().parent
MOBILE_WEB_DIR = CURRENT_DIR.parent
for _p in (str(MOBILE_WEB_DIR), str(CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from models.group_help import (
    DEFAULT_GROUP_TTL_SECONDS,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    GROUP_STATUS_EXPIRED,
    GROUP_STATUS_FULL,
    extract_pdd_share_token,
    extract_smart_fields_from_pdd_share_text,
    parse_pdd_group_share_fields,
)
from repositories.group_help_repo import GroupHelpRepository

_CST_TZ = timezone(timedelta(hours=8))
_DEFAULT_BRIDGE_DIR = MOBILE_WEB_DIR / "runtime" / "group_help_bridge"
_DEFAULT_EXTERNAL_TTL_SECONDS = 6 * 3600  # 外部流入默认 6 小时有效，避免陈旧滞留
_DEFAULT_SYNC_INTERVAL_SECONDS = DEFAULT_SYNC_INTERVAL_SECONDS  # 外部同步默认 10 分钟间隔 (600s)
_CORE_CODE_RE = re.compile(r"^[A-Za-z0-9]{6,28}$")

ACDOG_API_URL = "https://acdog.cn/api/pin/deals"
MIAOZHULI_API_URL = "http://bz.5138zhuan.com/zd/api/list.php"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _setup_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("group_help_importer")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        file_handler = RotatingFileHandler(
            log_dir / "importer.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(fmt)
        log.addHandler(file_handler)
        log.addHandler(stream_handler)
    log.propagate = False
    return log


def _parse_epoch_seconds(val: Any, default_ts: float) -> float:
    if val is None or val == "" or isinstance(val, bool):
        return default_ts
    if isinstance(val, (int, float)):
        num = float(val)
        return (num / 1000.0) if num > 1e12 else num
    text = str(val).strip()
    if not text:
        return default_ts
    try:
        num = float(text)
        return (num / 1000.0) if num > 1e12 else num
    except ValueError:
        pass
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_CST_TZ)
        return dt.timestamp()
    except ValueError:
        return default_ts


def _extract_list_items(payload: Any) -> List[Dict[str, Any]]:
    """兼容常见列表包装结构，仅提取 dict 条目。"""
    candidates: Any = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            candidates = payload.get("items")
        elif isinstance(payload.get("list"), list):
            candidates = payload.get("list")
        elif isinstance(payload.get("data"), list):
            candidates = payload.get("data")
        elif isinstance(payload.get("data"), dict):
            nested = payload["data"]
            for key in ("items", "list", "deals", "rows"):
                if isinstance(nested.get(key), list):
                    candidates = nested[key]
                    break
    return [it for it in candidates if isinstance(it, dict)]


def _clean_external_goods_name(title_candidate: str, raw_text: str) -> str:
    """清洗外部截断标题，复用 models.group_help.extract_smart_fields_from_pdd_share_text 唯一真源。"""
    title = str(title_candidate or "").strip()
    raw = str(raw_text or "").strip()
    smart = extract_smart_fields_from_pdd_share_text(raw)
    smart_title = str(smart.get("goods_name") or "").strip()
    if not title:
        return smart_title
    if smart_title and ("..." in title or "…" in title) and smart_title != title:
        return smart_title
    if "（" in smart_title and "）" in smart_title and "（" not in title:
        suffix_part = smart_title[smart_title.rfind("（") :]
        return f"{title}{suffix_part}"
    return title


class GroupHelpImporter:
    """外部多人团轮询与入库同步器。"""

    def __init__(
        self,
        repo: Optional[GroupHelpRepository] = None,
        *,
        http_get: Optional[Callable[..., Any]] = None,
        external_ttl_seconds: int = _DEFAULT_EXTERNAL_TTL_SECONDS,
        pages: int = 0,
        dry_run: bool = False,
        logger: Optional[logging.Logger] = None,
    ):
        self.repo = repo if repo is not None else GroupHelpRepository()
        self._http_get = http_get
        self.external_ttl_seconds = max(
            1800,
            min(int(external_ttl_seconds), DEFAULT_GROUP_TTL_SECONDS),
        )
        self.pages = max(0, int(pages))  # 0 表示全量获取，>0 表示抓取指定页数
        self.dry_run = bool(dry_run)
        self.log = logger or _setup_logger(_DEFAULT_BRIDGE_DIR)

    def _fetch_json(self, url: str, params: Dict[str, Any]) -> Any:
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if self._http_get is not None:
            resp = self._http_get(url, params=params, headers=headers, timeout=12)
        else:
            resp = requests.get(url, params=params, headers=headers, timeout=12)
        if getattr(resp, "status_code", 200) != 200:
            raise RuntimeError(f"HTTP {getattr(resp, 'status_code', '?')} from {url}")
        return resp.json()

    def _upsert_external_candidate(
        self,
        *,
        source: str,
        raw_share_text: str,
        goods_name: str,
        group_price: Any,
        total_people: Any,
        remain_people: Any,
        publisher_nick: str,
        created_at: float,
        upstream_expires_at: Optional[float],
        now: float,
        stats: Dict[str, int],
    ) -> Optional[str]:
        parsed = parse_pdd_group_share_fields(
            share_text=raw_share_text,
            goods_name=goods_name,
            group_price=group_price,
            total_people=total_people,
            remain_people=remain_people,
            publisher_nick=publisher_nick,
        )
        gid = str(parsed.get("group_order_id") or "").strip()
        if parsed["needs_url_resolve"] and not gid:
            # 外部采集列表不额外发起逐条短链反查请求，避免给目标或拼多多带来不必要请求压力
            stats["skipped_unresolved"] += 1
            return None

        # 核心铁律：网页用户反馈下架了的团，就算外部源还在，也绝不要恢复
        if gid:
            existing = self.repo.get_by_group_order_id(gid)
            if existing is not None and bool(existing.get("user_takedown")):
                stats["skipped_user_takedown"] = stats.get("skipped_user_takedown", 0) + 1
                return gid

        local_cap = now + self.external_ttl_seconds
        pdd_cap = created_at + DEFAULT_GROUP_TTL_SECONDS
        expires_at = min(local_cap, pdd_cap)
        if upstream_expires_at is not None and upstream_expires_at > 0:
            expires_at = min(expires_at, upstream_expires_at)

        if expires_at <= now:
            stats["skipped_expired"] += 1
            return None

        if self.dry_run:
            stats["dry_run_ok"] += 1
            return gid

        doc_input: Dict[str, Any] = {
            **parsed,
            "source": source,
            "poster_path": "",
            "created_at": min(created_at, now),
            "expires_at": expires_at,
        }
        _, is_new, _ = self.repo.upsert_parsed_group(
            doc_input,
            ttl_seconds=DEFAULT_GROUP_TTL_SECONDS,
            now=now,
            preserve_local_takedown=True,
            check_similar=False,
        )
        if is_new:
            stats["upserted"] += 1
        else:
            stats["unchanged"] += 1
        return gid

    def _sync_acdog(self, now: float) -> Tuple[Dict[str, int], Set[str]]:
        stats = {
            "fetched": 0,
            "upserted": 0,
            "unchanged": 0,
            "synced_inactive": 0,
            "skipped_invalid": 0,
            "skipped_expired": 0,
            "skipped_unresolved": 0,
            "skipped_user_takedown": 0,
            "dry_run_ok": 0,
        }
        active_keys: Set[str] = set()
        page = 1
        max_pages = self.pages if self.pages > 0 else 50
        while page <= max_pages:
            payload = self._fetch_json(ACDOG_API_URL, {"page": page, "size": 50})
            items = _extract_list_items(payload)
            if not items:
                break
            stats["fetched"] += len(items)

            page_all_expired = True
            for item in items:
                platform = str(item.get("platform") or "").strip().lower()
                if platform and platform not in ("pdd", "pinduoduo"):
                    stats["skipped_invalid"] += 1
                    continue

                raw_text = str(item.get("rawText") or "").strip()
                core_code = str(item.get("coreCode") or "").strip()
                if core_code and _CORE_CODE_RE.match(core_code) and not extract_pdd_share_token(raw_text):
                    raw_text = f"{raw_text} ⇥{core_code}⇤".strip()

                token_code = extract_pdd_share_token(raw_text) or (
                    core_code if _CORE_CODE_RE.match(core_code) else ""
                )
                gid_guess = f"tok_{token_code}" if token_code else ""

                status_str = str(item.get("status") or "").strip().lower()
                spots_left = item.get("spotsLeft")
                marked_full = int(item.get("markedFullCount") or 0)
                marked_invalid = int(item.get("markedInvalidCount") or 0)
                created_at = _parse_epoch_seconds(item.get("createdAt"), now)
                upstream_expires = _parse_epoch_seconds(item.get("expiresAt"), 0.0) or None

                if (now - created_at) < DEFAULT_GROUP_TTL_SECONDS:
                    page_all_expired = False

                is_full = (
                    status_str in ("full", "completed", "finished")
                    or (spots_left is not None and not isinstance(spots_left, bool) and int(spots_left) <= 0)
                    or marked_full >= 2
                )
                is_expired = (
                    status_str in ("expired", "invalid", "deleted", "closed")
                    or marked_invalid >= 2
                    or (upstream_expires is not None and upstream_expires <= now)
                )
                if is_full or is_expired:
                    if not self.dry_run and gid_guess:
                        target_status = GROUP_STATUS_FULL if is_full else GROUP_STATUS_EXPIRED
                        if self.repo.sync_external_status(
                            gid_guess, source="acdog", new_status=target_status, now=now
                        ):
                            stats["synced_inactive"] += 1
                    stats["skipped_expired"] += 1
                    continue

                cleaned_title = _clean_external_goods_name(str(item.get("titleDisplay") or ""), raw_text)
                try:
                    upserted_gid = self._upsert_external_candidate(
                        source="acdog",
                        raw_share_text=raw_text,
                        goods_name=cleaned_title,
                        group_price=item.get("price"),
                        total_people=item.get("groupSize") or 0,
                        remain_people=spots_left if spots_left is not None else 1,
                        publisher_nick=str(item.get("publisherDisplayName") or ""),
                        created_at=created_at,
                        upstream_expires_at=upstream_expires,
                        now=now,
                        stats=stats,
                    )
                    if upserted_gid:
                        active_keys.add(upserted_gid)
                    if gid_guess:
                        active_keys.add(gid_guess)
                    if token_code:
                        active_keys.add(token_code)
                except ValueError:
                    stats["skipped_invalid"] += 1

            has_more = bool(payload.get("data", {}).get("hasMore", True)) if isinstance(payload, dict) else True
            if not has_more or page_all_expired:
                break
            page += 1

        return stats, active_keys

    def _sync_miaozhuli(self, now: float) -> Tuple[Dict[str, int], Set[str]]:
        stats = {
            "fetched": 0,
            "upserted": 0,
            "unchanged": 0,
            "synced_inactive": 0,
            "skipped_invalid": 0,
            "skipped_expired": 0,
            "skipped_unresolved": 0,
            "skipped_user_takedown": 0,
            "dry_run_ok": 0,
        }
        active_keys: Set[str] = set()
        page = 1
        max_pages = self.pages if self.pages > 0 else 50
        while page <= max_pages:
            payload = self._fetch_json(MIAOZHULI_API_URL, {"tab": "all", "page": page})
            items = _extract_list_items(payload)
            if not items:
                break
            stats["fetched"] += len(items)

            for item in items:
                share_text = str(item.get("share_text") or "").strip()
                url_val = str(item.get("url") or "").strip()
                if url_val and url_val not in share_text:
                    raw_text = f"{share_text} {url_val}".strip()
                else:
                    raw_text = share_text or url_val

                token_code = extract_pdd_share_token(raw_text)
                gid_guess = f"tok_{token_code}" if token_code else ""

                status_str = str(item.get("status") or "").strip().lower()
                marks = item.get("marks") if isinstance(item.get("marks"), dict) else {}
                full_marks = int(marks.get("full") or 0)
                exp_marks = int(marks.get("expire") or 0)
                time_left_raw = item.get("time_left")
                time_left_sec: Optional[float] = None
                if time_left_raw is not None and not isinstance(time_left_raw, bool):
                    try:
                        time_left_sec = float(time_left_raw)
                    except (TypeError, ValueError):
                        time_left_sec = None

                created_at = _parse_epoch_seconds(item.get("created_at"), now)
                upstream_expires = (
                    (now + time_left_sec)
                    if time_left_sec is not None
                    else (created_at + self.external_ttl_seconds)
                )

                is_full = status_str in ("full", "completed", "finished") or full_marks >= 1
                is_expired = (
                    status_str in ("expired", "invalid", "deleted", "closed")
                    or exp_marks >= 1
                    or (time_left_sec is not None and time_left_sec <= 0)
                )
                if is_full or is_expired:
                    if not self.dry_run and gid_guess:
                        target_status = GROUP_STATUS_FULL if is_full else GROUP_STATUS_EXPIRED
                        if self.repo.sync_external_status(
                            gid_guess, source="miaozhuli", new_status=target_status, now=now
                        ):
                            stats["synced_inactive"] += 1
                    stats["skipped_expired"] += 1
                    continue

                cleaned_title = _clean_external_goods_name(str(item.get("title") or ""), raw_text)
                try:
                    upserted_gid = self._upsert_external_candidate(
                        source="miaozhuli",
                        raw_share_text=raw_text,
                        goods_name=cleaned_title,
                        group_price=item.get("price"),
                        total_people=item.get("team_size") or 0,
                        remain_people=1,
                        publisher_nick=str(item.get("nick") or ""),
                        created_at=created_at,
                        upstream_expires_at=upstream_expires,
                        now=now,
                        stats=stats,
                    )
                    if upserted_gid:
                        active_keys.add(upserted_gid)
                    if gid_guess:
                        active_keys.add(gid_guess)
                    if token_code:
                        active_keys.add(token_code)
                except ValueError:
                    stats["skipped_invalid"] += 1

            has_more = bool(payload.get("has_more", True)) if isinstance(payload, dict) else True
            if not has_more:
                break
            page += 1

        return stats, active_keys

    def run_once(self, now: Optional[float] = None) -> Dict[str, Any]:
        loop_t0 = time.perf_counter()
        current_ts = float(now if now is not None else time.time())
        summary: Dict[str, Any] = {
            "timestamp": int(current_ts),
            "sources": {},
            "offline_synced": 0,
        }

        if not self.dry_run:
            try:
                self.repo.expire_outdated_groups(now=current_ts)
            except Exception as exc:
                self.log.warning("[IMPORTER-WARN] 清理站内过期拼团异常: %s", exc)

        seen_active_keys: Set[str] = set()
        has_source_error = False

        for source_name, handler in (
            ("acdog", self._sync_acdog),
            ("miaozhuli", self._sync_miaozhuli),
        ):
            try:
                st, source_keys = handler(current_ts)
                summary["sources"][source_name] = st
                seen_active_keys.update(source_keys)
            except Exception as exc:
                has_source_error = True
                self.log.warning("[IMPORTER-WARN] 采集源 [%s] 本轮失败 (跳过不中断): %s", source_name, exc)
                summary["sources"][source_name] = {"error": str(exc)}

        # 宁缺毋滥与防误杀铁律：两站皆无才自动下架；若任一外部源网络抖动/报错，不贸然下架存量团
        if not self.dry_run and not has_source_error:
            try:
                offline_count = self.repo.mark_missing_external_groups_offline(
                    seen_active_keys,
                    now=current_ts,
                )
                summary["offline_synced"] = offline_count
            except Exception as exc:
                self.log.warning("[IMPORTER-WARN] 两站皆无自动下架同步异常: %s", exc)

        total_time_ms = int((time.perf_counter() - loop_t0) * 1000)
        acdog_st = summary["sources"].get("acdog", {})
        miao_st = summary["sources"].get("miaozhuli", {})

        def _fmt_src(st: Any) -> str:
            if not isinstance(st, dict) or "error" in st:
                err_text = st.get("error", "未知错误") if isinstance(st, dict) else str(st)
                return f"异常 ({err_text})"
            return (
                f"抓取 {st.get('fetched', 0):3d} | 新增 {st.get('upserted', 0):2d} | "
                f"刷新 {st.get('unchanged', 0):2d} | 满员/下架同步 {st.get('synced_inactive', 0):2d} | "
                f"用户下架跳过 {st.get('skipped_user_takedown', 0):2d} | "
                f"过滤垃圾/过期 {st.get('skipped_invalid', 0) + st.get('skipped_expired', 0):2d}"
            )

        total_fetched = (acdog_st.get("fetched", 0) if isinstance(acdog_st, dict) else 0) + (
            miao_st.get("fetched", 0) if isinstance(miao_st, dict) else 0
        )
        total_upserted = (acdog_st.get("upserted", 0) if isinstance(acdog_st, dict) else 0) + (
            miao_st.get("upserted", 0) if isinstance(miao_st, dict) else 0
        )
        total_unchanged = (acdog_st.get("unchanged", 0) if isinstance(acdog_st, dict) else 0) + (
            miao_st.get("unchanged", 0) if isinstance(miao_st, dict) else 0
        )
        total_synced_inactive = (acdog_st.get("synced_inactive", 0) if isinstance(acdog_st, dict) else 0) + (
            miao_st.get("synced_inactive", 0) if isinstance(miao_st, dict) else 0
        )
        total_user_takedown_skipped = (acdog_st.get("skipped_user_takedown", 0) if isinstance(acdog_st, dict) else 0) + (
            miao_st.get("skipped_user_takedown", 0) if isinstance(miao_st, dict) else 0
        )
        offline_count = int(summary.get("offline_synced") or 0)

        try:
            active_in_hall = self.repo.col.count_documents(
                {
                    "parse_status": "parsed",
                    "group_status": "active",
                    "expires_at": {"$gt": current_ts},
                    "remain_people": {"$gt": 0},
                }
            )
        except Exception:
            active_in_hall = -1

        hall_str = f"{active_in_hall} 款" if active_in_hall >= 0 else "未知"
        page_mode_str = f"{self.pages}页" if self.pages > 0 else "全量(直至无更多/超时)"

        self.log.info(
            "\n"
            "========================================================================================\n"
            "[PDD-IMPORTER] 采集轮询完成 | 总耗时: %sms | 翻页模式: %s\n"
            "  ├─ 闪助力 [acdog]   : %s\n"
            "  ├─ 秒助力 [miaozhuli]: %s\n"
            "  └─ 全局汇总: 扫描 %d 款 | 实际新增 %d 款 | 状态刷新 %d 款 | 同步下架 %d 款 | 两站皆无自动下架 %d 款 | 用户下架保持拦截 %d 款 | 大厅在售: %s\n"
            "========================================================================================",
            total_time_ms,
            page_mode_str,
            _fmt_src(acdog_st),
            _fmt_src(miao_st),
            total_fetched,
            total_upserted,
            total_unchanged,
            total_synced_inactive,
            offline_count,
            total_user_takedown_skipped,
            hall_str,
        )
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="拼多多多人团外部高质量源定时采集守护脚本")
    parser.add_argument("--once", action="store_true", help="仅执行一轮采集后退出")
    parser.add_argument("--interval", type=int, default=_DEFAULT_SYNC_INTERVAL_SECONDS, help="后台轮询间隔秒数（默认 600，即10分钟）")
    parser.add_argument("--pages", type=int, default=0, help="每次每个源抓取前 N 页（默认 0 表示全量获取所有有效团）")
    parser.add_argument("--ttl-hours", type=float, default=6.0, help="外部团最多在站内保留小时数（默认 6）")
    parser.add_argument("--dry-run", action="store_true", help="仅抓取校验并打印统计，不写入数据库")
    args = parser.parse_args()

    log = _setup_logger(_DEFAULT_BRIDGE_DIR)
    importer = GroupHelpImporter(
        external_ttl_seconds=int(max(0.5, args.ttl_hours) * 3600),
        pages=args.pages,
        dry_run=args.dry_run,
        logger=log,
    )

    if args.once:
        importer.run_once()
        return

    stop_flag = {"stop": False}

    def _handle_stop(signum, _frame):
        log.info("收到退出信号 (%s)，将在当前轮结束后安全退出...", signum)
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_stop)

    interval = max(15, int(args.interval))
    log.info("外部多人团采集脚本已启动，轮询间隔=%ss，pages=%s (0=全量获取)", interval, importer.pages)
    while not stop_flag["stop"]:
        importer.run_once()
        slept = 0
        while slept < interval and not stop_flag["stop"]:
            time.sleep(min(2, interval - slept))
            slept += 2


if __name__ == "__main__":
    main()
