-- 合成 SQL：orders_a 的按日客单价
SELECT order_date AS day, ROUND(AVG(amount), 2) AS avg_order_value
FROM orders_a
GROUP BY order_date
ORDER BY day
