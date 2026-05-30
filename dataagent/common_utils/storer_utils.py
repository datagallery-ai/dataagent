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

from dataclasses import asdict, is_dataclass
from typing import Any

# 需要映射的 message 则补充到这里
type_to_message_class_dict = {}


def _to_jsonable_message(m: Any) -> Any:
    if isinstance(m, dict):
        return m
    if hasattr(m, "model_dump"):
        try:
            return m.model_dump()
        except Exception:
            return {"content": str(m)}
    if is_dataclass(m) and not isinstance(m, type):
        return asdict(m)
    return {"content": str(m)}


def serialize_state_for_store(state: dict[str, Any]) -> dict[str, Any]:
    """
    convert the un-serialisable state to serialisable one
    exemplar usage inside a node:
    ```python
    store = get_store()
    namespace = ...
    key = ...
    store.put(namespace, key, serialize_state_for_store(state))
    ```
    """
    new_state: dict[str, Any] = {}
    for k, v in (state or {}).items():
        if k == "messages" and isinstance(v, list):
            new_state[k] = [_to_jsonable_message(x) for x in v]
        elif k == "tools":
            new_state[k] = str(v)
        elif k == "reflected_nodes":
            new_state[k] = tuple(v) if isinstance(v, (list, set, tuple)) else str(v)
        else:
            new_state[k] = v
    return new_state


def deserialize_state_from_store(state: dict[str, Any]):
    """
    exemplar usage inside a node:
    ```python
    store = get_store()
    data = store.search(namespace) # all histories
    data.sort(key=lambda x:x.created_at)
    recovered_state = deserialize_state_from_store(data[-1].value) # recover the latest history
    ```
    """
    new_state = {}
    for k, v in (state or {}).items():
        if k == "messages" and isinstance(v, list):
            recovered_messages = []
            for ms in v:
                if not isinstance(ms, dict):
                    continue
                src = ms.get("source")
                if not src or src not in type_to_message_class_dict:
                    continue
                payload = dict(ms)
                payload.pop("source", None)
                recovered_messages.append(type_to_message_class_dict[src](source=src, **payload))
            new_state[k] = recovered_messages
        elif k == "reflected_nodes":
            new_state[k] = set(v) if isinstance(v, (list, set, tuple)) else set()
        else:
            new_state[k] = v
    return new_state


def clear_namespace(store, namespace, prefix_clear=False):
    """
    Arguments:
        * store: LangGraphStore
        * namespace: target namespace
        * prefix_clear: if enabled, delete all prefix-match records,
            otherwise, only delete full-match records.

    Return:
        * number of deleted records
    """
    data = store.search(namespace)
    cnt = 0
    for d in data:
        # only prefix match
        if d.namespace != namespace:
            if prefix_clear:
                store.delete(d.namespace, d.key)
                cnt += 1
            continue
        # full match
        store.delete(namespace, d.key)
        cnt += 1
    return cnt
