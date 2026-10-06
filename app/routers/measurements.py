"""测量记录写入 / 查询（含实时有效性判定）。"""

import asyncpg
from fastapi import APIRouter, Depends, Query

from ..db import get_pool
from ..errors import ApiError
from ..schemas import MeasurementCreate, MeasurementOut, Validity

router = APIRouter()

# 关联证书当前状态，有效性在查询时实时计算（撤销立即反映到历史记录）
_JOIN_SQL = """
SELECT m.id, m.device_id, m.measured_at, m.certificate_id, m.created_at,
       c.certificate_no, c.revoked_at, c.revoke_reason
FROM measurements m
JOIN certificates c ON c.id = m.certificate_id
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
    )


@router.post("", response_model=MeasurementOut, status_code=201)
async def create_measurement(
    payload: MeasurementCreate,
    pool: asyncpg.Pool = Depends(get_pool),
) -> MeasurementOut:
    """写入测量记录。

    仅当指定证书存在、覆盖测量时刻（左闭右开）且未撤销时接受；
    记录一经写入不可修改（数据库触发器强制）。
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
            if cert["revoked_at"] is not None:
                raise ApiError(
                    422,
                    "CERTIFICATE_REVOKED",
                    f"certificate '{payload.certificate_no}' was revoked at "
                    f"{cert['revoked_at'].isoformat()}: {cert['revoke_reason'] or ''}".rstrip(": "),
                )
            if not (cert["valid_from"] <= payload.measured_at < cert["valid_to"]):
                raise ApiError(
                    422,
                    "MEASUREMENT_TIME_NOT_COVERED",
                    f"certificate covers [{cert['valid_from'].isoformat()}, "
                    f"{cert['valid_to'].isoformat()}) but measurement time is "
                    f"{payload.measured_at.isoformat()}",
                )
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
    )


@router.get("", response_model=list[MeasurementOut])
async def list_measurements(
    device_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    pool: asyncpg.Pool = Depends(get_pool),
) -> list[MeasurementOut]:
    rows = await pool.fetch(
        _JOIN_SQL
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
    row = await pool.fetchrow(_JOIN_SQL + " WHERE m.id = $1", measurement_id)
    if row is None:
        raise ApiError(404, "MEASUREMENT_NOT_FOUND", f"measurement id={measurement_id} not found")
    return _to_out(row)
