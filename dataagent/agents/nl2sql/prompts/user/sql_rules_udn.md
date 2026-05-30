1. All queries MUST NOT require cross-table JOIN operations.
2. For day-over-day or week-over-week or year-over-year (DoD/WoW/YoY) comparisons, MUST extract different timestamps for the same entity from a single table and apply the formula (A - B) / B.
3. Time Handling Rules (PostgreSQL):
   - Use `CAST(EXTRACT(EPOCH FROM <timestamp>) AS BIGINT)` to convert timestamps to Unix epoch seconds.
   - For relative time ranges, use `NOW() - INTERVAL 'X days/hours'` pattern.
   - For time zone handling: use `AT TIME ZONE` to convert between time zones.
   - When querying human-readable local time ranges (e.g., "yesterday 8:00-20:00"):
     * First construct the local timestamp using `date_trunc()` and interval arithmetic.
     * Then convert to UTC using `AT TIME ZONE` before extracting epoch.
   - If no explicit end time is specified, use `NOW()` as the default end time.
   - For open-ended time ranges, MUST add `<= CAST(EXTRACT(EPOCH FROM NOW()) AS BIGINT)`.
4. Column Naming Rules:
   - Do NOT use column aliases unless required for complex expressions (e.g., calculations, aggregations)
   - Return original column names as they appear in the schema
5. Select granularity based on time range in the query:
    - MUST Use '_1d' for queries about today, within the last day, days, weeks, months, or longer periods
    - MUST Use '_1h' for queries about last 1 hour, hours
    - MUST Use '_15min' for queries requiring high resolution or short time windows (< 1 hour)
6. NWDAF (Network Data Analytics Function) related queries: focus on tables in category 'fact_dw1745159003_0000000000040000_metric_1d/1h/15min'
7. DO NOT Add ne_name filters when query does not explicitly require filtering specific INSTANCE
8. If columns such as `time`, `ne_name`, or `cell_id` exist in the table, include these columns in the SELECT clause to facilitate subsequent chart visualizations.
9. 手游类软件的业务卡顿次数 related queries: MUST use table 'dw1745159015_0000000000018194_metric_1h'
10. 终端手游的时延、抖音的上行流量 related queries: MUST use tables in category 'fact_dw1745159014_00000000000181a4_metric_1d/1h/15min'
11. 上行重载小区 related queries: MUST use tables in category 'fact_dw1745159013_'
12. sub_app_id列枚举值:taobao_live=淘宝;pinduoduo_live=拼多多;huya_live=虎牙直播;douyu_live=斗鱼直播;yy_live=YY直播;weixin_im=微信/企业微信IM;qq_im=QQIM;weixin_voip=微信/企业微信VOIP;qq_voip=QQVOIP;migu_vod=咪咕视频;tencent_vod=腾讯视频;iqiyi_vod=爱奇艺视频;mangguo_vod=芒果TV;bilibili_vod=哔哩哔哩;youku_vod=优酷视频;xigua_vod=西瓜视频;yunshixun_meeting=云视讯;dingding_meeting=钉钉;tencent_meeting=腾讯会议;feishu_meeting=飞书
13. app_id列枚举值:mobile_game=游戏;live_streaming=直播;instant_message=即时通信(消息);voip=即时通信(语音);vod_streaming=视频;meeting=办公
14. term_brand列枚举值:1=苹果;2=华为;3=小米;4=荣耀;5=OPPO;6=VIVO
15. info_indicate列枚举值:2=保障用户的EXP单据;3=体验用户的EXP单据
16. When filtering city-related fields, always convert Chinese city names into official administrative division codes (GB/T 2260) before generating SQL.Examples:海口市 -> 460100,三亚市 -> 460200,北京市 -> 110100.Use the code value in SQL instead of the city name.

# example 1
   - "query":"查询最近1周AMF网元实例"
   - "output" : "SELECT DISTINCT ne_name FROM fact_dw1745159005_0000000000040000_metric_1d WHERE time >= CAST(EXTRACT(EPOCH FROM NOW() - INTERVAL '7 days') AS BIGINT) AND time <= CAST(EXTRACT(EPOCH FROM NOW()) AS BIGINT)"
# example 2 
   - "query" : "查询最近一天北京市上行重载小区游戏大类下各类自定义分组的上行流量排序"
   - "output" : "select time, city, cell_ul_group, app_id,
                    case when sum(uplink_duration)>0
                            then sum(uplink_traffic) / sum(uplink_duration)
                            else 0
                    as mean_uplink_traffic,
                from fact_dw1745159013_0000000000038194_metric_1d
                where time >= now() - interval '1 day'
                    and city = ***
                    and cell_ul_group=1
                    and app_id=mobile_game
                group by time
                order by mean_uplink_traffic"
# example 3 
   - "query" : "查询最近一天的小米终端的触发保障的用户数，并对齐前一天的环比"   
   - "output" : "SELECT
                    t1.time,
                    t1.term_brand,
                    t1.trigger_assurance_users AS yesterday_users,
                    t2.trigger_assurance_users AS day_before_yesterday_users,
                    -- 核心：计算除法，并保留4位小数
                    ROUND(
                    (t1.trigger_assurance_users - t2.trigger_assurance_users) * 1.0 
                    / t2.trigger_assurance_users, 4) AS growth_rate
                FROM (
                    -- 昨天的数据
                    SELECT time, term_brand, trigger_assurance_users
                    FROM fact_dw1745159016_0000000000000020_metric_1d
                    WHERE time >= (current_date - 1) AND term_brand = 3
                ) t1
                JOIN (
                    -- 前天的数据
                    SELECT time, term_brand, trigger_assurance_users
                    FROM fact_dw1745159016_0000000000000020_metric_1d
                    WHERE time >= (current_date - 2) AND time < (current_date - 1) AND term_brand = 3
                ) t2
                ON t1.time = t2.time AND t1.term_brand = t2.term_brand"
