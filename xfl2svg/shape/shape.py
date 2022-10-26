"""Convert the XFL <DOMShape> element to SVG <path> elements."""

from collections import defaultdict
import copy
from functools import reduce
import json
from sre_parse import expand_template
import warnings
import xml.etree.ElementTree as ET

from xfl2svg.shape.edge import xfl_domshape_to_edges, xfl_domshape_to_visible_edges
from xfl2svg.shape.style import parse_fill_style, parse_stroke_style, parse_json_style
from xfl2svg.util import Traceable


def svg_normalize_style(d):
    """Expand out any Traceable items in a style dict to use in SVG elements."""
    result = {}
    extra_defs = {}

    for key, value in d.items():
        if isinstance(value, Traceable):
            extra_defs[value.id] = value.to_svg()
            result[key] = f"url(#{value.id})"
        else:
            result[key] = value

    return result, extra_defs


# This function converts point lists into the SVG path format.
def path_to_svg_format(point_list: list) -> str:
    """Convert a point list into the SVG path format."""
    point_iter = iter(point_list)
    path = ["M"]
    points = []
    last_command = "M"

    def append_point(pt):
        path.append(f"{pt[0]} {pt[1]}")

    append_point(next(point_iter))

    try:
        while True:
            point = next(point_iter)
            command = "Q" if isinstance(point[0], tuple) else "L"
            # SVG lets us omit the command letter if we use the same command
            # multiple times in a row.
            if command != last_command:
                path.append(command)
                last_command = command

            if command == "Q":
                # Append control point and destination point
                append_point(point[0])
                append_point(next(point_iter))
            else:
                append_point(point)
    except StopIteration:
        if point_list[0] == point_list[-1]:
            # Animate adds a "closepath" (Z) command to every filled shape and
            # closed stroke. For shapes, it makes no difference, but for closed
            # strokes, it turns two overlapping line caps into a bevel, miter,
            # or round join, which does make a difference.
            # TODO: It is likely that closed strokes can be broken into
            # segments and spread across multiple Edge elements, which would
            # require a function like point_lists_to_shapes(), but for strokes.
            # For now, though, adding "Z" to any stroke that is already closed
            # seems good enough.
            # path.append("Z")
            pass
        return " ".join(path)


# We can convert edges (segment path + color) into SVG <path> elements. The algorithm
# works as follows:
#
#   For filled shapes:
#     * For a given edge, process each of its segments:
#         * If the edge has left fill, associate the fill style ID
#           ("index" in XFL) with the segment.
#         * If the edge has right fill, associate the ID with the segment,
#           reversed. This way, the fill of the shape is always to the left of
#           the segment (arbitrary choice--the opposite works too).
#     * For each fill style ID, consider its segments:
#         * Create a graph of the associated paths, letting each path be
#           represented by a vertex. Connect paths with a directed edge
#           if they can be composed.
#         * Find a cycle. This can be done with a modified spanning tree
#           algorithm. Normally, a spanning tree algorithm stops when all
#           vertices are reached. Instead, stop the algorithm when the root
#           node is reached. Mark all vertices in the cycle as "covered".
#         * Continue finding cycles rooted in uncovered vertices until all
#           vertices have been covered.
#
#   For stroked paths:
#     * Pair up segments with their stroke style IDs. There is only one
#       "strokeStyle" attribute, so we don't need to reverse any segments.
#     * Use all paths directly. There's no need to split them into groups.
#
# The PathGraph class implements the algorithm to find covering cycles and paths.
# the ShapeGraph class collects paths by their fill and stroke id.#
#
# Assumptions:
#   * There are enough cycles to cover all paths.
#   * All sets of covering cycles are equivalent. It doesn't matter which ones
#     we find.
#
# Notes:
#   * For stroked paths, Animate joins together segments by their start/end
#     points. But, this isn't necessary: when converting to the SVG path
#     format, each segment starts with a "move to" command, so they can be
#     concatenated in any order.
#   * For filled shapes, there is usually only one choice for the next point
#     list. The only time there are multiple choices is when multiple shapes
#     share a point:
#
#               +<-----+
#      Shape 1  |      ^
#               v      |
#               +----->o<-----+
#                      |      ^  Shape 2
#                      v      |
#                      +----->+


class ShapeGraph:
    def __init__(self):
        self.fills = defaultdict(PathGraph)
        self.strokes = defaultdict(PathGraph)

    def add_edge(self, path, fill_left, fill_right, stroke):
        if fill_left != None:
            self.fills[fill_left].add(path)

        if fill_right != None:
            self.fills[fill_right].add(path[::-1])

        if stroke != None:
            self.strokes[stroke].add(path)

    def get_fills(self):
        for fill_id, g in self.fills.items():
            point_lists = []
            for cycle in g.covering_cycles():
                next_pl = []
                for path in cycle:
                    next_pl.extend(path)
                point_lists.append(next_pl)
            yield fill_id, point_lists

    def get_strokes(self):
        for stroke_id, g in self.strokes.items():
            yield stroke_id, g.covering_paths()


class PathGraph:
    """This class represents a graph of paths.

    Each path (tuple of points and control points) is represented as a vertex.
    There exists an PathGraph edge from A to B if A ends where B starts.

    This class is used to find a set of cycles that covers all given paths.
    """

    def __init__(self):
        # Standard graph data
        self.vertices = set()
        self.paths = defaultdict(set)

        # Vertices "behind" a given target node
        self.tails = defaultdict(set)
        # Vertices "in front of" a given source node
        self.heads = defaultdict(set)

    def add(self, path=None):
        source = path[0]
        target = path[-1]

        self.vertices.add(path)

        self.heads[source].add(path)
        self.tails[target].add(path)

        for incoming in self.tails[source]:
            self.paths[incoming].add(path)

        for outgoing in self.heads[target]:
            self.paths[path].add(outgoing)

    def get_cycle(self, v):
        """Find a cycle by building a spanning tree.

        This function builds a spanning tree rooted in vertex v until it hits v again. It
        then returns the discovered path from v to v.
        """
        parents = {}
        pending = set()

        for child in self.paths[v]:
            parents[child] = v
            pending.add(child)

        while pending:
            curr_vertex = pending.pop()
            if curr_vertex == v:
                break

            for child in self.paths[curr_vertex]:
                if child in parents:
                    continue
                parents[child] = curr_vertex
                pending.add(child)

        if v not in parents:
            # Exhausted all possibilities without finding a cycle.
            return []

        result = [v]
        next_node = parents[v]
        while next_node != v:
            result.insert(0, next_node)
            next_node = parents[next_node]

        return result

    def covering_cycles(self):
        # Make sure every path (vertex in the PathGraph) gets used at least once
        pending = self.vertices.copy()

        while pending:
            start = pending.pop()
            cycle = self.get_cycle(start)
            if not cycle:
                continue

            yield cycle
            for v in cycle:
                if v in pending:
                    pending.remove(v)

    def covering_paths(self):
        return self.vertices


# When all segments have been joined into shapes and converted,
# concatenate the path strings and put them in *one* SVG <path>
# element per fill or stroke. (This ensures that holes work correctly.)
# Finally, look up the fill attributes from the ID and assign them to
# the <path>. This is done by shape_graph_to_svg.


def shape_graph_to_svg(shape, fill_styles, stroke_styles):
    fills = {}
    strokes = {}
    extra_defs = {}
    fill_paths = []
    stroke_paths = []
    bbox = None
    all_paths = []

    def require_fill(index):
        # Get the SVG Element-compatible fill data for an index.
        if index not in fills:
            fill, defs = svg_normalize_style(fill_styles[index])

            # Add a hairwidth stroke around fills to avoid gaps between shapes
            if "fill" in fill:
                fill["stroke"] = fill["fill"]
                fill["stroke-width"] = "0.05"
                if "fill-opacity" in fill:
                    fill["stroke-opacity"] = fill["fill-opacity"]

            fills[index] = fill
            extra_defs.update(defs)
        return fills[index]

    def require_stroke(index):
        # Get the SVG Element-compatible stroke data for an index.
        if index not in strokes:
            strokes[index], defs = svg_normalize_style(stroke_styles[index])
            extra_defs.update(defs)
        return strokes[index]

    for fill_id, paths in shape.get_fills():
        paths = list(paths)
        if not paths:
            continue

        fill_style = require_fill(fill_id)
        path = ET.Element("path", fill_style)
        path.set("d", " ".join(path_to_svg_format(pl) for pl in paths))
        fill_paths.append(path)

        all_paths.extend((x, 0) for x in paths)

    for stroke_id, paths in shape.get_strokes():
        paths = list(paths)
        if not paths:
            continue

        stroke_style = require_stroke(stroke_id)
        stroke_width = float(stroke_style.get("stroke-width", 1))
        stroke = ET.Element("path", stroke_style)
        stroke.set("d", " ".join(path_to_svg_format(pl) for pl in paths))
        stroke_paths.append(stroke)

        all_paths.extend((x, stroke_width) for x in paths)

    fill_g = None
    if fill_paths:
        fill_g = ET.Element("g")
        fill_g.extend(fill_paths)

    stroke_g = None
    if stroke_paths:
        # Animate directly dumps all stroked <path>s into <defs>, but it's
        # cleaner to wrap them in a <g> like it does for filled paths.
        stroke_g = ET.Element("g")
        stroke_g.extend(stroke_paths)

    return fill_g, stroke_g, extra_defs, all_paths


def xfl_domshape_to_styles(domshape):
    fill_styles = {}
    for style in domshape.iterfind(".//{*}FillStyle"):
        index = style.get("index")
        fill_styles[index] = parse_fill_style(style[0])

    stroke_styles = {}
    for style in domshape.iterfind(".//{*}StrokeStyle"):
        index = style.get("index")
        stroke_styles[index] = parse_stroke_style(style[0])

    return fill_styles, stroke_styles


def xfl_domshape_to_svg(domshape, mask=False):
    """Convert the XFL <DOMShape> element to SVG <path> elements.

    Args:
        domshape: An XFL <DOMShape> element
        mask: If True, all fill colors will be set to #FFFFFF. This ensures
              that the resulting mask is fully transparent.

    Returns a 4-tuple of:
        SVG <g> element containing filled <path>s
        SVG <g> element containing stroked <path>s
        dict of extra elements to put in <defs> (e.g. filters and gradients)
        bounding box [left, bottom, right, top] of the elements
    """

    if mask:
        # TODO: Figure out how strokes are supposed to behave in masks
        fill_styles = defaultdict(lambda: {"fill": "#FFFFFF", "stroke": "none"})
        stroke_styles = defaultdict(lambda: {"fill": "#FFFFFF", "stroke": "none"})
        shape_edges = xfl_domshape_to_edges(domshape)
    else:
        fill_styles, stroke_styles = xfl_domshape_to_styles(domshape)
        shape_edges = xfl_domshape_to_visible_edges(
            domshape, fill_styles, stroke_styles
        )

    shape = ShapeGraph()
    for edge in shape_edges:
        shape.add_edge(*edge)

    return shape_graph_to_svg(shape, fill_styles, stroke_styles)


def json_normalize_style(d):
    """Expand out any Traceable items in a style dict to use in a JSON object."""
    result = {}

    for key, value in d.items():
        if isinstance(value, Traceable):
            result[key] = value.to_dict()
        else:
            result[key] = value

    return result


def json_normalize_path(path):
    result = []
    for point in path:
        control = isinstance(point[0], tuple)
        if control:
            point = point[0]

        result.append({"point": list(point), "control": control})

    return result


def json_normalize_xfl_domshape(domshape, mask=False):
    result = {"mask": mask}
    if mask:
        # TODO: Figure out how strokes are supposed to behave in masks
        shape_edges = xfl_domshape_to_edges(domshape)
    else:
        fill_styles, stroke_styles = xfl_domshape_to_styles(domshape)

        result["fill_styles"] = result_fills = {}
        for index, style in fill_styles.items():
            result_fills[index] = json_normalize_style(style)

        result["stroke_styles"] = result_strokes = {}
        for index, style in stroke_styles.items():
            result_strokes[index] = json_normalize_style(style)

        shape_edges = xfl_domshape_to_visible_edges(
            domshape, fill_styles, stroke_styles
        )

    result["shape"] = shape = []
    for edge in shape_edges:
        path, fill_left, fill_right, stroke = edge

        edge_data = {"path": json_normalize_path(path)}
        if fill_left:
            edge_data["fill_left"] = fill_left
        if fill_right:
            edge_data["fill_right"] = fill_right
        if stroke:
            edge_data["stroke"] = stroke

        shape.append(edge_data)

    return result


def dict_shape_to_svg(data):
    mask = data["mask"]
    shape = ShapeGraph()

    if mask:
        fill_styles = defaultdict(lambda: {"fill": "#FFFFFF", "stroke": "none"})
        stroke_styles = defaultdict(lambda: {"fill": "#FFFFFF", "stroke": "none"})
    else:
        fill_styles = {}
        for index, style in data["fill_styles"].items():
            fill_styles[index] = parse_json_style(style)

        stroke_styles = {}
        for index, style in data["stroke_styles"].items():
            stroke_styles[index] = parse_json_style(style)

    for edge in data["shape"]:
        path = []
        for point in edge["path"]:
            coord = tuple(point["point"])
            if point["control"]:
                path.append((coord,))
            else:
                path.append(coord)

        fill_left = edge.get("fill_left", None)
        fill_right = edge.get("fill_right", None)
        stroke = edge.get("stroke", None)
        shape.add_edge(tuple(path), fill_left, fill_right, stroke)

    return shape_graph_to_svg(shape, fill_styles, stroke_styles)
