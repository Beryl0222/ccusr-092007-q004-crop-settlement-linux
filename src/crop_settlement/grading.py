"""浮动分级：按实验室指标定级、计算扣重与保底单价。

价格公式（与 README 一致）：
    基准价 = 市场参考价（若接收时提供）否则保底价
    单价   = max(保底价, 基准价 + 等级浮动)
即保底价是下限；没有市场参考价时，向下浮动不生效，农户至少拿保底价。
"""

from __future__ import annotations

from decimal import Decimal

from .domain import (
    Deduction,
    DeductionReason,
    GradeBand,
    GradingRule,
    QualityMetrics,
    kg,
    yuan,
)


def select_band(metrics: QualityMetrics, rule: GradingRule) -> GradeBand:
    """按指标从高到低匹配等级带；都不满足时落入最后一个（等外）。"""
    for band in rule.bands:
        if (
            metrics.moisture_pct <= band.max_moisture_pct
            and metrics.impurity_pct <= band.max_impurity_pct
        ):
            return band
    return rule.bands[-1]


def weight_deductions(
    metrics: QualityMetrics, rule: GradingRule, quantity_kg: Decimal
) -> tuple[Deduction, ...]:
    """水分、杂质超过阈值时按比例扣重，返回带理由的扣款明细。"""
    deductions: list[Deduction] = []
    moisture_over = metrics.moisture_pct - rule.moisture_threshold_pct
    if moisture_over > 0:
        pct = moisture_over * rule.moisture_deduction_pct_per_point
        deducted = kg(quantity_kg * pct / Decimal("100"))
        if deducted > 0:
            deductions.append(
                Deduction(
                    reason=DeductionReason.MOISTURE_OVER,
                    label=(
                        f"水分 {metrics.moisture_pct}% 超阈值 "
                        f"{rule.moisture_threshold_pct}%，扣重 {pct}%"
                    ),
                    weight_kg=deducted,
                )
            )
    impurity_over = metrics.impurity_pct - rule.impurity_threshold_pct
    if impurity_over > 0:
        pct = impurity_over * rule.impurity_deduction_pct_per_point
        deducted = kg(quantity_kg * pct / Decimal("100"))
        if deducted > 0:
            deductions.append(
                Deduction(
                    reason=DeductionReason.IMPURITY_OVER,
                    label=(
                        f"杂质 {metrics.impurity_pct}% 超阈值 "
                        f"{rule.impurity_threshold_pct}%，扣重 {pct}%"
                    ),
                    weight_kg=deducted,
                )
            )
    return tuple(deductions)


def unit_price(
    floor_price_per_kg: Decimal,
    band: GradeBand,
    market_reference_per_kg: Decimal | None = None,
) -> Decimal:
    """保底价托底的浮动单价，见模块 docstring 的公式。"""
    base = market_reference_per_kg if market_reference_per_kg is not None else floor_price_per_kg
    price = base + band.price_delta_per_kg
    return yuan(max(price, floor_price_per_kg, Decimal("0")))
