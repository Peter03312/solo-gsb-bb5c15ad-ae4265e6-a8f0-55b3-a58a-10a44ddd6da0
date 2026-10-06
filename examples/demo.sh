#!/usr/bin/env bash
# 端到端调用示例：覆盖证书登记、幂等、区间冲突、测量校验、撤销与实时有效性，
# 以及测量记录的追加式更正（版本链、乐观并发控制、更正后有效性实时重算）。
# 用法: BASE=http://localhost:8000 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API="$BASE/api/v1"

if command -v jq >/dev/null 2>&1; then PPRINT="jq ."; else PPRINT="cat"; fi

post() { # post <path> <json-body> [extra curl args, e.g. -H 'Idempotency-Key: ...']
  local path="$1" body="$2"; shift 2
  echo "==> POST $path"
  curl -sS -X POST "$API$path" -H 'Content-Type: application/json' "$@" -d "$body" | eval "$PPRINT"
  echo
}

get() { # get <path>
  echo "==> GET $1"
  curl -sS "$API$1" | eval "$PPRINT"
  echo
}

json_val() { # json_val <field> —— 从 stdin 的 JSON 中提取顶层数字字段（有 jq 用 jq）
  local f="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r ".$f"
  else
    tr -d '{}' | tr ',' '\n' | sed -n "s/^\"$f\":\([0-9]\+\).*/\1/p" | head -1
  fi
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

echo "### 1. 登记证书（201）"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-0001'

echo "### 2. 相同幂等键 + 相同内容重试 → 200 返回首次结果（响应头含 Idempotency-Replayed: true）"
curl -sS -i -X POST "$API/certificates" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: reg-eq001-0001' \
  -d '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' | head -20
echo

echo "### 3. 相同幂等键 + 不同内容 → 409 IDEMPOTENCY_KEY_CONFLICT"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2026-0001","valid_from":"2026-02-01T00:00:00+08:00","valid_to":"2027-02-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-0001'

echo "### 4. 同设备有效期重叠 → 409 VALIDITY_OVERLAP（并发提交同样只有一份成功）"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2026-0002","valid_from":"2026-06-01T00:00:00+08:00","valid_to":"2027-06-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-0002'

echo "### 5. 时刻缺时区 → 422；起点不早于终点 → 422"
post /certificates \
  '{"device_id":"EQ-002","certificate_no":"CERT-X","valid_from":"2026-01-01T00:00:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-bad-1'
post /certificates \
  '{"device_id":"EQ-002","certificate_no":"CERT-X","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2026-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-bad-2'

echo "### 6. 紧邻上一段的新证书（左闭右开，端点相接不算重叠）→ 201"
post /certificates \
  '{"device_id":"EQ-001","certificate_no":"CERT-2027-0001","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: reg-eq001-2027'

echo "### 7. 写入测量记录：落在有效期内 → 201"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-0001"}'

echo "### 8. 边界：measured_at == valid_from（左闭）→ 201；== valid_to（右开）→ 422"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-01-01T00:00:00+08:00","certificate_no":"CERT-2026-0001"}'
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2027-01-01T00:00:00+08:00","certificate_no":"CERT-2026-0001"}'

echo "### 9. 证书不存在 → 422"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"NO-SUCH-CERT"}'

echo "### 10. 撤销证书（历史测量保留，不删除）"
post /certificates/1/revoke '{"reason":"calibration lab found drift, cert voided"}'

echo "### 11. 撤销后写入新测量 → 422 CERTIFICATE_REVOKED"
post /measurements \
  '{"device_id":"EQ-001","measured_at":"2026-07-01T09:00:00+08:00","certificate_no":"CERT-2026-0001"}'

echo "### 12. 查询测量记录：实时给出当前有效性及原因（历史记录变为 invalid）"
get "/measurements?device_id=EQ-001"

echo "### 13. 重复撤销 → 409 CERTIFICATE_ALREADY_REVOKED"
post /certificates/1/revoke '{"reason":"again"}'

# ---------------------------------------------------------------------------
# 测量记录更正
# ---------------------------------------------------------------------------

echo "### 14. 为更正演示准备：EQ-002 的两段相邻证书 + 一张已撤销证书 + 一条测量记录"
CERT26_ID=$(curl -sS -X POST "$API/certificates" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: reg-eq002-2026' \
  -d '{"device_id":"EQ-002","certificate_no":"CERT-2026-1001","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' | json_val id)
CERT27_ID=$(curl -sS -X POST "$API/certificates" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: reg-eq002-2027' \
  -d '{"device_id":"EQ-002","certificate_no":"CERT-2027-1001","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' | json_val id)
CERT_VOID_ID=$(curl -sS -X POST "$API/certificates" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: reg-eq002-void' \
  -d '{"device_id":"EQ-002","certificate_no":"CERT-2028-VOID","valid_from":"2028-01-01T00:00:00+08:00","valid_to":"2029-01-01T00:00:00+08:00"}' | json_val id)
curl -sS -X POST "$API/certificates/$CERT_VOID_ID/revoke" -H 'Content-Type: application/json' \
  -d '{"reason":"demo: cert voided before use"}' > /dev/null
M_ID=$(curl -sS -X POST "$API/measurements" -H 'Content-Type: application/json' \
  -d '{"device_id":"EQ-002","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"CERT-2026-1001"}' | json_val id)
echo "EQ-002: CERT-2026-1001 id=$CERT26_ID, CERT-2027-1001 id=$CERT27_ID, CERT-2028-VOID id=$CERT_VOID_ID (已撤销)"
echo "measurement id=$M_ID（当前 version=1）"; echo

echo "### 15. 更正测量时刻（base_version=1 → version=2，设备编号保持原值）"
post "/measurements/$M_ID/corrections" \
  '{"base_version":1,"measured_at":"2026-06-16T09:00:00+08:00","certificate_no":"CERT-2026-1001","reason":"测量时刻填错，实际为 2026-06-16 09:00"}'

echo "### 16. 版本不符（仍用过期的 base_version=1）→ 409 VERSION_CONFLICT"
post "/measurements/$M_ID/corrections" \
  '{"base_version":1,"measured_at":"2026-06-17T09:00:00+08:00","certificate_no":"CERT-2026-1001","reason":"基于过期版本号"}'

echo "### 17. 更正所用证书（base_version=2 → version=3，改用 2027 年证书）"
post "/measurements/$M_ID/corrections" \
  '{"base_version":2,"measured_at":"2027-03-01T09:00:00+08:00","certificate_no":"CERT-2027-1001","reason":"证书编号填错，实际使用 CERT-2027-1001"}'

echo "### 18. 同一版本（base_version=3）的并发更正 → 最多成功一次，其余 409"
curl -sS -X POST "$API/measurements/$M_ID/corrections" -H 'Content-Type: application/json' \
  -d '{"base_version":3,"measured_at":"2027-03-02T09:00:00+08:00","certificate_no":"CERT-2027-1001","reason":"并发更正 A"}' \
  -o /tmp/corr-a.json -w 'A -> HTTP %{http_code}\n' &
curl -sS -X POST "$API/measurements/$M_ID/corrections" -H 'Content-Type: application/json' \
  -d '{"base_version":3,"measured_at":"2027-03-03T09:00:00+08:00","certificate_no":"CERT-2027-1001","reason":"并发更正 B"}' \
  -o /tmp/corr-b.json -w 'B -> HTTP %{http_code}\n' &
wait
eval "$PPRINT" < /tmp/corr-a.json; echo
eval "$PPRINT" < /tmp/corr-b.json; echo

CUR_V=$(curl -sS "$API/measurements/$M_ID" | json_val version)
echo "当前版本号：$CUR_V"; echo

echo "### 19. 更正时选择已撤销证书 → 422 CERTIFICATE_REVOKED"
post "/measurements/$M_ID/corrections" \
  "{\"base_version\":$CUR_V,\"measured_at\":\"2028-06-01T09:00:00+08:00\",\"certificate_no\":\"CERT-2028-VOID\",\"reason\":\"尝试改用已撤销证书\"}"

echo "### 20. 新时刻不被所选证书覆盖 → 422 MEASUREMENT_TIME_NOT_COVERED"
post "/measurements/$M_ID/corrections" \
  "{\"base_version\":$CUR_V,\"measured_at\":\"2028-06-01T09:00:00+08:00\",\"certificate_no\":\"CERT-2026-1001\",\"reason\":\"时刻超出证书有效期\"}"

echo "### 21. 完整版本链：原始版本（version=1）+ 按序历次更正 + 当前版本号"
get "/measurements/$M_ID/versions"

echo "### 22. 现有详情/列表查询返回当前版本值（含 version 字段）"
get "/measurements/$M_ID"

echo "### 23. 撤销当前版本所用证书 → 当前版本有效性实时变为无效，旧版本历史继续可查"
post "/certificates/$CERT27_ID/revoke" '{"reason":"2027 cert voided after audit"}'
get "/measurements/$M_ID"
get "/measurements/$M_ID/versions"
