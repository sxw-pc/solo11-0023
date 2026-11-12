-- 合成 SQL：users_b 的活跃用户口径
SELECT last_active_date AS day, COUNT(DISTINCT user_id) AS active_users
FROM users_b
WHERE status = 'active'
GROUP BY last_active_date
ORDER BY day
