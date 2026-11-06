# -*- coding: utf-8 -*-
"""MongoDB 仓储基础设施：延迟连接、可注入数据库及可重试的索引初始化。"""
import threading
from infrastructure.mongo import gen_db_object
from config import logger


class _CollectionIndexState:
    def __init__(self):
        self.lock = threading.RLock()
        self.ready = False


class BaseRepository:
    COLLECTION_NAME: str = ""
    # 只保护数据库上的状态字典装配；连接与索引 DDL 都不持有此锁。
    _initialization_lock = threading.RLock()

    def __init__(self, db_instance=None):
        # 路由/服务装配不触发网络 I/O；真实连接和索引在首次仓储操作时获取。
        self._db = db_instance
        self._connection_lock = threading.RLock()
        self._index_state = threading.local()
        self._collection_index_state = None
        if db_instance is not None and self.COLLECTION_NAME:
            self._ensure_indexes()

    def _get_collection_index_state(self):
        cached = self._collection_index_state
        if cached is not None and cached[0] is self._db and cached[1] == self.COLLECTION_NAME:
            return cached[2]
        with self._initialization_lock:
            cached = self._collection_index_state
            if cached is not None and cached[0] is self._db and cached[1] == self.COLLECTION_NAME:
                return cached[2]
            # 状态跟随具体 db 生命周期，不能用永久全局 id() 集合记录短生命周期 mock。
            states = getattr(self._db, "_repository_index_states", None)
            if not isinstance(states, dict):
                states = {}
                try:
                    self._db._repository_index_states = states
                except Exception:
                    # 不允许附加属性的注入对象使用仓储实例自己的状态；不强持有全局引用。
                    pass
            state = states.setdefault(self.COLLECTION_NAME, _CollectionIndexState())
            self._collection_index_state = (self._db, self.COLLECTION_NAME, state)
            return state

    def _ensure_indexes(self):
        if not self.COLLECTION_NAME or getattr(self._index_state, "building", False) or self._db is None:
            return
        state = self._get_collection_index_state()
        if state.ready:
            return
        with state.lock:
            if state.ready:
                return
            self._index_state.building = True
            try:
                if self._init_indexes() is not False:
                    state.ready = True
            except Exception as exc:
                logger.warning(f"{self.COLLECTION_NAME} 索引初始化失败，后续访问将重试: {exc}")
            finally:
                self._index_state.building = False

    @property
    def col(self):
        if not self.COLLECTION_NAME:
            raise NotImplementedError("子类仓储必须声明 COLLECTION_NAME")
        if self._db is None:
            with self._connection_lock:
                if self._db is None:
                    self._db = gen_db_object()
        self._ensure_indexes()
        return self._db.get_collection(self.COLLECTION_NAME)

    def _init_indexes(self):
        """子类失败须返回 False 或抛异常，不能误登记为已完成。"""
        return True
