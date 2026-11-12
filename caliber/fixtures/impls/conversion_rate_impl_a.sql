-- 合成 SQL：orders_a 的按日支付转化率
SELECT order_date AS day,
       ROUND(100.0 * SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) / COUNT(*), 6) AS rate
FROM orders_a
GROUP BY order_date
ORDER BY day
