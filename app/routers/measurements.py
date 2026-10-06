"""测量记录写入 / 查询（含实时有效性判定）与追加式更正。"""

import asyncpg
from fastapi import APIRouter, Depends, Query

from ..db import get_pool
from ..errors import ApiError
from ..schemas import (
    CorrectionCreate,
    MeasurementCreate,
    MeasurementHistoryOut,
    MeasurementOut,
    MeasurementVersionOut,
    Validity,
)

router = APIRouter()

# 关联"当前版本"（原始记录 + 最新一次更正）与证书当前状态，
# 有效性在查询时实时计算（撤销立即反映到历史记录）
_CURRENT_SQL = """
SELECT m.id, m.device_id, m.created_at,
       COALESCE(mc.version, 1)                       AS version,
       COALESCE(mc.measured_at, m.measured_at)       AS measured_at,
       COALESCE(mc.certificate_id, m.certificate_id) AS certificate_id,
       c.certificate_no, c.revoked_at, c.revoke_reason
FROM measurements m
LEFT JOIN LATERAL (
    SELECT version, measured_at, certificate_id
    FROM measurement_corrections
    WHERE measurement_id = m.id
    ORDER BY version DESC
    LIMIT 1
) mc ON TRUE
JOIN certificates c ON c.id = COALESCE(mc.certificate_id, m.certificate_id)
"""

# 完整版本链：原始版本（version=1）+ 历次更正（version>=2），按版本号升序
_HISTORY_SQL = """
SELECT 1 AS version, m.device_id, m.measured_at, m.certificate_id,
       NULL::text AS reason, m.created_at,
       c.certificate_no, c.revoked_at, c.revoke_reason
FROM measurements m
JOIN certificates c ON c.id = m.certificate_id
WHERE m.id = $1
UNION ALL
SELECT mc.version, m.device_id, mc.measured_at, mc.certificate_id,
       mc.reason, mc.created_at,
       c.certificate_no, c.revoked_at, c.revoke_reason
FROM measurement_corrections mc
JOIN measurements m ON m.id = mc.measurement_id
JOIN certificates c ON c.id = mc.certificate_id
WHERE mc.measurement_id = $1
ORDER BY 1
"""


def _validity(revoked_at, revoke_reason) -> Validity:
    if revoked_at is not None:
        reason = f"certificate revoked at {revoked_at.isoformat()}"
        if revoke_reason:
            reason += f": {revoke_reason}"
        return Validity(is_valid=False, reason=reason)
    return Validity(
        is_valid=True,
        reason="certificate is active and covers the measurement time",
    )


def _to_out(row) -> MeasurementOut:
    return MeasurementOut(
        id=row["id"],
        device_id=row["device_id"],
        measured_at=row["measured_at"],
        certificate_no=row["certificate_no"],
        certificate_id=row["certificate_id"],
        created_at=row["created_at"],
        validity=_validity(row["revoked_at"], row["revoke_reason"]),
        version=row["version"],
    )


def _check_cert_usable(cert, measured_at) -> None:
    """证书须未撤销且覆盖测量时刻（左闭右开），否则 422。"""
    if cert["revoked_at"] is not None:
        raise ApiError(
            422,
            "CERTIFICATE_REVOKED",
            f"certificate '{cert['certificate_no']}' was revoked at "
            f"{cert['revoked_at'].isoformat()}: {cert['revoke_reason'] or ''}".rstrip(": "),
        )
    if not (cert["valid_from"] <= measured_at < cert["valid_to"]):
        raise ApiError(
            422,
            "MEASUREMENT_TIME_NOT_COVERED",
            f"certificate covers [{cert['valid_from'].isoformat()}, "
            f"{cert['valid_to'].isoformat()}) but measurement time is "
            f"{measured_at.isoformat()}",
        )


@router.post("", response_model=MeasurementOut, status_code=201)
async def create_measurement(
    payload: MeasurementCreate,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementOut:
    """写入测量记录。

    仅当指定证书存在、覆盖测量时刻（左闭右开）且未撤销时接受；
    记录一经写入不可修改（数据库触发器强制），后续只能通过追加更正修正。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 行锁防止证书在校验与写入之间被并发撤销
            cert = await conn.fetchrow(
                "SELECT * FROM certificates WHERE device_id = $1 AND certificate_no = $2 "
                "FOR UPDATE",
                payload.device_id,
                payload.certificate_no,
            )
            if cert is None:
                raise ApiError(
                    422,
                    "CERTIFICATE_NOT_FOUND",
                    f"no certificate '{payload.certificate_no}' registered "
                    f"for device '{payload.device_id}'",
                )
            _check_cert_usable(cert, payload.measured_at)
            row = await conn.fetchrow(
                "INSERT INTO measurements (device_id, measured_at, certificate_id) "
                "VALUES ($1, $2, $3) RETURNING *",
                payload.device_id,
                payload.measured_at,
                cert["id"],
            )
    return MeasurementOut(
        id=row["id"],
        device_id=row["device_id"],
        measured_at=row["measured_at"],
        certificate_no=cert["certificate_no"],
        certificate_id=row["certificate_id"],
        created_at=row["created_at"],
        validity=_validity(cert["revoked_at"], cert["revoke_reason"]),
        version=1,
    )


@router.get("", response_model=list[MeasurementOut])
async def list_measurements(
    device_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[MeasurementOut]:
    """列表：返回每条记录的当前版本值与实时有效性。"""
    rows = await pool.fetch(
        _CURRENT_SQL
        + " WHERE ($1::text IS NULL OR m.device_id = $1) ORDER BY m.id LIMIT $2 OFFSET $3",
        device_id,
        limit,
        offset,
    )
    return [_to_out(r) for r in rows]


@router.get("/{measurement_id}", response_model=MeasurementOut)
async def get_measurement(
    measurement_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementOut:
    """详情：返回当前版本值、版本号与实时有效性。"""
    row = await pool.fetchrow(_CURRENT_SQL + " WHERE m.id = $1", measurement_id)
    if row is None:
        raise ApiError(404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found")
    return _to_out(row)


@router.post(
    "/{measurement_id}/corrections",
    response_model=MeasurementVersionOut,
    status_code=201,
)
async def create_correction(
    measurement_id: int,
    payload: CorrectionCreate,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementVersionOut:
    """提交更正：修正测量时刻和/或所用证书，必须给出原因。

    - 追加式：原版本与历次更正全部保留，任何版本不可修改/删除（触发器强制）；
    - 乐观版本控制：`base_version` 须等于当前最新版本，否则 409；
      同一版本的并发更正最多成功一次；
    - 设备编号保持原值；新证书须属于该设备、覆盖新时刻且提交时未撤销
      （校验与写入在同一事务内，并对证书行加锁，防止并发撤销造成无效更正）。
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 锁测量行：串行化同一记录的并发更正
            measurement = await conn.fetchrow(
                "SELECT * FROM measurements WHERE id = $1 FOR UPDATE", measurement_id
            )
            if measurement is None:
                raise ApiError(
                    404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found"
                )
            latest = await conn.fetchval(
                "SELECT max(version) FROM measurement_corrections WHERE measurement_id = $1",
                measurement_id,
            )
            current_version = latest if latest is not None else 1
            if payload.base_version != current_version:
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"measurement id={measurement_id} is at version {current_version}, "
                    f"but the correction bases on version {payload.base_version}; "
                    f"reload the latest version and retry",
                )
            # 设备编号保持原值；行锁防止证书在校验与写入之间被并发撤销
            cert = await conn.fetchrow(
                "SELECT * FROM certificates WHERE device_id = $1 AND certificate_no = $2 "
                "FOR UPDATE",
                measurement["device_id"],
                payload.certificate_no,
            )
            if cert is None:
                raise ApiError(
                    422,
                    "CERTIFICATE_NOT_FOUND",
                    f"no certificate '{payload.certificate_no}' registered "
                    f"for device '{measurement['device_id']}'",
                )
            _check_cert_usable(cert, payload.measured_at)
            try:
                row = await conn.fetchrow(
                    "INSERT INTO measurement_corrections "
                    "(measurement_id, version, base_version, measured_at, certificate_id, reason) "
                    "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                    measurement_id,
                    current_version + 1,
                    payload.base_version,
                    payload.measured_at,
                    cert["id"],
                    payload.reason,
                )
            except asyncpg.UniqueViolationError:
                # 兜底：唯一约束 (measurement_id, version) 保证同版本并发更正只有一份成功
                raise ApiError(
                    409,
                    "VERSION_CONFLICT",
                    f"measurement id={measurement_id} was concurrently corrected; "
                    f"reload the latest version and retry",
                )
    return MeasurementVersionOut(
        version=row["version"],
        device_id=measurement["device_id"],
        measured_at=row["measured_at"],
        certificate_no=cert["certificate_no"],
        certificate_id=row["certificate_id"],
        reason=row["reason"],
        created_at=row["created_at"],
        validity=_validity(cert["revoked_at"], cert["revoke_reason"]),
    )


@router.get("/{measurement_id}/versions", response_model=MeasurementHistoryOut)
async def get_measurement_history(
    measurement_id: int,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementHistoryOut:
    """完整版本链：原始版本 + 按序排列的历次更正 + 当前版本号。

    每个版本的有效性按其证书当前撤销状态实时计算；旧版本历史继续可查。
    """
    rows = await pool.fetch(_HISTORY_SQL, measurement_id)
    if not rows:
        raise ApiError(404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found")
    versions = [
        MeasurementVersionOut(
            version=r["version"],
            device_id=r["device_id"],
            measured_at=r["measured_at"],
            certificate_no=r["certificate_no"],
            certificate_id=r["certificate_id"],
            reason=r["reason"],
            created_at=r["created_at"],
            validity=_validity(r["revoked_at"], r["revoke_reason"]),
        )
        for r in rows
    ]
    return MeasurementHistoryOut(
        measurement_id=measurement_id,
        device_id=rows[0]["device_id"],
        current_version=rows[-1]["version"],
        versions=versions,
    )
