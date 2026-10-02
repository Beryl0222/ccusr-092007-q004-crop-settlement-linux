"""服务层抛出的领域异常。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域异常的基类。"""


class NotFound(DomainError):
    """记录不存在。"""


class AccessDenied(DomainError):
    """调用方无权查看或操作该记录。"""


class InvalidState(DomainError):
    """状态机不允许的迁移。"""


class OverReceiveError(DomainError):
    """接收数量超出批次实收数量。"""


class QuantityExceeded(DomainError):
    """接收数量超出承诺总量或可用余量。"""


class OutsideDeliveryWindow(DomainError):
    """交付时间不在承诺的交付窗口内。"""


class ConsentRequired(DomainError):
    """条款变更必须取得农户明确同意。"""


class EvidenceRequired(DomainError):
    """协商定案前必须先提交证据。"""
