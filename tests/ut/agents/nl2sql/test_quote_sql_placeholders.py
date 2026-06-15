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
import pytest

from dataagent.agents.nl2sql.utils.nl2sql_utils import quote_sql_placeholders, sql_parser


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pt_d = $date", "pt_d = '$date'"),
        ("pt_d='$date'", "pt_d='$date'"),
        ("pt_d > ${starttime, -5, yyyyMMdd}", "pt_d > '${starttime, -5, yyyyMMdd}'"),
        ("pt_d>'${starttime, -5, yyyyMMdd}'", "pt_d>'${starttime, -5, yyyyMMdd}'"),
        (
            "pt_d = $date AND pt_d > ${starttime, -5, yyyyMMdd}",
            "pt_d = '$date' AND pt_d > '${starttime, -5, yyyyMMdd}'",
        ),
    ],
)
def test_quote_sql_placeholders(raw, expected):
    assert quote_sql_placeholders(raw) == expected


def test_sql_parser_quotes_placeholders():
    content = "```sql\nSELECT * FROM t WHERE pt_d = $date\n```"
    assert sql_parser(content) == ["SELECT * FROM t WHERE pt_d = '$date'"]
