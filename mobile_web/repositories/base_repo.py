# -*- coding: utf-8 -*-
"""MongoDB 仓储基础设施：延迟连接、可注入数据库及可重试的索引初始化。"""
import threading
from infrastructure.mongo import gen_db_object
from config import logger


class BaseRepository:
    COLLECTION_NAME: str = ""
    _initialized_collections = set()
    _initialization_lock = threading.RLock()

    def __init__(self, db_instance=None):
        # 路由/服务装配不触发网络 I/O；真实连接和索引在首次仓储操作时获取。
        self._db = db_instance
        self._index_state = threading.local()
        if db_instance is not None and self.COLLECTION_NAME:
            self._ensure_indexes()

    def _ensure_indexes(self):
        if not self.COLLECTION_NAME or getattr(self._index_state, "building", False) or self._db is None:
            return
        with self._initialization_lock:
            # 优先挂载在具体 db 实例上，彻底避免短生命周期测试 mock 对象 GC 后 id() 地址重用冲突
            initialized = getattr(self._db, "_initialized_collections", None)
            if not isinstance(initialized, set):
                initialized = set()
                try:
                    self._db._initialized_collections = initialized
                except Exception:
                    initialized = None
            if initialized is not None:
                if self.COLLECTION_NAME in initialized:
                    return
            else:
                key = (id(self._db), self.COLLECTION_NAME)
                if key in self._initialized_collections:
                    return
            self._index_state.building = True
            try:
                if self._init_indexes() is not False:
                    if initialized is not None:
                        initialized.add(self.COLLECTION_NAME)
                    else:
                        self._initialized_collections.add((id(self._db), self.COLLECTION_NAME))
            except Exception as exc:
                logger.warning(f"{self.COLLECTION_NAME} 索引初始化失败，后续访问将重试: {exc}")
            finally:
                self._index_state.building = False

    @property
    def col(self):
        if not self.COLLECTION_NAME:
            raise NotImplementedError("子类仓储必须声明 COLLECTION_NAME")
        if self._db is None:
            with self._initialization_lock:
                if self._db is None:
                    self._db = gen_db_object()
        self._ensure_indexes()
        return self._db.get_collection(self.COLLECTION_NAME)

    def _init_indexes(self):
        """子类失败须返回 False 或抛异常，不能误登记为已完成。"""
        return True
