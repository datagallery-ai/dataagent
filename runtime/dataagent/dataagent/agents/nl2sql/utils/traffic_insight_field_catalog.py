# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Traffic Insight field catalog: canonical field name → kind (dimension|metric).

Keep in sync with ``prompts/perceptor/filter_traffic_insight_fields_system.md``.
"""

from __future__ import annotations

from typing import Literal

FieldKind = Literal["dimension", "metric"]

# name → kind. Descriptions live in the prompt catalog for LLM consumption.
TRAFFIC_INSIGHT_FIELD_KINDS: dict[str, FieldKind] = {
    "time": "dimension",
    "appcate": "dimension",
    "cell": "dimension",
    "browser": "dimension",
    "country": "dimension",
    "term_model": "dimension",
    "subscriber": "dimension",
    "msisdn": "dimension",
    "operator": "dimension",
    "app": "dimension",
    "term_type": "dimension",
    "term_vendor": "dimension",
    "term_os": "dimension",
    "server_ip": "dimension",
    "website": "dimension",
    "network_type": "dimension",
    "imei_tac": "dimension",
    "age_range": "dimension",
    "apn": "dimension",
    "rat": "dimension",
    "subapp": "dimension",
    "user_segment": "dimension",
    "contype": "dimension",
    "block_reason": "dimension",
    "bwsection": "dimension",
    "encryption": "dimension",
    "free_rgsid": "dimension",
    "gender": "dimension",
    "http_version": "dimension",
    "ip_protocol": "dimension",
    "ip_version": "dimension",
    "lai": "dimension",
    "ne": "dimension",
    "ne_group": "dimension",
    "ntai": "dimension",
    "offline_rg": "dimension",
    "offline_rgsid": "dimension",
    "online_rg": "dimension",
    "online_rgsid": "dimension",
    "quic": "dimension",
    "rai": "dimension",
    "region": "dimension",
    "rg_group": "dimension",
    "rule": "dimension",
    "rule_base": "dimension",
    "service_package": "dimension",
    "tai": "dimension",
    "user_group": "dimension",
    "vlsection": "dimension",
    "experience_category": "dimension",
    "rat_type": "dimension",
    "resolution": "dimension",
    "code_rate": "metric",
    "stall": "metric",
    "application": "dimension",
    "rule_base_name": "dimension",
    "term_migration": "dimension",
    "tethering": "dimension",
    "servicename": "dimension",
    "user_migration": "dimension",
    "uplink_volume": "metric",
    "downlink_volume": "metric",
    "connections": "metric",
    "subs_count": "metric",
    "duration": "metric",
    "uplink_packets": "metric",
    "download_packets": "metric",
    "total_call_times": "metric",
    "block_call_times": "metric",
    "tcp_total_data_rtt": "metric",
    "tcp_data_rtt_cnt": "metric",
    "tcp_average_bandwidth": "metric",
    "tcp_bandwidth_count": "metric",
    "tcp_packets_discard_cnt": "metric",
    "tcp_total_packets": "metric",
    "tcp_total_jitter": "metric",
    "tcp_jitter_count": "metric",
    "data_count": "metric",
    "rtt": "metric",
    "short_video_buffer_num": "metric",
    "up_package_loss_rate": "metric",
    "dl_package_loss_rate": "metric",
    "mos": "metric",
    "tcp_ul_max_bandwidth": "metric",
    "tcp_dl_max_bandwidth": "metric",
    "tcp_ul_avg_bandwidth": "metric",
    "tcp_dl_avg_bandwidth": "metric",
    "initial_buffer_time": "metric",
}

# Time words that may appear in LLM output but must not enter need_d/need_m.
# Granularity and time-range intent are handled by LLM₂ / SQL rules, not field recall.
TIME_PSEUDO_FIELDS = frozenset(
    {
        "time",
        "date",
        "day",
        "hour",
        "minute",
        "month",
        "year",
        "week",
        "timestamp",
        "dt",
    }
)

DIMENSION_NAMES = frozenset(name for name, kind in TRAFFIC_INSIGHT_FIELD_KINDS.items() if kind == "dimension")
METRIC_NAMES = frozenset(name for name, kind in TRAFFIC_INSIGHT_FIELD_KINDS.items() if kind == "metric")
