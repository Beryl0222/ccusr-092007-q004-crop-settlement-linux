"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则错误的基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(DomainError):
    """输入不满足业务约束。"""

    code = "validation_error"
    http_status = 422


class NotFoundError(DomainError):
    """实体不存在。"""

    code = "not_found"
    http_status = 404


class PermissionDenied(DomainError):
    """当前身份无权访问该采购关系或执行该操作。"""

    code = "forbidden"
    http_status = 403


class ConflictError(DomainError):
    """并发冲突：版本号过期、数量被突破等。"""

    code = "conflict"
    http_status = 409


class QuantityOverflow(ConflictError):
    """登记/验收数量会超过物理实收或商业上限。"""

    code = "quantity_overflow"
