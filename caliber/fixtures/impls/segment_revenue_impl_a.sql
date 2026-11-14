-- 合成 SQL：revenue_a 按区域/细分段的收入口径（明细行直接返回复合自然键 + 指标值）
SELECT region, segment, revenue
FROM revenue_a
ORDER BY region, segment
