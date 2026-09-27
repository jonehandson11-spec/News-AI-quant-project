-- 最新新闻；含正文。时间均为带 +08:00 偏移的 ISO 8601 文本。
SELECT title, publish_time, source, url, content
FROM news
ORDER BY julianday(publish_time) DESC, url;

-- 来源数量及实际最早/最晚发布时间
SELECT * FROM source_summary;

-- 关键词查询：把下面的“经济”改成需要的词
SELECT title, publish_time, source, url
FROM news
WHERE title LIKE '%经济%' OR content LIKE '%经济%'
ORDER BY julianday(publish_time) DESC, url;

-- 按北京时间日历日统计
SELECT substr(publish_time, 1, 10) AS publish_date, source, count(*) AS articles
FROM news
GROUP BY publish_date, source
ORDER BY publish_date, source;

-- 采集窗口、来源覆盖和计数等元数据
SELECT key, value FROM collection_info ORDER BY key;

-- 数据库结构和完整性
SELECT type, name, sql
FROM sqlite_master
WHERE name NOT LIKE 'sqlite_%'
ORDER BY type, name;
PRAGMA integrity_check;

