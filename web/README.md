# 新闻 × 港股价格查询网页

此目录是团队仓库的 GitHub Pages 前端。公开网页地址：

`https://jonehandson11-spec.github.io/News-AI-quant-project/`

网页按香港时间查询一条新闻前 1／3／6 个月及后 1／3／12／24 小时、3 天、1 周的价格点。取得行情后，网页自动计算差价和涨跌幅，并保留目标时间、实际取价时间、休市顺延和价格来源。新闻候选由仓库 `data/news.csv` 构建；候选仅按公司名称直接提及筛选，仍需人工复核。

发布时间可点选“1 小时前”“6 小时前”等快捷选项，以点击时刻换算香港时间；也可手动输入或选择候选新闻。

## 行情来源

计算差价需要价格数据，但不一定要上传 CSV。网页目前不会自行抓取或伪造行情；公开仓库也没有已获准再分发的港股分钟行情，因此初始会显示“尚未发布共享行情”。CSV 是当前的临时数据入口：每个成员可以点击“上传价格 CSV”，选择自己有权使用的本地文件。文件保存在该浏览器的 IndexedDB，后续访问自动载入；浏览器清理站点数据后需重新选择。文件不上传到 GitHub 或其他服务器，也不会共享给其他成员。页面提供清除本机保存文件的按钮。

如团队取得**允许公开再分发**的行情 CSV，可在仓库添加 `data/public_prices.csv`。Pages 构建会先校验，再把它复制到站点，所有访问者随后可直接查询。上传前请确认数据许可、日期覆盖和价格口径。不要把账号、Cookie、API 密钥或未获准公开的数据放入该文件。

CSV 至少有四列：

```csv
ticker,interval,timestamp,close
1211.HK,day,2026-08-28,90.0
1211.HK,minute,2026-09-28T09:59:00+08:00,100.0
```

上面两行是**格式示意，不是历史价格**。`interval` 为 `day` 或 `minute`。日线可用 `YYYY-MM-DD`，分钟线必须有时区。可选 `basis` 列；省略时标为 `raw`。要完整查询，文件需要覆盖新闻前半年日线、新闻发布前的分钟基准价，以及发布后一周的分钟线。用户上传的 CSV 格式相同。

## 部署

仓库拥有者在 **Settings → Pages → Build and deployment** 将 Source 设为 **GitHub Actions**。合并 `.github/workflows/price-query-pages.yml` 后，手动运行一次该工作流；其后新闻 CSV、网页代码或授权公开价格文件更新时自动重新发布。工作流只上传构建出的 `_site`，其中新闻索引只含标题、发布时间、来源、链接和匹配标记，不包含文章正文。

本地预览：

```sh
python web/build_site.py --news data/news.csv --output _site
python -m http.server 8765 --directory _site
```

然后访问 `http://127.0.0.1:8765/`。测试：

```sh
python -m unittest discover -s price_query_tests -v
node --test web/core.test.mjs
```

原有命令行接口见仓库 `docs/news-price-query.md`，可用于批量导出和周会分析。网页和 CLI 的输入价格都使用未复权收盘价；它们展示描述性相关变化，不证明新闻导致了涨跌，也不是交易建议。
