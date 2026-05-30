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
import json
import random
import re

import networkx as nx
import pandas as pd
from pyvis.network import Network  # type: ignore


def generate_color(n: int) -> list[str]:
    """
    Generate a list of RGB color strings.

    Args:
        n (int): The number of unique colors to generate.

    Returns:
        List[str], a list of RGB color strings.
    """
    preset_colors = [
        "rgb(176,224,229)",
        "rgb(78,206,196)",
        "rgb(69,165,209)",
        "rgb(86,134,210)",
        "rgb(106,201,146)",
        "rgb(154,127,209)",
        "rgb(255,160,122)",
        "rgb(247,204,85)",
        "rgb(235,95,95)",
        "rgb(255,107,107)",
    ]

    if n <= len(preset_colors):
        return preset_colors[:n]

    result_colors = list(preset_colors)
    while len(result_colors) < n:
        base_color = random.choice(preset_colors)

        r, g, b = map(int, base_color.strip("rgb()").split(","))
        channels = [r, g, b]
        ids = random.sample([0, 1, 2], k=random.choice([1, 2]))
        for cur_id in ids:
            channels[cur_id] = max(0, min(255, channels[cur_id] + random.randint(-20, 20)))

        r, g, b = channels
        new_color = f"rgb({r},{g},{b})"
        if new_color not in result_colors:
            result_colors.append(new_color)

    return result_colors


def html_config(config_type: str, G: nx.MultiDiGraph | nx.DiGraph) -> dict:
    """
    Config of visual elements for the graph. Currently user designed for two types of graphs: G1 and G2.

    Args:
        config_type (str): Type of the configuration to generate, either "G1" or "G2".
        G (Union[nx.MultiDiGraph, nx.DiGraph]): The graph object.

    Returns:
        Dict, return html's config {"node_color_map":"...", "node_shape_map":"...", ...}
    """
    config = {}
    node_types = set()
    for node in G.nodes(data=True):
        node_types.add(node[1]["type"])

    colors = generate_color(len(node_types))
    node_color_map = {node_type: colors[i] for i, node_type in enumerate(node_types)}
    node_shape_map = {}
    for node_type in node_types:
        if node_type.lower() in ["file", "software", "platform", "tool"]:
            node_shape_map[node_type] = "box"
        else:
            node_shape_map[node_type] = "dot"

    node_size_map = {"dot": 20, "box": 25}
    config["node_color_map"] = node_color_map
    config["node_shape_map"] = node_shape_map
    config["node_size_map"] = node_size_map
    edge_types = set()
    potential_types = set()
    potential_color = "rgba(152, 154, 154, 1)"
    for edge in G.edges(data=True):
        if "relationship" in edge[2]:
            edge_types.add(edge[2]["relationship"])
        else:
            potential_types.add(edge[2]["potential_relationship"])

    edge_colors = generate_color(len(edge_types))
    edge_color_map = {edge_type: edge_colors[i] for i, edge_type in enumerate(edge_types)}
    potential_color_map = dict.fromkeys(potential_types, potential_color)
    config["edge_color_map"] = edge_color_map
    config["edge_length"] = 180 if config_type == "G1" else 100
    config["potential_color_map"] = potential_color_map
    return config


def parse_node(node_attr: dict, node_title: str) -> str:
    """
    Parse specified node title from row.

    Args:
        node_title_type (str): Type of the node title to parse, one of the following ("metadata", "tool", "kb").
        row (pd.Series): The row data from the DataFrame.
        node_title (str): The title of the node to parse.

    Returns:
        Str, parsed node title from row.
    """
    for attr_name, value in node_attr.items():
        if attr_name in ["id", "type", "label", "description", "path"] or pd.isna(value):
            continue

        parsed_data = {}
        try:
            if isinstance(value, str):
                parsed_data = json.loads(value.replace("'", '"'))
            elif isinstance(value, dict):
                parsed_data = value
        except Exception:
            node_title += f"\n {attr_name.replace('_', ' ').title()}: {value}"
            continue

        if parsed_data:
            node_title += f"\n {attr_name.replace('_', ' ').title()}:"
            try:
                for k, v in parsed_data.items():
                    node_title += f"\n  - {k}: {v}"
            except Exception:
                node_title += f" {parsed_data}"

    return node_title


def add_legend(node_color_map: dict, node_shape_map: dict, edge_color_map: dict, potential_color_map: dict):
    """
    Add legend HTML for the graph visualization.

    Args:
        node_color_map (dict): Mapping of node types to colors.
        node_shape_map (dict): Mapping of node types to shapes.
        edge_color_map (dict): Mapping of edge types to colors.
        potential_color_map (dict): Mapping of potential edge types to colors.

    Returns:
        Str, legend HTML string.
    """
    # add legend
    legend_html = """
    <div id="network-legend"
        style="position: absolute; top: 10px; right: 10px;
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid #ccc; border-radius: 5px;
                padding: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.2);
                z-index: 1000;">
        <div style="margin-bottom: 8px; font-weight: bold;">节点类型：</div>
    """

    # nodes legend
    for node_type, color in node_color_map.items():  # type: ignore
        shape = node_shape_map.get(node_type, "dot")  # type: ignore
        border_radius = "border-radius: 50%;" if shape == "dot" else ""
        shape_html = f"""<div style="width: 20px; height: 20px; {border_radius}
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
    relationships = [(rel, color, False) for rel, color in edge_color_map.items()]  # type: ignore
    relationships += [(rel, color, True) for rel, color in potential_color_map.items()]  # type: ignore
    potential_legend = False
    for relationship, color, is_potential in relationships:
        if is_potential and not potential_legend:
            legend_html += """
            <div style="margin-top: 15px; margin-bottom: 8px; font-weight: bold;">潜在关系：</div>
        """
            potential_legend = True

        dash = ' stroke-dasharray="5,3"' if relationship == "is_joinable_with" else ""
        legend_html += f"""
        <div style="display: flex; align-items: center; margin-bottom: 8px;">
            <div style="width: 50px; display: flex; justify-content: center; align-items: center;">
                <svg width="40" height="10">
                    <line x1="0" y1="5" x2="30" y2="5" stroke="{color}" stroke-width="2"{dash}></line>
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


def graph_to_html(config: dict, G: nx.MultiDiGraph | nx.DiGraph, output_html: str) -> None:
    """
    Convert pandas DataFrames to an interactive HTML visualization using pyvis.

    Args:
        config (dict): Configuration for the visualization.
        G (Union[nx.MultiDiGraph, nx.DiGraph]): The graph object.
        output_html (str): Path to save HTML file.
    """
    net = Network(height="800px", width="100%", bgcolor="#f9f9f9", font_color="black", directed=True)  # type: ignore
    net.barnes_hut(
        gravity=-25000, central_gravity=-1500, spring_length=500, spring_strength=0.03, damping=0.09, overlap=0
    )

    node_color_map: dict = config.get("node_color_map", {})
    node_shape_map: dict = config.get("node_shape_map", {})
    node_size_map: dict = config.get("node_size_map", {})
    nodes = G.nodes(data=True)
    for node_id, node_attr in nodes:
        node_label = node_attr.get("label")
        node_type = node_attr.get("type")
        if node_type == "file" and isinstance(node_label, str):
            node_label = node_label.split("/")[-1]
        node_title = f"Label: {node_label}\n Type: {node_type} \n Description: {node_attr.get('description', '')}"
        node_title = parse_node(node_attr, node_title)
        node_title += f"\n Path: {node_attr.get('path', '')}"
        node_shape = node_shape_map.get(node_type, "dot")
        net.add_node(
            node_id,
            label=node_label,
            title=node_title,
            color=node_color_map.get(node_type, "rgb(201,201,201)"),
            shape=node_shape,
            size=node_size_map.get(node_shape, 20),
            font={"size": 16, "face": "Arial", "bold": True},
        )

    edge_color_map: dict = config.get("edge_color_map", {})
    potential_color_map: dict = config.get("potential_color_map", {})
    edges = G.edges(data=True)
    for source, target, edge_attr in edges:
        relationship = edge_attr.get("relationship")
        if relationship:
            edge_title = f"Relationship: {relationship}"
            edge_color = edge_color_map.get(relationship) if isinstance(edge_color_map, dict) else "black"
        else:
            relationship = edge_attr.get("potential_relationship")
            edge_title = f"Potential Relationship: {relationship}"
            edge_color = potential_color_map.get(relationship) if isinstance(potential_color_map, dict) else "black"
        edge_title += f"\n Description: {relationship} "
        net.add_edge(
            source,
            target,
            title=edge_title,
            color=edge_color,
            arrows="to",
            smooth={"type": "curvedCW", "roundness": 0.2} if relationship == "is_joinable_with" else True,
            width=3 if relationship == "is_joinable_with" else 2,
            length=config.get("edge_length", 100),
            dashes=relationship == "is_joinable_with",
        )

    net.set_options("""
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
    """)
    legend_html = add_legend(node_color_map, node_shape_map, edge_color_map, potential_color_map)

    net.save_graph(output_html)
    with open(output_html, encoding="utf-8") as f:
        html_content = f.read()
    html_content = re.sub("(<body.*?>)", r"\1" + legend_html, html_content)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html_content)
