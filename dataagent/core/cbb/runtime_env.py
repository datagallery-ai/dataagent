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
"""运行环境监测：系统、Python环境、资源占用、模型上下文窗口、数据库状态。"""

from __future__ import annotations

import contextlib
import importlib
import os
import platform
import queue
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.utils.constants import (
    DEFAULT_CPU_SAMPLE_SECONDS,
    DEFAULT_DB_CONNECT_PROBE_TIMEOUT,
    DEFAULT_DB_PROCESS_KILL_TIMEOUT,
    DEFAULT_DB_PROCESS_PROBE_TIMEOUT,
)


def _db_probe_worker_spawn(db_url: str, out_queue: Any) -> None:
    """spawn 模式下的 worker（必须是模块级可 pickle 函数）。"""
    try:
        sqlalchemy = importlib.import_module("sqlalchemy")
        engine = sqlalchemy.create_engine(db_url, connect_args={"connect_timeout": DEFAULT_DB_CONNECT_PROBE_TIMEOUT})
        try:
            with engine.connect() as conn:
                conn.execute(sqlalchemy.text("SELECT 1"))
        finally:
            engine.dispose()
        out_queue.put(True)
    except Exception:
        with contextlib.suppress(Exception):
            out_queue.put(False)


# 内置模型上下文窗口映射表
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v3": 131072,
    "deepseek-v3.2": 131072,
    "deepseek-r1": 131072,
    "deepseek-chat": 131072,
    "deepseek-reasoner": 131072,
    "deepseek-v4-pro": 1000000,
    "deepseek-v4-flash": 1000000,
    "qwen3-max": 262144,
    "qwen3-max-preview": 262144,
    "qwen3.6-plus": 1000000,
    "qwen3.5-plus": 1000000,
    "qwen-plus": 1000000,
    "qwen-plus-latest": 1000000,
    "qwen3.6-flash": 1000000,
    "qwen3.5-flash": 1000000,
    "qwen-flash": 1000000,
    "qwen-long": 10000000,
    "kimi-k2.6": 262144,
    "kimi-k2.5": 262144,
    "glm-5.1": 202745,
    "glm-5": 202752,
    "glm-4.7": 202752,
    "glm-4.6": 202752,
    "MiniMax-M2.5": 196608,
    "MiniMax-M2.1": 204800,
}


def _get_context_window(model_name: str, user_config: dict[str, Any] | None = None) -> int | None:
    """获取模型上下文窗口：优先用户配置，其次模糊匹配；都找不到则返回 None（不展示）。"""
    if user_config and isinstance(user_config, dict):
        user_window = user_config.get("context_windows")
        if user_window is not None:
            try:
                window = int(user_window)
            except (ValueError, TypeError):
                pass
            else:
                if window > 0:
                    return window

    # 模糊匹配：按 key 长度从长到短，避免如 "qwen-plus" 抢先匹配 "qwen-plus-latest"
    name_lower = model_name.lower()
    for key in sorted(DEFAULT_CONTEXT_WINDOWS.keys(), key=len, reverse=True):
        if key.lower() in name_lower:
            return DEFAULT_CONTEXT_WINDOWS[key]

    return None


@dataclass(frozen=True, slots=True)
class LinuxResourceAdjustments:
    cpu_total: float
    mem_total_bytes: int
    mem_used_bytes: int
    cpu_percent: float | None
    limit_source: str
    usage_source: str


class RuntimeEnvironmentCollector:
    """运行环境信息收集器"""

    def __init__(self, workspace_path: Path | None = None, agent_config_manager: Any | None = None):
        """Initialize collector for runtime environment introspection.

        Args:
            workspace_path: Current workspace directory.
            agent_config_manager: Per-Agent ConfigManager for DATABASE introspection.
        """
        self.workspace_path = workspace_path or Path.cwd()
        self._agent_config_manager = agent_config_manager

    @staticmethod
    def get_cpu_count() -> int | None:
        """获取容器CPU核心数（公共API）"""
        try:
            resources = RuntimeEnvironmentCollector._get_resource_info()
            cpu_count = resources.get("cpu_count")
            if cpu_count is not None:
                return int(cpu_count)
            return None
        except Exception as e:
            logger.debug(f"Failed to get CPU count: {e}")
            return None

    @staticmethod
    def _get_timezone() -> str:
        """获取系统时区。"""
        tz = os.environ.get("TZ", "").strip()
        if tz:
            return tz
        try:
            if time.tzname:
                return str(time.tzname[0])
        except (AttributeError, IndexError):
            pass
        return "Unknown"

    @staticmethod
    def _get_runtime_info() -> dict[str, str]:
        """Python/Conda环境。"""
        # 检测虚拟环境类型
        env_type = "system"
        env_name = "none"
        if os.environ.get("CONDA_DEFAULT_ENV"):
            env_type = "conda"
            env_name = os.environ.get("CONDA_DEFAULT_ENV", "unknown")
        elif sys.prefix != sys.base_prefix:
            env_type = "venv"
            env_name = Path(sys.prefix).name

        return {
            "python_version": sys.version.split()[0],
            "python_executable": (sys.executable or "").strip(),
            "env_type": env_type,
            "env_name": env_name,
        }

    @staticmethod
    def _linux_cgroup_resource_adjustments(
        host_cpu: int,
        cpu_total: float,
        mem_total_bytes: int,
        mem_used_bytes: int,
        cpu_percent: float | None,
        limit_source: str,
        usage_source: str,
    ) -> LinuxResourceAdjustments:
        """在 Linux 上根据 cgroup/cpuset 调整资源上限与用量来源（不改变非 Linux 行为）。"""

        def read(path: str) -> str:
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read().strip()
            except OSError:
                return ""

        def read_int(path: str) -> int | None:
            value = read(path)
            return int(value) if value and value != "max" else None

        cgroup_v2 = os.path.exists("/sys/fs/cgroup/cgroup.controllers")
        cgroup_version = 2 if cgroup_v2 else 1

        try:
            in_container = (
                any(os.path.exists(p) for p in ("/.dockerenv", "/.containerenv", "/run/.containerenv"))
                or any(k in os.environ for k in ("KUBERNETES_SERVICE_HOST", "POD_NAME"))
                or any(
                    k in read("/proc/1/cgroup").lower()
                    for k in ("docker", "kubepods", "containerd", "libpod", "podman", "lxc")
                )
            )
        except Exception:
            logger.debug(f"Failed to check container: {traceback.format_exc()}")
            in_container = False

        # --- CPU limit (quota + cpuset) ---
        cpu_quota: float | None = None
        if cgroup_v2:
            parts = read("/sys/fs/cgroup/cpu.max").split()
            if len(parts) == 2 and parts[0] != "max":
                quota, period = int(parts[0]), int(parts[1])
                if quota > 0 and period > 0:
                    cpu_quota = round(quota / period, 2)
        else:
            quota = read_int("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
            period = read_int("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
            if quota and period and quota > 0 and period > 0:
                cpu_quota = round(quota / period, 2)

        def parse_cpuset(value: str) -> int | None:
            total = 0
            for part in value.split(","):
                part = part.strip()
                if not part:
                    continue
                if "-" in part:
                    start, end = (int(x) for x in part.split("-", 1))
                    total += end - start + 1
                else:
                    total += 1
            return total or None

        cpuset_limit: int | None = None
        for path in (
            "/sys/fs/cgroup/cpuset.cpus.effective",
            "/sys/fs/cgroup/cpuset.cpus",
            "/sys/fs/cgroup/cpuset/cpuset.cpus",
        ):
            parsed = parse_cpuset(read(path))
            if parsed:
                cpuset_limit = parsed
                break

        cpu_candidates = (cpu_quota, float(cpuset_limit) if cpuset_limit else None)
        cpu_limits = [x for x in cpu_candidates if x and x < host_cpu]
        if cpu_limits:
            cpu_total = float(min(cpu_limits))
            limit_source = f"cgroup_v{cgroup_version}"

        # --- Memory limit / usage ---
        if cgroup_v2:
            mem_limit = read_int("/sys/fs/cgroup/memory.max")
            if mem_limit and mem_limit > 10**18:
                mem_limit = None
            mem_used = read_int("/sys/fs/cgroup/memory.current")
        else:
            mem_limit = read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
            if mem_limit and mem_limit > 10**18:
                mem_limit = None
            mem_used = read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")

        if mem_limit:
            mem_total_bytes = mem_limit
            limit_source = f"cgroup_v{cgroup_version}"
        if mem_used is not None and (in_container or mem_limit):
            mem_used_bytes = mem_used
            usage_source = f"cgroup_v{cgroup_version}"

        # --- CPU usage percent (prefer cgroup usage when possible) ---
        sample_seconds = DEFAULT_CPU_SAMPLE_SECONDS

        def read_usage_usec() -> int | None:
            if cgroup_v2:
                for line in read("/sys/fs/cgroup/cpu.stat").splitlines():
                    key, _, value = line.partition(" ")
                    if key == "usage_usec" and value:
                        return int(value)
                return None
            value = read_int("/sys/fs/cgroup/cpuacct/cpuacct.usage")
            return value // 1000 if value is not None else None

        if in_container or cpu_limits or mem_limit:
            start = read_usage_usec()
            if start is not None:
                time.sleep(sample_seconds)
                end = read_usage_usec()
                if end is not None and end >= start and cpu_total > 0:
                    used_cores = (end - start) / (sample_seconds * 1_000_000)
                    cpu_percent = round(used_cores / cpu_total * 100, 2)
                    usage_source = f"cgroup_v{cgroup_version}"

        return LinuxResourceAdjustments(
            cpu_total=cpu_total,
            mem_total_bytes=mem_total_bytes,
            mem_used_bytes=mem_used_bytes,
            cpu_percent=cpu_percent,
            limit_source=limit_source,
            usage_source=usage_source,
        )

    @staticmethod
    def _get_resource_info() -> dict[str, Any]:
        """CPU/内存资源。"""
        try:
            psutil = importlib.import_module("psutil")

            gb = 1024**3
            host_cpu = psutil.cpu_count(logical=True) or 1
            host_mem = psutil.virtual_memory()

            # Default: host view via psutil (works on Windows/macOS/Linux host).
            cpu_total: float = float(host_cpu)
            mem_total_bytes: int = int(host_mem.total)
            mem_used_bytes: int = int(host_mem.used)
            cpu_percent: float | None = None
            limit_source = "psutil"
            usage_source = "psutil"

            # On Linux, prefer cgroup/cpuset if present (container or constrained host).
            if platform.system() == "Linux":
                adjustments = RuntimeEnvironmentCollector._linux_cgroup_resource_adjustments(
                    host_cpu,
                    cpu_total,
                    mem_total_bytes,
                    mem_used_bytes,
                    cpu_percent,
                    limit_source,
                    usage_source,
                )
                cpu_total = adjustments.cpu_total
                mem_total_bytes = adjustments.mem_total_bytes
                mem_used_bytes = adjustments.mem_used_bytes
                cpu_percent = adjustments.cpu_percent
                limit_source = adjustments.limit_source
                usage_source = adjustments.usage_source

            if cpu_percent is None:
                cpu_percent = psutil.cpu_percent(interval=0.5)

            memory_total_gb = round(mem_total_bytes / gb, 2)
            memory_used_gb = round(mem_used_bytes / gb, 2)
            memory_percent = (
                round(mem_used_bytes / mem_total_bytes * 100, 2) if mem_total_bytes > 0 else host_mem.percent
            )

            cpu_count_out: float | int = int(cpu_total) if float(cpu_total).is_integer() else cpu_total

            result: dict[str, Any] = {
                "cpu_count": cpu_count_out,
                "cpu_usage_percent": cpu_percent,
                "memory_total_gb": memory_total_gb,
                "memory_used_gb": memory_used_gb,
                "memory_percent": memory_percent,
                "available": True,
            }

            # Keep original fields; add sources as optional debug/helpful metadata.
            if limit_source != "psutil" or usage_source != "psutil":
                result["limit_source"] = limit_source
                result["usage_source"] = usage_source
            return result
        except Exception:
            logger.debug(f"Failed to get resource info: {traceback.format_exc()}")
            return {}

    @staticmethod
    def _get_database_url(cfg_manager: Any) -> str:
        """从配置获取数据库URL（DATABASE.config.path）。"""
        database = cfg_manager.get("DATABASE") or {}
        if database:
            db_config = database.get("config", {})
            path = db_config.get("path", "")
            if path:
                return path
        return ""

    @staticmethod
    def _check_sqlite_file_readable(path: str) -> bool:
        """只读检测 sqlite 文件"""
        db_path = Path(path).expanduser().resolve()
        if not db_path.exists() or not os.access(db_path, os.R_OK):
            return False
        conn: sqlite3.Connection | None = None
        try:
            uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True)
            cur = conn.execute("SELECT 1")
            cur.fetchone()
            cur.close()
            return True
        except sqlite3.Error as e:
            logger.debug(f"SQLite readability check failed for {db_path}: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

    def collect(self) -> dict[str, Any]:
        """收集环境信息（system/runtime/resources/database）。"""
        return {
            "system": {
                "os_type": platform.system(),
                "os_release": platform.release(),
                "arch": platform.machine(),
                "timezone": self._get_timezone(),
            },
            "runtime": self._get_runtime_info(),
            "resources": self._get_resource_info(),
            "database": self._get_database_info(),
        }

    def collect_with_models(self, llm_configs: dict[str, Any]) -> dict[str, Any]:
        """收集完整信息（含 planner 模型上下文窗口）。

        只暴露 planner 这一个节点的上下文窗口，用作 token budget 提醒；
        不输出全量模型列表，避免误导 LLM「可以自行选模型」。
        """
        result = self.collect()

        planner_cfg = llm_configs.get("planner")
        if isinstance(planner_cfg, dict):
            model_id = str(planner_cfg.get("model") or "").strip()
            if model_id:
                window = _get_context_window(model_id, planner_cfg)
                if window is not None:
                    result["planner_model"] = {"model": model_id, "context_window": window}
        return result

    def _get_database_info(self) -> dict[str, Any]:
        """获取数据库配置及可读状态。

        规则：仅当能从 DATABASE.engine 拿到引擎类型时才展示数据库信息；否则打 warning 并跳过展示。
        """
        cm = self._agent_config_manager
        if cm is None:
            return {}
        database_cfg = cm.get("DATABASE") or {}
        if not database_cfg:
            return {}
        engine = ""
        if isinstance(database_cfg, dict) and database_cfg.get("engine"):
            engine = str(database_cfg.get("engine") or "").strip().lower()
        if not engine:
            logger.warning("runtime_env: DATABASE.engine is missing; skip database info in prompt.")
            return {}

        # 获取数据库URL（仅支持 DATABASE.config.path）
        db_url = self._get_database_url(cm)
        if not db_url:
            logger.warning("runtime_env: DATABASE.config.path is missing; skip database info in prompt.")
            return {}

        # SQLAlchemy统一检测
        readable = self._check_db_readable(db_url)

        # 获取db_id：优先DATABASE.db_id，其次从URL解析dbname，最后unknown
        db_id = "unknown"
        if database_cfg:
            db_id = database_cfg.get("db_id", "unknown")
        if db_id == "unknown" and "/" in db_url:
            # 从URL末尾解析数据库名
            db_id = db_url.rsplit("/", 1)[-1].split("?")[0] or "unknown"

        return {
            "db_id": db_id,
            "engine": engine,
            "readable": readable,
        }

    def _check_db_readable(self, url: str) -> bool:
        """检测数据库是否可读。"""
        if not url:
            return False
        raw = str(url).strip()
        if not raw:
            return False
        if "://" not in raw:
            return self._check_sqlite_file_readable(raw)

        def _db_probe_worker(db_url: str, out_queue: Any) -> None:
            """子进程中做连接探测，避免主进程被底层网络卡死。"""
            try:
                sqlalchemy = importlib.import_module("sqlalchemy")
                engine = sqlalchemy.create_engine(
                    db_url, connect_args={"connect_timeout": DEFAULT_DB_CONNECT_PROBE_TIMEOUT}
                )
                try:
                    with engine.connect() as conn:
                        conn.execute(sqlalchemy.text("SELECT 1"))
                finally:
                    engine.dispose()
                out_queue.put(True)
            except Exception:
                with contextlib.suppress(Exception):
                    out_queue.put(False)

        # 进程级硬超时兜底：connect_timeout 在某些网络/解析场景可能不生效，会导致卡死。
        # 这里最多等待几秒，超时直接判不可达。
        try:
            ctx = get_context("fork")
            target = _db_probe_worker
        except ValueError:
            # 某些平台/配置禁用 fork（例如 Windows 或强制 spawn）
            ctx = get_context("spawn")
            target = _db_probe_worker_spawn
        out_queue = ctx.Queue(maxsize=1)
        proc = ctx.Process(target=target, args=(raw, out_queue), daemon=True)
        proc.start()
        proc.join(timeout=DEFAULT_DB_PROCESS_PROBE_TIMEOUT)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=DEFAULT_DB_PROCESS_KILL_TIMEOUT)
            return False
        try:
            return bool(out_queue.get_nowait())
        except queue.Empty:
            return False
        except Exception:
            return False


def format_runtime_environment(env_data: dict[str, Any]) -> str:
    """格式化为简洁Markdown文本。"""
    lines: list[str] = []

    # 系统配置
    system_info = env_data.get("system", {})
    os_type = system_info.get("os_type", "Unknown")
    os_release = system_info.get("os_release", "")
    arch = system_info.get("arch", "")
    lines.append(f"- OS: {os_type} {os_release} ({arch})")
    tz = system_info.get("timezone")
    if tz:
        lines.append(f"- Timezone: {tz}")

    # Python环境
    rt = env_data.get("runtime", {})
    lines.append(f"- Python: {rt.get('python_version', 'Unknown')} ({rt.get('env_type', 'system')})")
    exe = (rt.get("python_executable") or "").strip()
    if exe:
        lines.append(f"- Python interpreter: {exe}")

    # 资源
    res = env_data.get("resources", {})
    if res.get("available"):
        cpu = res.get("cpu_usage_percent")
        if cpu is not None:
            lines.append(f"- CPU: {cpu}% ({res.get('cpu_count')} cores)")
        mem_total = res.get("memory_total_gb")
        if mem_total:
            lines.append(f"- Memory: {res.get('memory_used_gb')}GB / {mem_total}GB ({res.get('memory_percent')}%)")

    # Database
    db = env_data.get("database")
    if db:
        status = "readable" if db.get("readable") else "unreachable"
        lines.append(f"- Database ({db.get('engine', 'unknown')}): {db.get('db_id', 'unknown')} ({status})")

    # planner 模型上下文窗口（token budget 提醒）
    planner_model = env_data.get("planner_model") or {}
    if isinstance(planner_model, dict):
        model_id = str(planner_model.get("model") or "").strip()
        window = planner_model.get("context_window")
        if model_id and isinstance(window, int):
            lines.append(f"- Planner context window: {window // 1000}k ({model_id})")

    return "\n".join(lines)


def format_runtime_environment_section(env_data: dict[str, Any]) -> str:
    """格式化为完整system prompt段落（带标题）。"""
    content = format_runtime_environment(env_data)
    return f"## Runtime Environment\n{content}" if content else ""
