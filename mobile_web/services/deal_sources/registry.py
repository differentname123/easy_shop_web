# -*- coding: utf-8 -*-
"""
好价线报数据源插件注册表（唯一真源）。
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from .baicaio import BaicaioSource
from .base import BaseDealSource
from .empire_deal import KieesSource, NiuzaSource
from .guangdiu import GuangdiuSource
from .haodanku import HaodankuSource
from .ithome_lapin import IthomeLapinSource
from .m_zzzdm import MZzzdmSource
from .tehuicaicai import TehuicaicaiSource
from .telegram import TelegramChannelSource
from .tuan0818 import Tuan0818Source
from .xuandan import XuandanSource
from .zdzdm import ZdzdmSource

_SOURCE_REGISTRY: Dict[str, BaseDealSource] = {}


def register_source(source: BaseDealSource, *, aliases: Iterable[str] = ()) -> BaseDealSource:
    keys = [source.resolved_source_key, *(aliases or ())]
    for key in keys:
        k = str(key or "").strip()
        if k:
            _SOURCE_REGISTRY[k] = source
    return source


def _init_builtin_sources() -> None:
    if _SOURCE_REGISTRY:
        return
    register_source(TehuicaicaiSource())
    register_source(GuangdiuSource())
    register_source(HaodankuSource())
    register_source(XuandanSource())
    register_source(Tuan0818Source())
    register_source(MZzzdmSource(), aliases=("zzzdm",))
    register_source(TelegramChannelSource())
    register_source(IthomeLapinSource())
    register_source(KieesSource())
    register_source(NiuzaSource())
    register_source(ZdzdmSource())
    register_source(BaicaioSource())


def get_source(source_key: str) -> Optional[BaseDealSource]:
    _init_builtin_sources()
    return _SOURCE_REGISTRY.get(str(source_key or "").strip())


def list_sources() -> Dict[str, BaseDealSource]:
    _init_builtin_sources()
    return dict(_SOURCE_REGISTRY)


def get_source_troubleshooting(site_key: str) -> Dict[str, str]:
    _init_builtin_sources()
    src = _SOURCE_REGISTRY.get(str(site_key or "").strip())
    if src is not None and src.troubleshooting:
        return dict(src.troubleshooting)
    for item in _SOURCE_REGISTRY.values():
        if item.site_key == site_key and item.troubleshooting:
            return dict(item.troubleshooting)
    return {}
