# -*- coding: utf-8 -*-
"""内置原 common.mongo_db.mongo_base 的连接基础设施，不包含其他采集器依赖。"""
import urllib.parse

from pymongo import MongoClient

from infrastructure.settings import get_settings


class MongoBase:
    """连接池由仓储拥有；保留原有延迟连接、UTC 与超时约定。"""

    def __init__(self, host="localhost", port=27017, username=None, password=None,
                 db_name="admin", auth_source="admin", max_pool_size=100,
                 timeout_ms=5000):
        if bool(username) != bool(password):
            raise ValueError("MongoDB 用户名和密码必须同时配置")
        if username and password:
            username = urllib.parse.quote_plus(username)
            password = urllib.parse.quote_plus(password)
            uri = f"mongodb://{username}:{password}@{host}:{port}/{auth_source}"
        else:
            uri = f"mongodb://{host}:{port}/"
        self._client = MongoClient(
            uri, maxPoolSize=max_pool_size, connect=False, tz_aware=True,
            serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
        )
        try:
            self.db = self._client[db_name]
        except BaseException:
            self._client.close()
            raise

    def ping(self):
        if self._client is None:
            raise RuntimeError("MongoDB 连接已关闭")
        self._client.admin.command("ping")

    def close(self):
        client = self._client
        self._client = self.db = None
        if client is not None:
            client.close()

    def get_collection(self, collection_name):
        if self.db is None:
            raise RuntimeError("MongoDB 连接已关闭")
        return self.db[collection_name]

    def find_many(self, collection_name, query=None, projection=None, limit=0, sort=None):
        if type(limit) is not int or limit < 0:
            raise ValueError("limit 必须是非负整数，0 表示不限量")
        with self.get_collection(collection_name).find(
                {} if query is None else query, projection) as cursor:
            if sort:
                cursor = cursor.sort(sort)
            if limit:
                cursor = cursor.limit(limit)
            return list(cursor)

    def create_index(self, collection_name, keys, unique=False):
        return self.get_collection(collection_name).create_index(keys, unique=unique)


def gen_db_object(settings=None):
    """与应用常量共用配置，不再独立读取仓库外 JSON。"""
    settings = get_settings() if settings is None else settings
    return MongoBase(
        host=settings.mongo_host, port=settings.mongo_port,
        username=settings.mongo_user, password=settings.mongo_password,
        db_name=settings.mongo_db_name, auth_source=settings.mongo_auth_source,
        max_pool_size=50,
    )
