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

import random
import re
from typing import TYPE_CHECKING, Any

import networkx as nx

if TYPE_CHECKING:
    from pyvis.network import Network  # type: ignore[reportMissingTypeStubs]

_BACKBONE_NODE_TYPES: frozenset[str] = frozenset({"Query", "Response", "State", "Action"})

_PYVIS_NETWORK_OPTIONS: str = """
  var options = {
      "interaction": {
          "hover": true,
          "edge_titleDelay": 300,
          "zoomView": true,
          "navigationButtons": true,
          "keyboard": true
      },
      "physics": {
          "stabilization": {
              "iterations": 1000
          }
      },
      "nodes": {
          "shadow": {
              "enabled": true
          }
      },
      "edges": {
          "shadow": {
              "enabled": true
          },
          "smooth": {
              "type": "continuous",
              "forceDirection": "none"
          }
      }
  }
  """


def generate_color(*, n: int) -> list[str]:
    """
    Generate a list of RGB color strings.

    Args:
        n (int): The number of unique colors to generate.

    Returns:
        List[str], a list of RGB color strings.
    """
    preset_colors = [  # 淡彩
        "rgb(255,233,232)",  # 樱花粉
        "rgb(255,250,205)",  # 柠檬黄
        "rgb(224,255,255)",  # 薄荷蓝
        "rgb(240,255,240)",  # 薄荷绿
        "rgb(255,239,213)",  # 奶油杏
        "rgb(245,222,245)",  # 淡紫
        "rgb(255,245,238)",  # 雪白
        "rgb(230,230,250)",  # 薰衣草
        "rgb(255,248,220)",  # 米色
        "rgb(240,255,255)",  # 淡青
    ]

    if n <= len(preset_colors):
        return preset_colors[:n]

    result_colors: list[str] = list(preset_colors)
    while len(result_colors) < n:
        base_color: str = random.choice(preset_colors)
        r: int
        g: int
        b: int
        r, g, b = map(int, base_color.strip("rgb()").split(","))
        channels: list[int] = [r, g, b]
        ids: list[int] = random.sample([0, 1, 2], k=random.choice([1, 2]))
        for cur_id in ids:
            channels[cur_id] = max(0, min(255, channels[cur_id] + random.randint(-20, 20)))

        r, g, b = channels
        new_color: str = f"rgb({r},{g},{b})"
        if new_color not in result_colors:
            result_colors.append(new_color)

    return result_colors


def html_config(*, G: nx.DiGraph) -> dict[str, Any]:
    """
    Config of visual elements for the graph.

    Args:
        G (nx.DiGraph): The graph object.

    Returns:
        Dict, return html's config {"node_color_map":"...", "node_shape_map":"...", ...}
    """
    config: dict[str, Any] = {}
    node_types: set[str] = set()
    node: tuple[str, dict[str, str]]
    for node in G.nodes(data=True):
        node_types.add(node[1]["node_type"])

    color: list[str] = generate_color(n=len(node_types))
    config["node_shape_map"] = {}
    for node_type in node_types:
        if node_type in ["Query", "Response", "State", "Action"]:
            config["node_shape_map"][node_type] = "box"
        else:
            config["node_shape_map"][node_type] = "dot"

    config["node_color_map"] = {node_type: color[i] for i, node_type in enumerate(node_types)}
    config["node_size_map"] = {"dot": 20, "box": 25}
    edge_types: set[str] = set()
    edge: tuple[str, str, dict[str, str]]
    for edge in G.edges(data=True):
        edge_types.add(edge[2]["edge_type"])

    edge_colors: list[str] = generate_color(n=len(edge_types))
    config["edge_color_map"] = {edge_type: edge_colors[i] for i, edge_type in enumerate(edge_types)}
    config["edge_length"] = 180
    return config


def add_legend(
    *, node_color_map: dict[str, str], node_shape_map: dict[str, str], edge_color_map: dict[str, str]
) -> str:
    """
    Add legend HTML for the graph visualization.

    Args:
        node_color_map (dict[str, str]): Mapping of node types to colors.
        node_shape_map (dict[str, str]): Mapping of node types to shapes.
        edge_color_map (dict[str, str]): Mapping of edge types to colors.

    Returns:
        Str, legend HTML string.
    """
    # add legend
    legend_html: str = """
    <div id="network-legend"
        style="position: absolute; top: 10px; right: 10px;
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid #ccc; border-radius: 5px;
                padding: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.2);
                z-index: 1000;">
        <div style="margin-bottom: 8px; font-weight: bold;">节点类型：</div>
    """

    # nodes legend
    for node_type, color in node_color_map.items():
        shape: str = node_shape_map.get(node_type, "dot")
        border_radius: str = "border-radius: 50%;" if shape == "dot" else ""
        shape_html: str = f"""<div style="width: 20px; height: 20px; {border_radius}
                                background-color: {color}; border: 1px solid #aaa;"></div>"""
        legend_html += f"""
        <div style="display: flex; align-items: center; margin-bottom: 8px;">
            <div style="width: 50px; display: flex; justify-content: center; align-items: center;">
                {shape_html}
            </div>
            <span style="margin-left: 5px; line-height: 20px;">{node_type}</span>
        </div>
        """

    # edges legend
    legend_html += """
        <div style="margin-top: 15px; margin-bottom: 8px; font-weight: bold;">关系类型：</div>
    """
    for relationship, color in edge_color_map.items():
        legend_html += f"""
        <div style="display: flex; align-items: center; margin-bottom: 8px;">
            <div style="width: 50px; display: flex; justify-content: center; align-items: center;">
                <svg width="40" height="10">
                    <line x1="0" y1="5" x2="30" y2="5" stroke="{color}" stroke-width="2"></line>
                    <polygon points="30,5 25,2 25,8" fill="{color}"></polygon>
                </svg>
            </div>
            <span style="margin-left: 5px; line-height: 20px;">{relationship}</span>
        </div>
        """
    legend_html += """
    </div>
    """

    return legend_html


def format_node_title(*, node_attr: dict[str, str], max_value_len: int = 180) -> str:
    """
    Format all fields in node_attr into a string for the node title.
    If a value is too long, truncate and add ellipsis.

    Args:
        node_attr (dict[str, str]): The node attributes.
        max_value_len (int): Maximum length of each attribute value.

    Returns:
        Str, formatted node title.
    """
    lines = []
    for k, v in node_attr.items():
        v_str = str(v)
        if len(v_str) > max_value_len:
            v_str = v_str[:max_value_len] + "..."

        lines.append(f"{k}: {v_str}")

    return "\n".join(lines)


def _inject_disable_physics_after_stabilization(*, html_content: str) -> str:
    """
    Disable physics after stabilization.

    Args:
        html_content (str): The HTML content.

    Returns:
        Str, the HTML content with the JavaScript code to disable physics after stabilization.
    """
    return re.sub(
        r"(\s*network = new vis\.Network\(container, data, options\);)",
        (
            r"\1\n"
            r"                  network.once('stabilizationIterationsDone', "
            r"function () {"
            r"\n"
            r"                    nodes.update(nodes.get().map(function (n) {"
            r" return { id: n.id, physics: false }; }));"
            r"\n"
            r"                    network.setOptions({ physics: { enabled: false } });"
            r"\n"
            r"                    network.on('dragStart', function (params) {"
            r" if (!params.nodes || params.nodes.length === 0) { return; }"
            r" var dragged = params.nodes;"
            r" var affected = {};"
            r" dragged.forEach(function (id) {"
            r" affected[id] = true;"
            r" network.getConnectedNodes(id, 'to').forEach(function (nid) {"
            r" affected[nid] = true; }); });"
            r" var updates = [];"
            r" nodes.get().forEach(function (n) {"
            r" updates.push({ id: n.id, physics: !!affected[n.id] }); });"
            r" nodes.update(updates);"
            r" network.setOptions({ physics: { enabled: true,"
            r" stabilization: false } });"
            r" });"
            r"\n"
            r"                    network.on('dragEnd', function () {"
            r" nodes.update(nodes.get().map(function (n) {"
            r" return { id: n.id, physics: false }; }));"
            r" network.setOptions({ physics: { enabled: false } });"
            r" });"
            r"\n"
            r"                  });"
        ),
        html_content,
    )


def _ordered_backbone_nodes(*, G: nx.DiGraph) -> list[tuple[str, dict[str, str]]]:
    """
    Topo order from full DAG restricted to Query/State/Action nodes.

    Args:
        G (nx.DiGraph): The graph object.

    Returns:
        List[tuple[str, dict[str, str]]], the ordered backbone nodes.
    """
    backbone_set: set[str] = {
        nid for nid, attr in G.nodes(data=True) if attr.get("node_type", "") in _BACKBONE_NODE_TYPES
    }
    topo_order: list[str] = list(nx.topological_sort(G))
    backbone_ordered: list[str] = [nid for nid in topo_order if nid in backbone_set]
    attrs: dict[str, dict[str, str]] = dict(G.nodes(data=True))
    return [(nid, attrs[nid]) for nid in backbone_ordered]


def _backbone_vertical_positions(
    *,
    backbone_nodes: list[tuple[str, dict[str, str]]],
    col_step: int,
    row_gap: int,
) -> dict[str, tuple[int, int]]:
    """
    Generate positions using a vertical backbone with diamond-shaped Action branches.

    Query / Response / State nodes are placed on the central vertical axis (x=0).
    Consecutive Action nodes between two backbone nodes fan out horizontally
    at the midpoint, forming diamond shapes when multiple Actions exist.

    Args:
        backbone_nodes (list[tuple[str, dict[str, str]]]): The backbone nodes
            in topological order.
        col_step (int): Horizontal spacing between sibling Action nodes.
        row_gap (int): Vertical spacing between backbone nodes.

    Returns:
        Dict[str, tuple[int, int]], the positions of the backbone nodes.
    """
    positions: dict[str, tuple[int, int]] = {}
    y_level = 0
    i = 0
    n = len(backbone_nodes)

    while i < n:
        nid, attr = backbone_nodes[i]
        node_type = attr.get("node_type", "")

        if node_type in ("Query", "Response", "State"):
            positions[nid] = (0, y_level * row_gap)
            y_level += 1
            i += 1
        else:
            # Collect consecutive Action nodes
            actions: list[str] = []
            while i < n and backbone_nodes[i][1].get("node_type", "") == "Action":
                actions.append(backbone_nodes[i][0])
                i += 1
            if actions:
                count = len(actions)
                x_start = -(count - 1) * col_step / 2
                y = (y_level - 0.5) * row_gap
                for j, action_id in enumerate(actions):
                    x = x_start + j * col_step
                    positions[action_id] = (int(x), int(y))

    return positions


def _action_label_substitution(*, G: nx.DiGraph) -> dict[str, str]:
    """
    Map short label (parentheses suffix) -> action text for Action nodes.

    Args:
        G (nx.DiGraph): The graph object.

    Returns:
        Dict[str, str], the mapping of short label to action text.
    """
    mapping: dict[str, str] = {}
    for node_id, node_attr in G.nodes(data=True):
        if node_attr.get("node_type", "") == "Action":
            short_label = node_id.split("(")[-1].strip(")")
            mapping[short_label] = node_attr["action"]

    return mapping


def _pyvis_node_kwargs(
    *,
    node_id: str,
    node_attr: dict[str, str],
    config: dict[str, Any],
    action_labels: dict[str, str],
    backbone_pos: dict[str, tuple[int, int]],
) -> dict[str, Any]:
    """
    Generate the keyword arguments for the pyvis node.

    Args:
        node_id (str): The ID of the node.
        node_attr (dict[str, str]): The attributes of the node.
        config (dict[str, Any]): The configuration for the visualization.
        action_labels (dict[str, str]): The action labels.
        backbone_pos (dict[str, tuple[int, int]]): The positions of the backbone nodes.

    Returns:
        Dict[str, Any], the keyword arguments for the pyvis node.
    """
    node_type: str = node_attr.get("node_type", "")
    node_shape: str = config.get("node_shape_map", {}).get(node_type, "box")
    node_label: str = node_id.split("(")[-1].strip(")")
    node_color: str = config.get("node_color_map", {}).get(node_type, "rgb(201,201,201)")
    node_kwargs: dict[str, Any] = {
        "n_id": node_id,
        "label": action_labels.get(node_label, node_label),
        "title": format_node_title(node_attr=node_attr),
        "color": node_color,
        "shape": node_shape,
        "size": config.get("node_size_map", {}).get(node_shape, 20),
        "font": {"size": 16, "face": "Arial", "bold": True},
    }
    if node_type in ["Query", "Response"]:
        node_kwargs["color"] = {
            "background": node_color,
            "border": "#d32f2f",
            "highlight": {"background": node_color, "border": "#111111"},
            "hover": {"background": node_color, "border": "#111111"},
        }
        node_kwargs["borderWidth"] = 4
        node_kwargs["borderWidthSelected"] = 4
        node_kwargs["font"] = {"size": 16, "face": "Arial", "bold": {"mod": "bold", "size": 18}}

    if node_id in backbone_pos:
        x, y = backbone_pos[node_id]
        node_kwargs["x"] = x
        node_kwargs["y"] = y
        node_kwargs["fixed"] = {"x": True, "y": True}
        node_kwargs["physics"] = False

    return node_kwargs


def _add_pyvis_edges(*, net: Network, G: nx.DiGraph, config: dict[str, Any]) -> None:
    """
    Add edges to the pyvis network.

    Args:
        net (Network): The pyvis network object.
        G (nx.DiGraph): The graph object.
        config (dict[str, Any]): The configuration for the visualization.
    """
    edge_color_map: dict[str, str] = config.get("edge_color_map", {})
    for source, target, edge_attr in G.edges(data=True):
        is_backbone_edge = (
            G.nodes[source].get("node_type", "") in _BACKBONE_NODE_TYPES
            and G.nodes[target].get("node_type", "") in _BACKBONE_NODE_TYPES
        )
        edge_kwargs: dict[str, Any] = {
            "source": source,
            "to": target,
            "title": f"Type: {edge_attr.get('edge_type', '')}",
            "color": edge_color_map.get(edge_attr.get("edge_type", ""), "black"),
            "arrows": "to",
            "width": 5 if is_backbone_edge else 2,
            "length": config.get("edge_length", 100),
            "dashes": False,
        }
        if is_backbone_edge:
            edge_kwargs["smooth"] = {"enabled": True, "type": "cubicBezier", "roundness": 0.2}
        else:
            edge_kwargs["smooth"] = True

        net.add_edge(**edge_kwargs)


def _write_pyvis_html(*, net: Network, output_html: str, config: dict[str, Any]) -> None:
    """
    Write the HTML content to the file.

    Args:
        net (Network): The pyvis network object.
        output_html (str): The path to save the HTML file.
        config (dict[str, Any]): The configuration for the visualization.
    """
    edge_color_map: dict[str, str] = config.get("edge_color_map", {})
    legend_html: str = add_legend(
        node_color_map=config.get("node_color_map", {}),
        node_shape_map=config.get("node_shape_map", {}),
        edge_color_map=edge_color_map,
    )
    net.save_graph(output_html)
    with open(output_html, encoding="utf-8") as f:
        html_content: str = f.read()

    html_content = re.sub("(<body.*?>)", r"\1" + legend_html, html_content)
    html_content = _inject_disable_physics_after_stabilization(html_content=html_content)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)


def graph_to_html(*, config: dict[str, Any], G: nx.DiGraph, output_html: str) -> None:
    """
    Convert pandas DataFrames to an interactive HTML visualization using pyvis.

    Args:
        config (dict[str, Any]): Configuration for the visualization.
        G (nx.DiGraph): The graph object.
        output_html (str): Path to save HTML file.
    """
    try:
        from pyvis.network import Network  # type: ignore[reportMissingTypeStubs]
    except ImportError as e:
        raise ImportError(
            "pyvis is required for trajectory visualization. Install with: uv sync --extra trajectory_graph"
        ) from e

    net: Network = Network(height="800px", width="100%", bgcolor="#f9f9f9", directed=True)
    net.barnes_hut(
        gravity=-25000, central_gravity=-1500, spring_length=500, spring_strength=0.03, damping=0.09, overlap=0
    )
    backbone_nodes = _ordered_backbone_nodes(G=G)
    col_step = 400
    row_gap = 300
    backbone_pos = _backbone_vertical_positions(backbone_nodes=backbone_nodes, col_step=col_step, row_gap=row_gap)
    action_labels = _action_label_substitution(G=G)
    for node_id, node_attr in G.nodes(data=True):
        kwargs = _pyvis_node_kwargs(
            node_id=node_id, node_attr=node_attr, config=config, action_labels=action_labels, backbone_pos=backbone_pos
        )
        net.add_node(**kwargs)

    _add_pyvis_edges(net=net, G=G, config=config)
    net.set_options(_PYVIS_NETWORK_OPTIONS)
    _write_pyvis_html(net=net, output_html=output_html, config=config)
