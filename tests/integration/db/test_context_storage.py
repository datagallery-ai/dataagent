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
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras as pg_extras
import pytest
from sqlalchemy import create_engine, make_url, text

from dataagent.core.context.utils_context_storage import create_table, get_IR_from_pg, save_IR_to_pg

YOUR_PASSWORD = "xxx"  # 请替换为实际的数据库密码
TEST_URL = f"postgresql://datapilot_user:{YOUR_PASSWORD}@localhost:5432/contextIR_TestDBs"


class TestContextStorage:
    """ContextStorage相关接口测试"""

    @pytest.fixture(autouse=True)
    def set_up(self):
        """环境初始化"""
        create_table(url=TEST_URL)
        yield
        self.delete_database(TEST_URL)

    def delete_database(self, url: str):
        """删除测试数据库"""
        try:
            url_obj = make_url(url)
        except Exception as e:
            raise ValueError("Invalid database URL:") from e

        dbname = url_obj.database
        if not dbname:
            raise ValueError("Database name is required in the URL")

        admin_engine = create_engine(url_obj.set(database="postgres"))
        with admin_engine.connect() as conn:
            conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))

    def insert_data(self, table_name: str, data: dict):
        """插入数据到指定表"""
        conn = psycopg2.connect(TEST_URL)
        try:
            with conn.cursor() as cur:
                columns = ", ".join(data.keys())
                placeholders = ", ".join(["%s"] * len(data))
                sql = f'INSERT INTO "{table_name}" ({columns}) VALUES ({placeholders})'
                # convert Python lists/dicts to JSON for JSONB columns
                params = [pg_extras.Json(v) if isinstance(v, (list, dict)) else v for v in data.values()]
                cur.execute(sql, params)
                conn.commit()
        finally:
            conn.close()

    def test_get_IR_from_pg(self):
        """从PostgreSQL获取IR测试"""
        test_node_type = "Query"
        ir_data = {
            "label": "query00000",
            "description": "用户的加法计算请求",
            "user_id": "jiutian_applicationlayer",
            "session_id": "#00001",
            "run_id": 0,
            "sub_id": 0,
            "created_at": datetime.now(timezone(timedelta(hours=8))),
            "query": "12+23等于几?",
            "additional_files": ["test_file1.txt", "test_file2.txt"],
        }
        save_IR_to_pg(url=TEST_URL, node_type=test_node_type, ir_data=ir_data)
        retrieved = get_IR_from_pg(
            url=TEST_URL, user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        assert isinstance(retrieved, dict)
        expected_fields = {
            "label",
            "description",
            "user_id",
            "session_id",
            "run_id",
            "sub_id",
            "created_at",
            "query",
            "additional_files",
        }
        if retrieved[test_node_type]:
            for ir in retrieved[test_node_type]:
                assert expected_fields.issubset(set(ir.keys()))

    def test_save_IR_to_pg(self):
        """保存IR到PostgreSQL测试"""
        test_node_type = "Query"
        test_ir_data = {
            "label": "query00000",
            "description": "用户的加法计算请求",
            "user_id": "jiutian_applicationlayer",
            "session_id": "#00001",
            "run_id": 0,
            "sub_id": 0,
            "created_at": datetime.now(timezone(timedelta(hours=8))),
            "query": "12+23等于几?",
            "additional_files": ["file1.txt", "file2.txt"],
        }
        save_IR_to_pg(url=TEST_URL, node_type=test_node_type, ir_data=test_ir_data)
        conn = psycopg2.connect(TEST_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(f'SELECT * FROM "{test_node_type}"')
                rows = cur.fetchall()
                retrieved = []
                assert cur.description is not None
                col_names = [d[0] for d in cur.description]
                for row in rows:
                    rec = dict(zip(col_names, row, strict=True))
                    retrieved.append(rec)
        finally:
            conn.close()
        assert isinstance(retrieved, list)
        # 检查刚保存的IR是否存在
        assert any(
            ir.get("label") == test_ir_data["label"]
            and ir.get("user_id") == test_ir_data["user_id"]
            and ir.get("session_id") == test_ir_data["session_id"]
            for ir in retrieved
        )
