# 实验室设备校准追溯服务

基于 **FastAPI + PostgreSQL** 的校准证书登记与测量记录追溯服务。

- 登记设备校准证书：编号、有效起止时刻（**必须带时区**，起点 < 终点，区间**左闭右开** `[valid_from, valid_to)`）
- 同一设备证书有效期**不得重叠**：由数据库 exclusion 约束保证，**并发提交只有一份成功**，其余返回 409
- 测量记录：仅当证书**覆盖测量时刻且未撤销**时才接受写入；记录**一经写入不可修改**（数据库触发器强制）
- 测量记录支持**追加式更正**：测量时刻或所用证书填错时可提交带原因的更正；原测量与历次更正全部保留，任何版本不得修改或删除
- 更正采用**乐观版本控制**：须携带当前版本号，只能从最新版本继续；版本不符或同版本并发更正 → 409，**同一版本最多成功一次**
- 更正后的设备编号保持原值；新证书必须属于该设备、覆盖新时刻且提交时未撤销（校验与写入同事务 + 行锁，杜绝并发撤销造成无效更正）
- 版本链查询：一次取得原始版本、按序排列的更正链和当前版本；当前版本有效性按证书撤销状态**实时**重算，旧版历史继续可查
- 撤销证书**不删除**历史测量；查询时**实时**返回每条记录的当前有效性及原因
- 证书登记支持**幂等键**：相同键 + 相同内容重试返回首次结果；相同键 + 不同内容返回 409

## 一键启动

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
|---|---|
| HTTP API | http://localhost:8000 |
| 交互式文档 (Swagger UI) | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/密码 `postgres`/`postgres`，库名 `calibration`） |

数据库首次启动时自动执行 `db/init.sql` 完成建表初始化；API 容器等待数据库健康检查后启动，自身也带连接重试。

停止并清空数据：`docker compose down -v`

## 配置

通过环境变量覆盖（见 `app/config.py`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/calibration` | 数据库连接串 |
| `DB_POOL_MIN_SIZE` / `DB_POOL_MAX_SIZE` | `1` / `10` | 连接池大小 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库的重试次数（每秒一次） |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/calibration
psql "$DATABASE_URL" -f db/init.sql   # 初始化建表
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

统一错误格式：`{"error": {"code": "...", "message": "..."}}`

### 证书

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/certificates` | 登记证书（**要求 `Idempotency-Key` 请求头**），成功 201；幂等重放 200（响应头 `Idempotency-Replayed: true`） |
| `GET` | `/api/v1/certificates?device_id=&status=&limit=&offset=` | 列表（`status` 取 `active`/`revoked`） |
| `GET` | `/api/v1/certificates/{id}` | 详情 |
| `POST` | `/api/v1/certificates/{id}/revoke` | 撤销，请求体 `{"reason": "..."}` |

登记请求体：

```json
{
  "device_id": "EQ-001",
  "certificate_no": "CERT-2026-0001",
  "valid_from": "2026-01-01T00:00:00+08:00",
  "valid_to": "2027-01-01T00:00:00+08:00"
}
```

典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 422 | （校验错误） | 时刻缺时区、`valid_from >= valid_to` |
| 409 | `VALIDITY_OVERLAP` | 同设备有效期重叠（含并发冲突） |
| 409 | `DUPLICATE_CERTIFICATE_NO` | 同设备证书编号重复 |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | 同一幂等键提交了不同内容 |
| 409 | `CERTIFICATE_ALREADY_REVOKED` | 重复撤销 |

### 测量记录

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/measurements` | 写入测量记录，成功 201 |
| `GET` | `/api/v1/measurements?device_id=&limit=&offset=` | 列表，返回**当前版本**值与实时有效性 |
| `GET` | `/api/v1/measurements/{id}` | 详情，返回**当前版本**值、版本号与实时有效性 |
| `POST` | `/api/v1/measurements/{id}/corrections` | 提交更正（须带 `base_version` 和原因），成功 201 |
| `GET` | `/api/v1/measurements/{id}/versions` | 完整版本链：原始版本 + 按序历次更正 + 当前版本号 |

写入请求体：

```json
{
  "device_id": "EQ-001",
  "measured_at": "2026-06-15T10:30:00+08:00",
  "certificate_no": "CERT-2026-0001"
}
```

仅当 `valid_from <= measured_at < valid_to` 且证书未撤销时接受，否则 422
（`CERTIFICATE_NOT_FOUND` / `CERTIFICATE_REVOKED` / `MEASUREMENT_TIME_NOT_COVERED`）。

响应中的 `validity` 按**当前**证书状态实时计算——证书撤销后，历史记录立即变为无效并给出原因；
`version` 为当前版本号（`1` = 原始记录，每次更正 +1）：

```json
{
  "id": 1,
  "device_id": "EQ-001",
  "measured_at": "2026-06-15T02:30:00+00:00",
  "certificate_no": "CERT-2026-0001",
  "certificate_id": 1,
  "created_at": "2026-10-05T12:00:00+00:00",
  "validity": {
    "is_valid": false,
    "reason": "certificate revoked at 2026-10-05T13:00:00+00:00: calibration lab found drift"
  },
  "version": 1
}
```

#### 更正测量记录

测量时刻或所用证书填错时，对指定记录提交**带原因的更正**（设备编号保持原值，无需也不能修改）：

```json
{
  "base_version": 1,
  "measured_at": "2026-06-16T09:00:00+08:00",
  "certificate_no": "CERT-2026-0001",
  "reason": "测量时刻填错，实际为 2026-06-16 09:00"
}
```

- `base_version` 必须等于**当前最新版本号**（可由详情/列表/版本链查询获得）；原始记录版本号为 `1`，每次更正生成 `base_version + 1` 的新版本
- 原测量与历次更正**只能追加保存**，任何版本不得修改或删除（数据库触发器强制）
- 新时刻必须带时区；所选证书必须**属于该设备、覆盖新时刻且提交时未撤销**，校验与写入在同一事务内并对证书行加锁，并发撤销不会造成无效更正
- 成功返回 201 及新版本内容；典型错误：

| 状态码 | code | 场景 |
|---|---|---|
| 404 | `MEASUREMENT_NOT_FOUND` | 测量记录不存在 |
| 409 | `VERSION_CONFLICT` | `base_version` 与当前版本不符（含同版本并发更正，最多一份成功） |
| 422 | （校验错误） | 新时刻缺时区、原因为空 |
| 422 | `CERTIFICATE_NOT_FOUND` / `CERTIFICATE_REVOKED` / `MEASUREMENT_TIME_NOT_COVERED` | 证书不属于该设备 / 已撤销 / 不覆盖新时刻 |

版本链查询 `GET /api/v1/measurements/{id}/versions` 一次返回原始版本、按版本号升序的更正链和当前版本号，
每个版本的有效性按其证书**当前**撤销状态实时计算，旧版历史继续可查：

```json
{
  "measurement_id": 1,
  "device_id": "EQ-001",
  "current_version": 2,
  "versions": [
    {
      "version": 1,
      "device_id": "EQ-001",
      "measured_at": "2026-06-15T02:30:00+00:00",
      "certificate_no": "CERT-2026-0001",
      "certificate_id": 1,
      "reason": null,
      "created_at": "2026-10-05T12:00:00+00:00",
      "validity": {"is_valid": true, "reason": "certificate is active and covers the measurement time"}
    },
    {
      "version": 2,
      "device_id": "EQ-001",
      "measured_at": "2026-06-16T01:00:00+00:00",
      "certificate_no": "CERT-2026-0001",
      "certificate_id": 1,
      "reason": "测量时刻填错，实际为 2026-06-16 09:00",
      "created_at": "2026-10-06T08:30:00+00:00",
      "validity": {"is_valid": true, "reason": "certificate is active and covers the measurement time"}
    }
  ]
}
```

## 调用示例

完整端到端示例（登记 → 幂等重试 → 冲突 → 测量 → 撤销 → 有效性查询 → 更正 → 版本链）：

```bash
./examples/demo.sh
```

最小示例：

```bash
# 登记证书
curl -X POST http://localhost:8000/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: reg-eq001-0001' \
  -d '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001",
       "valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}'

# 写入测量记录
curl -X POST http://localhost:8000/api/v1/measurements \
  -H 'Content-Type: application/json' \
  -d '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00",
       "certificate_no":"CERT-2026-0001"}'

# 更正测量记录（base_version 须为当前版本号，此处原始记录为 1）
curl -X POST http://localhost:8000/api/v1/measurements/1/corrections \
  -H 'Content-Type: application/json' \
  -d '{"base_version":1,"measured_at":"2026-06-16T09:00:00+08:00",
       "certificate_no":"CERT-2026-0001",
       "reason":"测量时刻填错，实际为 2026-06-16 09:00"}'

# 查看完整版本链（原始版本 + 历次更正 + 当前版本）
curl http://localhost:8000/api/v1/measurements/1/versions

# 撤销证书
curl -X POST http://localhost:8000/api/v1/certificates/1/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason":"calibration lab found drift"}'

# 查询测量记录当前有效性
curl http://localhost:8000/api/v1/measurements?device_id=EQ-001
```

## 设计要点

- **区间语义**：有效期为 `[valid_from, valid_to)` 左闭右开，用 PostgreSQL `tstzrange(..., '[)')` 表达；端点相接的两段区间不算重叠。
- **并发不重叠**：`EXCLUDE USING gist (device_id WITH =, tstzrange WITH &&)` 约束在数据库层串行化并发写入，重叠提交只有一份成功，其余收到 409。
- **幂等**：`idempotency_key` 唯一约束 + 请求内容规范化哈希（时刻统一换算 UTC 后计算）；同键同内容返回首次结果，同键异内容 409。
- **测量写入一致性**：校验与写入在同一事务内，并对证书行加 `FOR UPDATE` 锁，防止校验后、写入前证书被并发撤销。
- **不可篡改**：测量表与更正表都挂 `BEFORE UPDATE OR DELETE` 触发器直接报错，应用层也不提供修改/删除接口；原测量与历次更正只能追加保存。
- **追加式更正**：原始测量记录视为 `version=1`，每次更正向 `measurement_corrections` 追加一行（`version` 递增）；更正请求不携带设备编号，设备编号始终取原记录值。
- **乐观版本控制**：更正事务先 `FOR UPDATE` 锁测量行串行化并发，再校验 `base_version` 等于当前最新版本；唯一约束 `(measurement_id, version)` 兜底——同一版本的并发更正最多一份成功，其余 409 `VERSION_CONFLICT`。
- **更正证书校验**：新证书须属于原设备、覆盖新时刻、提交时未撤销；校验与写入同事务且证书行 `FOR UPDATE`，并发撤销要么先提交（更正收到 422）、要么等更正提交后再生效，不会产生无效更正。
- **撤销即生效**：测量查询实时 JOIN 证书状态，撤销立即反映到全部历史记录（含当前版本与版本链中每个版本）的有效性上，历史数据本身保留不动。

## 目录结构

```
├── app/
│   ├── main.py            # FastAPI 入口、生命周期、路由挂载
│   ├── config.py          # 环境变量配置
│   ├── db.py              # asyncpg 连接池
│   ├── errors.py          # 统一错误格式
│   ├── schemas.py         # 请求/响应模型（AwareDatetime 强制时区）
│   └── routers/
│       ├── certificates.py
│       └── measurements.py
├── db/init.sql            # 建表、约束、不可变触发器（容器首次启动自动执行）
├── examples/demo.sh       # 端到端调用示例
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
