-- 合成 SQL：revenue_b 同口径实现；fixture 故意制造三类差异（单侧缺失/类型不一致/数值偏离）
SELECT region, segment, revenue
FROM revenue_b
ORDER BY region, segment
