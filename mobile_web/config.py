# -*- coding: utf-8 -*-
"""
移动端服务全局配置模块
"""
from infrastructure.logging_utils import logger
from infrastructure.settings import get_settings

settings = get_settings()

# 拼多多、仓储与启动入口消费同一份配置，不回溯仓库父级。
PDD_CLIENT_ID = settings.pdd_client_id
PDD_CLIENT_SECRET = settings.pdd_client_secret
PDD_PID = settings.pdd_pid
MONGO_HOST = settings.mongo_host
MONGO_PORT = settings.mongo_port
MONGO_USER = settings.mongo_user
MONGO_PASSWORD = settings.mongo_password
MONGO_DB_NAME = settings.mongo_db_name
MONGO_AUTH_SOURCE = settings.mongo_auth_source
WEB_HOST = settings.web_host
WEB_PORT = settings.web_port
AUTH_SECRET = settings.auth_secret

# 业务安全与防刷约束常量
ORDER_SYNC_INTERVAL = 180       # 增量订单拉取全局最小冷却间隔 (秒)
ORDER_SYNC_MAX_PAGES = 5        # 单次增量拉单最大翻页数
AUTH_STATUS_TTL = 86400         # 已确认平台授权状态的本地有效期
PRODUCT_DETAIL_TTL = 600        # 官方详情有效期，摘要更新不能延长它
PROMOTION_LINK_TTL = 3 * 86400   # 站长推广链接的来源有效期，不由内存回填续期
CACHE_MAX_RECOMMEND = 256       # 首页推荐流缓存最大条目
CACHE_MAX_DETAIL = 2048         # 商品详情缓存最大条目
CACHE_MAX_SEARCH = 1024         # 搜索缓存最大条目
CACHE_MAX_PROMOTION = 4096      # 推手专属转链缓存最大条目
CACHE_MAX_CATEGORY = 32         # 类目缓存最大条目

