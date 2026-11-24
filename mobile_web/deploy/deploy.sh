#!/usr/bin/env bash
set -uo pipefail

# ==============================================================================
# easy_shop_web 一键部署与日常更新脚本
# 用法：
#   1. 首次上线/旧版升级（装 Meilisearch + 配 Nginx + 一键申请 HTTPS）：
#      bash /root/easy_shop_web/mobile_web/deploy/deploy.sh --init --email 你的邮箱
#   2. 以后每次日常更新代码（只拉代码 + 同步依赖 + 自检 + 重启 + 出状态报告）：
#      bash /root/easy_shop_web/mobile_web/deploy/deploy.sh
#   3. 常用可选参数：
#      --init          首次部署或旧版升级（装免 Docker Meilisearch + 初始化 Nginx）
#      --email <addr>  配合 --init 使用，若尚未配置 HTTPS 则自动申请 Certbot 证书
#      --sync-meili    强制执行一次全量 Meilisearch 索引同步
#      --skip-pull     跳过 git pull（本地调试或已手动拉取代码时使用）
#      --force-nginx   强制覆盖 Nginx 站点配置（自动备份旧文件，慎用）
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${APP_DIR}/.." && pwd)"
VENV_PY="${APP_DIR}/.venv/bin/python"
CONFIG_FILE="${APP_DIR}/config.local.json"
NGINX_SRC="${SCRIPT_DIR}/mobile_web.nginx.conf"
NGINX_AVAIL="/etc/nginx/sites-available/shop.easyeverything.cn.conf"
NGINX_ENABLED="/etc/nginx/sites-enabled/shop.easyeverything.cn.conf"
MEILI_BIN="/opt/meilisearch/meilisearch"
MEILI_VERSION="v1.12.8"

DO_INIT=0
DO_SYNC_MEILI=0
SKIP_PULL=0
FORCE_NGINX=0
CERTBOT_EMAIL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --init)
      DO_INIT=1
      shift
      ;;
    --sync-meili)
      DO_SYNC_MEILI=1
      shift
      ;;
    --skip-pull)
      SKIP_PULL=1
      shift
      ;;
    --force-nginx)
      FORCE_NGINX=1
      shift
      ;;
    --email)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "❌ --email 需要提供有效邮箱地址" >&2
        exit 2
      fi
      CERTBOT_EMAIL="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '4,20p' "$0"
      exit 0
      ;;
    *)
      echo "❌ 未知参数: $1 (运行 --help 查看用法)" >&2
      exit 2
      ;;
  esac
done

CRITICAL_ERRORS=0

ST_GIT="⏭️ 已跳过"
ST_PIP="⏭️ 未运行"
ST_CFG="❌ 未通过"
ST_MONGO="❌ 未通过"
ST_AI="⚠️ 未检测"
ST_MEILI="⏭️ 未启用"
ST_WEB="❌ 未运行"
ST_NGINX="⚠️ 未检测"
ST_HTTPS="⚠️ 未配置"
ST_LOCAL_HTTP="❌ 未检测"

log_step() {
  echo ""
  echo "============================================================"
  echo "▶ $1"
  echo "============================================================"
}

# ------------------------------------------------------------------------------
# 0. 基础前提校验
# ------------------------------------------------------------------------------
log_step "0. 检查运行环境与私有配置"
if [[ ! -f "${CONFIG_FILE}" ]]; then
  echo "❌ 缺少配置文件: ${CONFIG_FILE}"
  echo "   请先从本地全额上传 config.local.json 到服务器后重新运行本脚本。"
  exit 1
fi
chmod 600 "${CONFIG_FILE}" 2>/dev/null || true

if [[ ! -x "${VENV_PY}" ]]; then
  echo "ℹ️ 未检测到虚拟环境，正在初始化 Python .venv..."
  python3 -m venv "${APP_DIR}/.venv" || {
    echo "❌ 创建虚拟环境失败，请先确认已安装 python3-venv"
    exit 1
  }
fi

# ------------------------------------------------------------------------------
# 1. 拉取最新代码
# ------------------------------------------------------------------------------
if [[ "${SKIP_PULL}" -eq 0 ]]; then
  log_step "1. 拉取 GitHub 最新代码 (main)"
  cd "${REPO_ROOT}"
  if git pull origin main; then
    ST_GIT="✅ 已同步 ($(git rev-parse --short HEAD 2>/dev/null || echo 'unknown'))"
  else
    echo "⚠️ 直连 GitHub 失败，尝试国内镜像通道 ghfast.top..."
    if git pull https://ghfast.top/https://github.com/differentname123/easy_shop_web.git main; then
      ST_GIT="✅ 镜像同步 ($(git rev-parse --short HEAD 2>/dev/null || echo 'unknown'))"
    else
      echo "❌ 拉取代码失败，请检查网络或本地未提交修改"
      ST_GIT="❌ 拉取失败"
      CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
    fi
  fi
else
  cd "${REPO_ROOT}"
  ST_GIT="⏭️ 跳过拉取 ($(git rev-parse --short HEAD 2>/dev/null || echo 'unknown'))"
fi

# ------------------------------------------------------------------------------
# 2. 同步 Python 依赖（腾讯云镜像源加速）
# ------------------------------------------------------------------------------
log_step "2. 同步 Python 运行依赖"
if "${VENV_PY}" -m pip install -i https://mirrors.cloud.tencent.com/pypi/simple -r "${APP_DIR}/requirements.txt"; then
  ST_PIP="✅ 依赖完整"
else
  echo "❌ 依赖安装失败"
  ST_PIP="❌ 安装失败"
  CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
fi

# 从 config.local.json 安全提取当前运行参数（本地/服务器 100% 同一真源）
read_cfg() {
  "${VENV_PY}" - "$CONFIG_FILE" "$1" "$2" << 'PYEOF'
import json, sys
with open(sys.argv[1], "r", encoding="utf-8-sig") as f:
    data = json.load(f)
val = data.get(sys.argv[2], sys.argv[3])
if isinstance(val, bool):
    print("true" if val else "false")
elif val is None:
    print(sys.argv[3])
else:
    print(str(val))
PYEOF
}

WEB_PORT="$(read_cfg web_port 8001)"
MEILI_ENABLED="$(read_cfg meilisearch_enabled true)"
MEILI_URL="$(read_cfg meilisearch_url http://127.0.0.1:7700)"
MEILI_KEY="$(read_cfg meilisearch_key easyshop2026)"

# ------------------------------------------------------------------------------
# 3. 无损自检（配置 / MongoDB / 云端 AI）
# ------------------------------------------------------------------------------
log_step "3. 运行配置、数据库与云端 AI 自检"
if "${VENV_PY}" "${APP_DIR}/run.py" --check; then
  ST_CFG="✅ 校验通过"
else
  ST_CFG="❌ 配置报错"
  CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
fi

if "${VENV_PY}" "${APP_DIR}/run.py" --check-mongo; then
  ST_MONGO="✅ 连接正常"
else
  ST_MONGO="❌ Mongo 异常"
  CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
fi

if "${VENV_PY}" "${APP_DIR}/run.py" --check-ai; then
  ST_AI="✅ 云端识图可用"
else
  ST_AI="⚠️ 识图不可用(不影响商城主站，仅影响拼团图片AI识别)"
fi

# ------------------------------------------------------------------------------
# 4. 安装与检查 Meilisearch 搜索服务（免 Docker 单二进制 + systemd）
# ------------------------------------------------------------------------------
log_step "4. 检查 / 部署 Meilisearch 全文搜索服务"
check_meili_health() {
  curl -sf --max-time 2 "http://127.0.0.1:7700/health" 2>/dev/null | grep -q '"available"'
}

if [[ "${MEILI_ENABLED}" != "true" ]]; then
  ST_MEILI="⏭️ 配置已关闭 (meilisearch_enabled=false)"
else
  if ! check_meili_health && [[ "${DO_INIT}" -eq 1 ]]; then
    if [[ ! -x "${MEILI_BIN}" ]]; then
      echo "ℹ️ 未检测到 Meilisearch，正在下载 ${MEILI_VERSION} 单二进制文件（免 Docker）..."
      mkdir -p /data/meili_data /opt/meilisearch
      TMP_BIN="$(mktemp /tmp/meilisearch.XXXXXX)"
      DOWNLOADED=0
      for url in \
        "https://ghfast.top/https://github.com/meilisearch/meilisearch/releases/download/${MEILI_VERSION}/meilisearch-linux-amd64" \
        "https://github.com/meilisearch/meilisearch/releases/download/${MEILI_VERSION}/meilisearch-linux-amd64"; do
        echo "  尝试下载: ${url}"
        if curl -fL --connect-timeout 15 --max-time 300 "${url}" -o "${TMP_BIN}"; then
          chmod +x "${TMP_BIN}"
          if "${TMP_BIN}" --version >/dev/null 2>&1; then
            mv -f "${TMP_BIN}" "${MEILI_BIN}"
            DOWNLOADED=1
            break
          fi
        fi
        rm -f "${TMP_BIN}"
      done
      if [[ "${DOWNLOADED}" -ne 1 ]]; then
        echo "❌ 下载 Meilisearch 失败，请检查网络后重试"
        ST_MEILI="❌ 下载失败"
        CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
      fi
    fi

    if [[ -x "${MEILI_BIN}" ]]; then
      cat > /etc/systemd/system/meilisearch.service << EOF
[Unit]
Description=Meilisearch Lightweight Search Engine
After=network.target

[Service]
Type=simple
ExecStart=${MEILI_BIN} \\
  --http-addr 127.0.0.1:7700 \\
  --db-path /data/meili_data \\
  --master-key ${MEILI_KEY} \\
  --max-indexing-memory 256Mb \\
  --no-analytics
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
      chmod 600 /etc/systemd/system/meilisearch.service
      systemctl daemon-reload
      systemctl enable --now meilisearch.service
      systemctl restart meilisearch.service

      for _ in $(seq 1 15); do
        if check_meili_health; then
          break
        fi
        sleep 1
      done
    fi
  fi

  if check_meili_health; then
    if [[ "${DO_INIT}" -eq 1 || "${DO_SYNC_MEILI}" -eq 1 ]]; then
      echo "ℹ️ 执行商品库 -> Meilisearch 全量索引同步..."
      if "${VENV_PY}" "${APP_DIR}/scripts/sync_meilisearch.py"; then
        ST_MEILI="✅ 运行中且已全量建索引"
      else
        ST_MEILI="⚠️ 运行中但全量同步失败(后台每120s会自动补齐)"
      fi
    else
      ST_MEILI="✅ 运行正常 (后台每120s自动增量对齐)"
    fi
    MEILI_DOCS="$("${VENV_PY}" - "$MEILI_URL" "$MEILI_KEY" << 'PYEOF' 2>/dev/null || echo "?"
import sys, urllib.request, json
req = urllib.request.Request(
    sys.argv[1].rstrip("/") + "/indexes/products/stats",
    headers={"Authorization": "Bearer " + sys.argv[2]} if sys.argv[2] else {},
)
with urllib.request.urlopen(req, timeout=3) as resp:
    print(json.loads(resp.read().decode("utf-8")).get("numberOfDocuments", "?"))
PYEOF
)"
    ST_MEILI="${ST_MEILI} [索引商品数: ${MEILI_DOCS}]"
  else
    echo "❌ Meilisearch (127.0.0.1:7700) 未启动或不可用（如为首次部署请带 --init 参数）"
    ST_MEILI="❌ 未运行 (当前业务会自动降级回 MongoDB 搜索)"
    if [[ "${DO_INIT}" -eq 1 ]]; then
      CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
    fi
  fi
fi

# ------------------------------------------------------------------------------
# 5. 启动 / 重启商城主服务 (mobile_web.service)
# ------------------------------------------------------------------------------
log_step "5. 启动 / 重启商城服务 (mobile_web.service)"
if [[ ! -f /etc/systemd/system/mobile_web.service ]]; then
  cat > /etc/systemd/system/mobile_web.service << EOF
[Unit]
Description=EasyShop Mobile Web
After=network-online.target mongod.service
Wants=network-online.target mongod.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_PY} ${APP_DIR}/run.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable mobile_web.service
fi

if systemctl restart mobile_web.service; then
  sleep 2
  if systemctl is-active --quiet mobile_web.service; then
    ST_WEB="✅ active (running) [端口 ${WEB_PORT}]"
  else
    ST_WEB="❌ 启动后退出"
    CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
  fi
else
  ST_WEB="❌ 重启失败"
  CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
fi

# ------------------------------------------------------------------------------
# 6. 配置 Nginx 与可选 HTTPS 证书（不覆盖已配好的 443 证书）
# ------------------------------------------------------------------------------
log_step "6. 检查 / 部署 Nginx 规则与 HTTPS"
if command -v nginx >/dev/null 2>&1; then
  mkdir -p /var/www/html
  if [[ "${FORCE_NGINX}" -eq 1 ]] || [[ "${DO_INIT}" -eq 1 && ( ! -f "${NGINX_AVAIL}" || -z "$(grep -s 'ssl_certificate' "${NGINX_AVAIL}" || true)" ) ]]; then
    if [[ -f "${NGINX_AVAIL}" ]]; then
      cp "${NGINX_AVAIL}" "${NGINX_AVAIL}.bak.$(date +%Y%m%d%H%M%S)"
    fi
    cp "${NGINX_SRC}" "${NGINX_AVAIL}"
    rm -f /etc/nginx/sites-enabled/default
    ln -sf "${NGINX_AVAIL}" "${NGINX_ENABLED}"
    echo "ℹ️ 已更新 Nginx 站点配置: ${NGINX_AVAIL}"
  elif [[ -f "${NGINX_AVAIL}" ]]; then
    echo "ℹ️ 检测到现有 Nginx 配置，跳过覆盖以保留当前配置/HTTPS 证书（强制更新可加 --force-nginx）"
  else
    echo "⚠️ 未找到 ${NGINX_AVAIL}（首次配置请加上 --init 参数）"
  fi

  if [[ -n "${CERTBOT_EMAIL}" ]] && [[ -f "${NGINX_AVAIL}" ]] && ! grep -q "ssl_certificate" "${NGINX_AVAIL}"; then
    if ! command -v certbot >/dev/null 2>&1; then
      echo "ℹ️ 正在安装 Certbot..."
      apt-get update && apt-get install -y certbot python3-certbot-nginx
    fi
    certbot --nginx --non-interactive --agree-tos -m "${CERTBOT_EMAIL}" \
      -d www.shop.easyeverything.cn -d shop.easyeverything.cn || true
  fi

  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx || systemctl start nginx || true
    ST_NGINX="✅ 语法正确且运行中"
  else
    echo "❌ nginx -t 校验失败："
    nginx -t || true
    ST_NGINX="❌ 配置校验失败"
    CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
  fi

  if grep -qs "ssl_certificate" "${NGINX_AVAIL}" /etc/nginx/sites-enabled/*.conf 2>/dev/null; then
    ST_HTTPS="✅ 已配置 SSL 证书"
  else
    ST_HTTPS="⚠️ 未配 443 证书 (可加 --init --email 你的邮箱 自动申请)"
  fi
else
  ST_NGINX="⚠️ 系统未安装 Nginx"
fi

# ------------------------------------------------------------------------------
# 7. 本机端到端抽检与状态总览面板
# ------------------------------------------------------------------------------
HEALTH_BODY="$(curl -sf --max-time 3 "http://127.0.0.1:${WEB_PORT}/health" 2>/dev/null || true)"
if echo "${HEALTH_BODY}" | grep -q '"ok"'; then
  ST_LOCAL_HTTP="✅ /health 正常"
else
  ST_LOCAL_HTTP="❌ 127.0.0.1:${WEB_PORT}/health 无响应"
  CRITICAL_ERRORS=$((CRITICAL_ERRORS + 1))
fi

NGINX_HOME_CODE="$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 -H "Host: www.shop.easyeverything.cn" "http://127.0.0.1/" 2>/dev/null || echo "000")"
NGINX_DOCS_CODE="$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 -H "Host: www.shop.easyeverything.cn" "http://127.0.0.1/docs" 2>/dev/null || echo "000")"

echo ""
echo "============================================================"
echo "📊 easy_shop_web 部署与运行状态总览"
echo "============================================================"
echo "  Git 代码版本     : ${ST_GIT}"
echo "  Python 依赖      : ${ST_PIP}"
echo "  私有配置检查     : ${ST_CFG}"
echo "  MongoDB 数据库   : ${ST_MONGO}"
echo "  云端 AI 识图     : ${ST_AI}"
echo "  Meilisearch 搜索 : ${ST_MEILI}"
echo "  商城后端服务     : ${ST_WEB}"
echo "  后端健康接口     : ${ST_LOCAL_HTTP}"
echo "  Nginx 服务       : ${ST_NGINX}"
echo "  HTTPS 证书       : ${ST_HTTPS}"
echo "  Nginx 转发抽检   : 首页状态=${NGINX_HOME_CODE} | /docs状态=${NGINX_DOCS_CODE} (期望404)"
echo "============================================================"

if [[ "${CRITICAL_ERRORS}" -gt 0 ]]; then
  echo "❌ 本次部署存在 ${CRITICAL_ERRORS} 项关键异常，请按上方提示检查或执行："
  echo "   journalctl -u mobile_web.service -n 50 --no-pager"
  exit 1
fi

echo "🎉 部署全部成功！请访问 https://www.shop.easyeverything.cn 验证页面、搜索与转链。"
exit 0
