# 双槽固件试启动 HTTP API

用 Python / FastAPI + SQLite 模拟 A/B 双槽设备的固件更新、试启动、确认与回退。
**不连接任何硬件**，所有“刷写/重启”都是数据库状态迁移。

## 启动

单条命令（自动构建镜像、初始化持久卷并启动）：

```bash
docker compose up --build -d
```

- 端口：**`8000`**（HTTP）
- API 文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：`GET http://localhost:8000/health`
- 持久化：命名卷 `firmware-api_firmware-data` 挂载到容器 `/data`，
  SQLite 文件 `/data/firmware.db`。卷首次启动时自动创建，
  **服务（进程）重启后分块上传进度、试启令牌等状态全部保留**；
  只有对设备调用 `POST /restart` 才算“设备重启”。

停止 / 重置数据：

```bash
docker compose down            # 停止，保留数据卷
docker compose down -v         # 停止并删除数据卷（清空所有设备）
docker compose logs -f         # 查看日志
```

无 Docker 时本地运行（Python 3.11+）：

```bash
pip install -r requirements.txt
FIRMWARE_DB_PATH=./firmware.db uvicorn app.main:app --port 8000
```

## 核心规则

- 每个镜像不超过 **64 KiB**（65536 字节）。
- 创建设备时写入**已确认的初始镜像**（槽 0），槽 1 为空。
- 更新**只能写另一槽**：先声明长度和 SHA-256，再按偏移提交 Base64 分块。
  - 同一偏移、相同内容可任意重传（幂等）。
  - 与已提交分块**重叠字节不同 → 整块拒绝**（`409 overlap_conflict`），不写入。
  - 分块越界（超过声明长度）→ `422`。
  - 重新声明（再次 `PUT /update`）会废弃候选槽已有分块，从头开始。
- 候选槽**字节完整且 SHA-256 相符**才是 `complete`，才可试启动；
  不完整为 `in-progress`，摘要错误为 `digest-mismatch`，二者都不能启动，旧镜像照常启动。
- `POST /restart`（设备重启）：
  - 候选可启动 → **优先试启候选**，返回唯一 `boot_token`；
    返回响应之前已把 `trial-pending` 与令牌持久化。
  - 候选不可启动 → 启动旧已确认镜像；废弃候选。
- 试启未决（`trial-pending`）期间：
  - `POST /confirm?token=...` 令牌正确 → 候选**替换**已确认镜像。
  - 未确认就再次 `POST /restart` → **回到旧镜像、废弃候选、令牌失效**；
    之后迟到的确认返回 **`409`**。
  - 两槽**禁止任何写入**（声明 / 分块都返回 `409 trial_pending_write_locked`）。
- 并发操作经 SQLite `BEGIN IMMEDIATE` 串行化，等价于某种串行执行。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 创建设备（带已确认初始镜像，≤64 KiB） |
| GET | `/devices` | 列出设备 ID |
| GET | `/devices/{id}` | 两槽摘要/状态、运行镜像、待确认令牌 |
| PUT | `/devices/{id}/update` | 在另一槽声明更新（长度 + SHA-256） |
| PUT | `/devices/{id}/chunks` | 提交一个 Base64 分块 `{offset, data_base64}` |
| GET | `/devices/{id}/candidate` | 候选槽进度与是否可试启动 |
| POST | `/devices/{id}/restart` | 设备重启：试启候选或回退旧镜像 |
| POST | `/devices/{id}/confirm?token=...` | 凭令牌确认试启 |
| GET | `/health` | 健康检查 |

## HTTP 示例

以下示例使用 `curl` + `jq`，`python3` 用于算摘要 / 切分块。

### 0. 准备一个固件文件（示例 100 KB 会被拒绝，这里用 ≤64 KiB）

```bash
head -c 20000 /dev/urandom > firmware-v1.bin
SHA=$(sha256sum firmware-v1.bin | cut -d' ' -f1)
LEN=$(stat -c%s firmware-v1.bin)
echo "$SHA $LEN"
```

### 1. 创建设备（初始镜像 = 已确认镜像，写入槽 0）

```bash
curl -s -X POST http://localhost:8000/devices \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "
import json, base64
print(json.dumps({
  'device_id': 'dev-001',
  'initial_image_base64': base64.b64encode(open('firmware-v1.bin','rb').read()).decode(),
}))")" | jq
```

### 2. 在另一槽声明更新（长度 + SHA-256）

```bash
head -c 30000 /dev/urandom > firmware-v2.bin
SHA2=$(sha256sum firmware-v2.bin | cut -d' ' -f1)
LEN2=$(stat -c%s firmware-v2.bin)

curl -s -X PUT http://localhost:8000/devices/dev-001/update \
  -H 'Content-Type: application/json' \
  -d "{\"length\": $LEN2, \"sha256\": \"$SHA2\"}" | jq '.slots.slot1'
```

### 3. 按偏移提交 Base64 分块（每块 4096 字节）

同位置同内容重跑本脚本是安全的（`identical_redelivery: true`）：

```bash
python3 - "$SHA2" <<'PY'
import base64, json, sys, urllib.request

DEV, CHUNK = "dev-001", 4096
data = open("firmware-v2.bin", "rb").read()
for off in range(0, len(data), CHUNK):
    body = json.dumps({
        "offset": off,
        "data_base64": base64.b64encode(data[off:off+CHUNK]).decode(),
    }).encode()
    req = urllib.request.Request(
        f"http://localhost:8000/devices/{DEV}/chunks",
        data=body, method="PUT",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        print(json.load(r)["candidate_status"], "@", off)
PY
```

重叠但内容不同会被拒：

```bash
# 拿前 100 字节，改掉一个字节后写到偏移 0（与已存内容重叠冲突）
python3 - <<'PY'
import base64, json, urllib.error, urllib.request
b = bytearray(open("firmware-v2.bin","rb").read(100)); b[5] ^= 0xFF
req = urllib.request.Request(
    "http://localhost:8000/devices/dev-001/chunks",
    data=json.dumps({"offset": 0,
        "data_base64": base64.b64encode(bytes(b)).decode()}).encode(),
    method="PUT", headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print(e.code, json.load(e))   # 409 overlap_conflict
PY
```

### 4. 查询状态（两槽摘要、状态、运行镜像、待确认令牌）

```bash
curl -s http://localhost:8000/devices/dev-001 | jq
```

### 5. 设备重启 → 优先试启候选，拿到唯一启动令牌

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/devices/dev-001/restart \
  | tee /tmp/restart.json | jq -r '.boot_token')
cat /tmp/restart.json | jq
echo "TOKEN=$TOKEN"
```

此时查询可见 `trial_state: "trial-pending"`、`pending_token` 非空、`running_slot: 1`。
试启未决时写两槽会被拒：

```bash
curl -s -X PUT http://localhost:8000/devices/dev-001/update \
  -H 'Content-Type: application/json' \
  -d "{\"length\": $LEN2, \"sha256\": \"$SHA2\"}" | jq
# 409 trial_pending_write_locked
```

### 6. 确认试启 → 候选替换已确认镜像

```bash
curl -s -X POST "http://localhost:8000/devices/dev-001/confirm?token=$TOKEN" | jq
# confirmed_slot 变为 1；槽 0 变为 superseded
```

## 回退示例（不确认，直接再次重启）

```bash
# 设备当前已确认运行槽 1。再来一版槽 0 的更新并试启：
head -c 12345 /dev/urandom > firmware-v3.bin
SHA3=$(sha256sum firmware-v3.bin | cut -d' ' -f1); LEN3=$(stat -c%s firmware-v3.bin)

curl -s -X PUT http://localhost:8000/devices/dev-001/update \
  -H 'Content-Type: application/json' \
  -d "{\"length\": $LEN3, \"sha256\": \"$SHA3\"}" | jq '.slots.slot0.status'

python3 - <<'PY'
import base64, json, urllib.request
data = open("firmware-v3.bin","rb").read()
for off in range(0, len(data), 4096):
    req = urllib.request.Request(
        "http://localhost:8000/devices/dev-001/chunks",
        data=json.dumps({"offset": off,
            "data_base64": base64.b64encode(data[off:off+4096]).decode()}).encode(),
        method="PUT", headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)
print("uploaded")
PY

# 试启 v3（槽 0），拿到令牌但“不确认”
RESP=$(curl -s -X POST http://localhost:8000/devices/dev-001/restart)
TOKEN3=$(echo "$RESP" | jq -r .boot_token); echo "$RESP" | jq

# 再次重启：自动回退到旧已确认镜像（槽 1），候选 v3 被废弃
curl -s -X POST http://localhost:8000/devices/dev-001/restart | jq
#   result: "confirmed"，running_slot: 1

# 迟到的确认 -> 409
curl -s -X POST "http://localhost:8000/devices/dev-001/confirm?token=$TOKEN3" | jq
#   409 no_trial_pending
```

候选不完整 / 摘要错误时重启，旧镜像同样直接启动（候选不会被试启）。
