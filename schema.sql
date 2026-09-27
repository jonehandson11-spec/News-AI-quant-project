-- SQLite schema for the shared news snapshot.

CREATE TABLE collection_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE news (
                article_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                publish_time TEXT NOT NULL,
                crawl_time TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                language TEXT NOT NULL
            );

CREATE INDEX news_publish_time ON news(publish_time DESC);

CREATE INDEX news_source_time ON news(source,publish_time DESC);

CREATE VIEW bbc_news AS SELECT * FROM news WHERE source='BBC News';

CREATE VIEW raw_news AS SELECT * FROM news;

CREATE VIEW sina_news AS SELECT * FROM news WHERE source='新浪财经';

CREATE VIEW source_summary AS SELECT source, count(*) AS article_count,
                min(publish_time) AS earliest_publish_time, max(publish_time) AS latest_publish_time
                FROM news GROUP BY source;
