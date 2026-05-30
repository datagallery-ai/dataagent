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
from __future__ import annotations

import argparse
import os

import uvicorn
from loguru import logger

_CONFIG_ENV_NAME = "DATAAGENT_REST_CONFIG"


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the DataAgent API server."""
    parser = argparse.ArgumentParser(description="Start the DataAgent FastAPI service.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--workers", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    """Start the DataAgent FastAPI server."""
    args = parse_args()
    config_path = args.config
    os.environ[_CONFIG_ENV_NAME] = config_path
    logger.info(f"Using DataAgent config: {config_path}")
    logger.info(f"Starting DataAgent service on {args.host}:{args.port} with {args.workers} worker(s)")

    uvicorn.run(
        "dataagent.interface.rest_api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
