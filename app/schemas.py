"""请求/响应模型。

所有时刻字段使用 AwareDatetime：缺少时区信息的请求会被 422 拒绝。
"""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 证书
# ---------------------------------------------------------------------------

class CertificateCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    certificate_no: str = Field(min_length=1, max_length=128, description="证书编号")
    valid_from: AwareDatetime = Field(description="有效期起点（含，须带时区）")
    valid_to: AwareDatetime = Field(description="有效期终点（不含，须带时区）")

    @model_validator(mode="after")
    def _check_range(self) -> "CertificateCreate":
        if not self.valid_from < self.valid_to:
            raise ValueError("valid_from must be earlier than valid_to")
        return self


class CertificateOut(BaseModel):
    id: int
    device_id: str
    certificate_no: str
    valid_from: datetime
    valid_to: datetime
    revoked_at: datetime | None
    revoke_reason: str | None
    status: str = Field(description="active / revoked")
    created_at: datetime


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="撤销原因")


# ---------------------------------------------------------------------------
# 测量记录
# ---------------------------------------------------------------------------

class MeasurementCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    measured_at: AwareDatetime = Field(description="测量时刻（须带时区）")
    certificate_no: str = Field(min_length=1, max_length=128, description="证书编号")


class Validity(BaseModel):
    is_valid: bool
    reason: str


class MeasurementOut(BaseModel):
    id: int
    device_id: str
    measured_at: datetime
    certificate_no: str
    certificate_id: int
    created_at: datetime
    validity: Validity = Field(description="该记录按当前证书状态计算的实时有效性")
