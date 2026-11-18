# Meilisearch 搜索引擎极简部署与使用教程

本项目现已全面接入 **Meilisearch** 高性能全文搜索引擎，为“严选好价”和商品库提供毫秒级模糊搜索、中文分词、纠错容错与高相关性排序，彻底解决此前正则搜索无法匹配及排序失真的问题。

---

## 一、本地开发环境启动（只需 1 条命令）

确保本地已安装并运行 Docker，在命令行中直接运行以下单行命令：

```bash
docker run -d --name meilisearch --restart always -p 7700:7700 -v meili_data:/meili_data -e MEILI_NO_ANALYTICS=true -e MEILI_MASTER_KEY=easyshop2026 getmeili/meilisearch:v1.12
```

> **参数说明**：
> - `-p 7700:7700`：对外暴露端口 7700。
> - `-v meili_data:/meili_data`：数据持久化到本地 Docker Volume，容器重启数据不丢失。
> - `MEILI_MASTER_KEY=easyshop2026`：接口密钥（已默认写入项目配置）。

---

## 二、云端服务器部署（Linux）

在腾讯云/阿里云等服务器终端中，同样只需一条 Docker 命令启动：

```bash
docker run -d --name meilisearch --restart always -p 127.0.0.1:7700:7700 -v /data/meili_data:/meili_data -e MEILI_NO_ANALYTICS=true -e MEILI_MASTER_KEY=easyshop2026 getmeili/meilisearch:v1.12
```

> **安全提示**：在云端服务器上，建议使用 `-p 127.0.0.1:7700:7700` 绑定本地回环，防止 7700 端口向公网裸露；若跨服务器访问，请配置服务器防火墙/安全组仅允许 Web 机器 IP 访问。

---

## 三、全量同步 / 重建商品索引（1 条命令）

当首次启动 Meilisearch，或者需要全量从 MongoDB 同步全部商品到索引库时，只需在工程根目录下运行：

```bash
python mobile_web/scripts/sync_meilisearch.py
```

- 若需清空旧索引从零重建，追加 `--reset` 参数：
  ```bash
  python mobile_web/scripts/sync_meilisearch.py --reset
  ```

---

## 四、项目配置说明

配置文件位于 `mobile_web/config.local.json`（已自动配置，通常无需改动）：

```json
{
  "meilisearch_url": "http://localhost:7700",
  "meilisearch_key": "easyshop2026",
  "meilisearch_enabled": true
}
```

- `meilisearch_url`: Meilisearch 服务地址。
- `meilisearch_key`: 启动容器时设置的 `MEILI_MASTER_KEY`。
- `meilisearch_enabled`: 总开关（默认 `true`）。

---

## 五、自动写穿透与优雅降级保障

1. **自动写穿透**：当爬虫采集流或详情查询向 MongoDB 写入新商品（`upsert_home_products` / `upsert_product_detail`）时，系统会自动增量同步至 Meilisearch，无需人工干预。
2. **优雅降级**：若 Meilisearch 容器停机或不可用，系统内置 10 秒 TTL 熔断探活机制，会自动无缝回退至原生的 MongoDB 正则查询逻辑，**保证前台服务绝对不崩溃、零中断**。
