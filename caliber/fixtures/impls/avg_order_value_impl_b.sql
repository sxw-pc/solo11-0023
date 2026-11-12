-- 合成 SQL：orders_b 的按日客单价（金额与 A 不同）
SELECT order_date AS day, ROUND(AVG(amount), 2) AS avg_order_value
FROM orders_b
GROUP BY order_date
ORDER BY day
