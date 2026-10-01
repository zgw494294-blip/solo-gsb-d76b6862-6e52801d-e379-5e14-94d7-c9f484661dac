# 双槽固件试启动 HTTP API

用 Python/FastAPI + SQLite 模拟设备 A/B 双槽固件更新与试启动，**不连接任何硬件**。

## 规则概览

- 每个镜像硬上限 **64 KiB**；创建设备时写入已确认的初始镜像（槽 A）。
- 更新**只会写另一槽（备槽）**：先声明长度与 SHA-256，再按字节偏移提交 Base64 分块。
- 同一位置、同样内容可重复提交（幂等，返回 `duplicate: true`）；与已有分块重叠且字节不一致时，**整块拒绝（409）**。
- 分块覆盖完整且 SHA-256 与声明相符，槽位才变为 `complete`，才可作为候选启动；不完整或摘要错误的候选**不能启动**，已确认镜像仍可启动。
- 设备重启（`/restart`）时若有合格候选：试启候选，返回唯一 32 字节十六进制**启动令牌**，并在返回前把“已试启/待确认”标记和令牌持久化。
- 凭令牌 `/confirm` 才会把候选提升为已确认镜像；**未确认就再次重启 → 回退旧镜像并废弃候选**；此后迟到的确认返回 **409**。
- 试启未决（trial pending）期间，**两个槽都禁止写入**（声明更新、传分块均 409）。
- 可查询两槽摘要/状态、当前运行镜像、待确认令牌。
- **服务重启不等于设备重启**：分块、槽位、试启状态、令牌全部保存在 SQLite（持久卷）。
- 单 worker + 全局写锁 + SQLite WAL 事务，并发请求等价于某种串行执行。

## 一条命令启动

```bash
docker compose up -d --build
```

首次启动会自动创建并初始化持久化命名卷 `firmware-data`（挂载到容器 `/data`，SQLite 文件为 `/data/firmware.db`）。

- 端口：**8000**
- 健康检查：`GET http://localhost:8000/health`
- 交互式文档：`http://localhost:8000/docs`

停止与清理（加 `-v` 才会删除数据卷）：

```bash
docker compose down        # 保留数据
docker compose down -v     # 连同固件数据一起删除
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 创建设备（存入已确认初始镜像） |
| GET | `/devices` | 列出设备 ID |
| GET | `/devices/{id}` | 查询两槽摘要、状态、运行镜像、待确认令牌 |
| POST | `/devices/{id}/updates` | 声明更新（长度 + SHA-256），目标为另一槽 |
| PUT | `/devices/{id}/chunks` | 按偏移提交 Base64 分块 |
| POST | `/devices/{id}/restart` | 模拟设备重启（试启 / 回退 / 启旧镜像） |
| POST | `/devices/{id}/confirm` | 凭启动令牌确认，提升候选 |

## HTTP 示例

以下示例中 `:8000` 即 `http://localhost:8000`。用 `base64`/`sha256sum` 生成测试载荷，
`jq` 读取返回的令牌。

### 1. 造一个初始镜像并创建设备

```bash
# 1 KiB 的 v1 固件
head -c 1024 /dev/urandom > fw-v1.bin
V1_B64=$(base64 -w0 fw-v1.bin)
V1_SHA=$(sha256sum fw-v1.bin | cut -d' ' -f1)

curl -sS -X POST :8000/devices \
  -H 'Content-Type: application/json' \
  -d "{\"device_id\":\"dev1\",\"initial_image_base64\":\"$V1_B64\",\"initial_image_sha256\":\"$V1_SHA\"}" | jq
```

槽 A 为 `confirmed`，槽 B 为 `empty`，运行槽为 A。

### 2. 查询状态

```bash
curl -sS :8000/devices/dev1 | jq
```

### 3. 声明更新（只写另一槽 = 槽 B）

```bash
head -c 2048 /dev/urandom > fw-v2.bin
V2_B64=$(base64 -w0 fw-v2.bin)
V2_SHA=$(sha256sum fw-v2.bin | cut -d' ' -f1)
V2_LEN=$(stat -c%s fw-v2.bin)

curl -sS -X POST :8000/devices/dev1/updates \
  -H 'Content-Type: application/json' \
  -d "{\"declared_length\":$V2_LEN,\"sha256\":\"$V2_SHA\"}" | jq
# => {"target_slot":"b","slot":{"status":"uploading", ...}}
```

### 4. 按偏移分块提交（Base64）

以 512 字节为一块。每块请求体为 `{"offset": <字节偏移>, "data": "<base64>"}`：

```bash
python3 - <<'PY'
import base64, json, urllib.request

data = open("fw-v2.bin", "rb").read()
CHUNK = 512
for off in range(0, len(data), CHUNK):
    piece = data[off:off + CHUNK]
    body = json.dumps({"offset": off,
                       "data": base64.b64encode(piece).decode()}).encode()
    req = urllib.request.Request(
        "http://localhost:8000/devices/dev1/chunks",
        data=body, method="PUT",
        headers={"Content-Type": "application/json"})
    print(off, json.load(urllib.request.urlopen(req))["complete"])
PY
```

最后一块返回 `"complete": true`，槽 B 状态变为 `complete`、`verified: true`。

- 重传同一偏移的相同内容：返回 `200` 且 `"duplicate": true`。
- 重叠但字节不同：返回 `409`，整块不写入。
- 超出声明长度：返回 `416`；镜像超过 64 KiB：创建/声明阶段即被拒。

### 5. 重启 → 试启候选，取得唯一令牌

```bash
TOKEN=$(curl -sS -X POST :8000/devices/dev1/restart | jq -r .token)
echo "$TOKEN"
# => {"action":"trial","token":"....","trial_pending":true,"running_slot":"b", ...}
```

“已试启”标记与令牌在响应返回前已落库；此时重启 API 服务进程后再查询，
`trial_pending` 与 `pending_confirmation_token` 仍在。

试启未决期间写槽会被拒：

```bash
curl -sS -X POST :8000/devices/dev1/updates \
  -H 'Content-Type: application/json' \
  -d "{\"declared_length\":1,\"sha256\":\"$(printf 0%.0s {1..64})\"}"
# => 409 trial boot is pending ...
```

### 6a. 确认试启 → 候选成为已确认镜像

```bash
curl -sS -X POST :8000/devices/dev1/confirm \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}" | jq
# => {"action":"confirmed","confirmed_slot":"b","trial_pending":false, ...}
# 旧槽 A 已清空，下一次更新将写入 A。
```

### 6b. 不确认、再次重启 → 回退旧镜像并废弃候选

若上一步没有确认，而是再次重启：

```bash
curl -sS -X POST :8000/devices/dev1/restart | jq
# => {"action":"rollback","running_slot":"a","trial_pending":false, ...}
# 槽 B 的不完整/候选数据被清空。
```

迟到的确认令牌失效：

```bash
curl -sS -X POST :8000/devices/dev1/confirm \
  -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}"
# => 409 no trial boot pending; confirmation rejected
```

## 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt
FIRMWARE_DB_PATH=./firmware.db \
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```
