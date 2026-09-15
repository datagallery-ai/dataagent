# 基础特征表全景

当用户未指定具体表名时，根据用户需求关键词匹配最相关的表。注意以下要求：
- 本文档所列的表不保证已经创建，实际是否存在以元数据返回结果为准
- 检索时，要求数据库名和表名均相同才视为同一张表（格式：{数据库名}:{表名}），否则是不同表

## 表清单

| L1 | L2 | L3 | L4 | 表名 | 前置原始表 |
|----|----|----|----|------|-----------|
| 货 | 推广标的 | 应用 | 基础属性 | agapads.ads_agalg_feature_app_basic_info_ds | agapads.ads_agalg_feature_app_basic_info_origin_ds |
| 货 | 推广标的 | 应用 | 行为属性 | agapads.ads_agalg_feature_app_statistics_info_dm | |
| 货 | 推广标的 | 搜索词 | 基础属性 | agapads.ads_agalg_feature_searchword_basic_info_ds | |
| 货 | 推广标的 | 搜索词 | 行为属性 | agapads.ads_agalg_feature_searchword_statistics_info_dm | |
| 货 | 推广标的 | 卡片 | 基础属性 | agapads.ads_agalg_feature_card_basic_info_ds | |
| 货 | 推广标的 | 卡片 | 行为属性 | agapads.ads_agalg_feature_card_statistics_info_dm | |
| 货 | 推广标的 | 故事 | 基础属性 | agapads.ads_agalg_feature_story_basic_info_ds | |
| 货 | 推广标的 | 故事 | 行为属性 | agapads.ads_agalg_feature_story_statistics_info_dm | |
| 人 | 用户属性 | — | — | agapads.ads_agalg_feature_usid_basic_info_ds | |
| 人 | 用户属性 | — | — | agapads.ads_agalg_feature_udid_basic_info_ds | |
| 人 | 用户行为 | 应用行为 | 点击 | agapads.ads_agalg_feature_udid_click_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 点击 | agapads.ads_agalg_feature_usid_click_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 下载 | agapads.ads_agalg_feature_udid_download_behavior_dm | agapads.ads_agalg_feature_udid_download_behavior_origin_dm |
| 人 | 用户行为 | 应用行为 | 下载 | agapads.ads_agalg_feature_usid_download_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 安装状态 | agapads.ads_agalg_feature_udid_install_status_ds | agapads.ads_agalg_feature_udid_install_status_origin_ds |
| 人 | 用户行为 | 应用行为 | 安装状态 | agapads.ads_agalg_feature_usid_install_status_ds | |
| 人 | 用户行为 | 应用行为 | 安装行为 | agapads.ads_agalg_feature_udid_install_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 安装行为 | agapads.ads_agalg_feature_usid_install_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 更新 | agapads.ads_agalg_feature_udid_upgrade_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 更新 | agapads.ads_agalg_feature_usid_upgrade_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 卸载 | agapads.ads_agalg_feature_udid_uninstall_behavior_dm | agapads.ads_agalg_feature_udid_uninstall_behavior_origin_dm |
| 人 | 用户行为 | 应用行为 | 卸载 | agapads.ads_agalg_feature_usid_uninstall_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 曝光 | agapads.ads_agalg_feature_udid_exposure_behavior_dm | agapads.ads_agalg_feature_udid_exposure_behavior_origin_dm |
| 人 | 用户行为 | 应用行为 | 曝光 | agapads.ads_agalg_feature_usid_exposure_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 搜索 | agapads.ads_agalg_feature_udid_search_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 搜索 | agapads.ads_agalg_feature_usid_search_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 使用 | agapads.ads_agalg_feature_udid_use_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 使用 | agapads.ads_agalg_feature_usid_use_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 激活 | agapads.ads_agalg_feature_udid_activate_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 激活 | agapads.ads_agalg_feature_usid_activate_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 注册 | agapads.ads_agalg_feature_udid_register_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 注册 | agapads.ads_agalg_feature_usid_register_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 付费 | agapads.ads_agalg_feature_udid_pay_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 付费 | agapads.ads_agalg_feature_usid_pay_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 留存 | agapads.ads_agalg_feature_udid_retain_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 留存 | agapads.ads_agalg_feature_usid_retain_behavior_dm | |
| 人 | 用户行为 | 应用行为 | 深层行为 | agapads.ads_agalg_feature_udid_deep_behavior_dm | agapads.ads_agalg_feature_udid_deep_behavior_origin_dm |
| 人 | 用户行为 | 应用行为 | 深层行为 | agapads.ads_agalg_feature_usid_deep_behavior_dm | |

## 表名命名规律

- `udid` 系列表：以设备 ID（device_id2）为主键的特征表
- `usid` 系列表：以用户 ID 为主键的特征表
- `_ds` 后缀：日全量表
- `_dm` 后缀：日增量表
- `_basic_info_`：基础属性表（静态属性）
- `_statistics_info_`：行为属性表（统计指标）
- `_behavior_`：行为特征表（序列/指标）

## 匹配规则

当用户未指定表名时，按以下优先级匹配：
1. 用户需求中明确提到行为类型（安装/卸载/曝光/点击/下载/搜索/使用/激活/注册/付费/留存/更新/深层行为）→ 匹配对应行为表
2. 用户需求中提到主体维度（设备/用户）→ 选择 `udid` 或 `usid` 系列
3. 用户需求中提到标的类型（应用/搜索词/卡片/故事）→ 匹配推广标的特征表
4. 用户需求中提到基础属性/行为属性 → 选择 `_basic_info_` 或 `_statistics_info_` / `_behavior_`
