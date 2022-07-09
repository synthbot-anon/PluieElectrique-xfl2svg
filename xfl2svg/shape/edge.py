"""Convert XFL edges to SVG paths.

If you just want to convert, use `xfl_edge_to_svg_path()`. If you're interested
in how everything works, read on.
"""

# Read these links first, as there is no official documentation for the XFL
# edge format:
#
#   * https://github.com/SasQ/SavageFlask/blob/master/doc/FLA.txt
#   * https://stackoverflow.com/a/4077709
#
# Overview:
#
#    In Animate, graphic symbols are made of filled shapes and stroked paths.
#    Both are defined by their outline, which Animate breaks into pieces. We'll
#    call such a piece a "segment", rather than an "edge", to avoid confusion
#    with the edge format.
#
#    A segment may be part of up to two shapes: one on its left and one on its
#    right. This is determined by the presence of the "fillStyle0" (left) and
#    "fillStyle1" (right) attributes, which specify the style for the shape on
#    that side.
#
#    A segment may be part of up to one stroked path. This is determined by the
#    presence of the "strokeStyle" attribute.
#
#    So, to extract graphic symbols from XFL, we first convert the edge format
#    into segments (represented as point lists, see below). Each <Edge> element
#    produces one or more segments, each of which inherits the <Edge>'s
#    "fillStyle0", "fillStyle1", and "strokeStyle" attributes.
#
#    Then, for filled shapes, we join segments of the same fill style by
#    matching their start/end points. The fill styles must be for the same
#    side. For stroked paths, we just collect all segments of the same style.
#
#    Finally, we convert segments to the SVG path format, put them in an SVG
#    <path> element, and assign fill/stroke style attributes to the <path>.


from collections import defaultdict, OrderedDict
from dataclasses import dataclass
from email.policy import default
import math
import re
from typing import Dict, Iterator, List, Set, Tuple
import warnings
import xml.etree.ElementTree as ET

from xfl2svg.util import merge_bounding_boxes


# The XFL edge format can be described as follows:
#
#   start  : moveto (moveto | lineto | quadto)*
#   moveto : "!" NUMBER ~ 2 select?             // Move to this point
#   lineto : ("|" | "/") NUMBER ~ 2             // Line from current point to here
#   quadto : ("[" | "]") NUMBER ~ 4             // Quad Bézier (control point, dest)
#   select : /S[1-7]/                           // Only used by Animate
#   NUMBER : /-?\d+(\.\d+)?/                    // Decimal number
#          | /#[A-Z0-9]{1,6}\.[A-Z0-9]{1,2}/    // Signed, 32-bit number in hex
#   %import common.WS                           // Ignore whitespace
#   %ignore WS
#
# Notes:
#  * This grammar is written for use with Lark, a Python parsing toolkit. See:
#      * Project page:  https://github.com/lark-parser/lark
#      * Try it online: https://www.lark-parser.org/ide/
#  * The cubic commands are omitted:
#      * They only appear in the "cubics" attribute and not in "edges"
#      * They're just hints for Animate and aren't needed for conversion to SVG
#  * "select" is also just a hint for Animate, but it appears in "edges", so we
#    include it for completeness.
#
# Anyhow, this language can actually be tokenized with a single regex, which is
# faster than using Lark:

EDGE_TOKENIZER = re.compile(
    r"""
[!|/[\]]                |   # Move to, line to, quad to
(?<!S)-?\d+(?:\.\d+)?   |   # Decimal number
\#[A-Z0-9]+\.[A-Z0-9]+      # Hex number
""",
    re.VERBOSE,
)

# Notes:
#   * Whitespace is automatically ignored, as we only match what we want.
#   * The negative lookbehind assertion (?<!S) is needed to avoid matching the
#     digit in select commands as a number.


# After tokenizing, we need to parse numbers:


def parse_number(num: str) -> float:
    """Parse an XFL edge format number."""
    if num[0] == "#":
        # Signed, 32-bit fixed-point number in hex
        parts = num[1:].split(".")
        # Pad to 8 digits
        hex_num = "{:>06}{:<02}".format(*parts)
        num = int.from_bytes(bytes.fromhex(hex_num), "big", signed=True)
        # Account for hex scaling and Animate's 20x scaling (twips)
        return (num / 256) / 20
    else:
        # Decimal number. Account for Animate's 20x scaling (twips)
        return float(num) / 20


def line_bounding_box(p1, p2):
    return (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))


def quadratic_bezier(p1, p2, p3, t):
    x = (1 - t) * ((1 - t) * p1[0] + t * p2[0]) + t * ((1 - t) * p2[0] + p3[0])
    y = (1 - t) * ((1 - t) * p1[1] + t * p2[1]) + t * ((1 - t) * p2[1] + p3[1])
    return (x, y)


def quadratic_critical_points(p1, p2, p3):
    x_denom = p1[0] - 2 * p2[0] + p3[0]
    if x_denom == 0:
        x_crit = math.inf
    else:
        x_crit = (p1[0] - p2[0]) / x_denom

    y_denom = p1[1] - 2 * p2[1] + p3[1]
    if y_denom == 0:
        y_crit = math.inf
    else:
        y_crit = (p1[1] - p2[1]) / y_denom

    return x_crit, y_crit


def quadratic_bounding_box(p1, control, p2):
    t3, t4 = quadratic_critical_points(p1, control, p2)

    if t3 > 0 and t3 < 1:
        p3 = quadratic_bezier(p1, control, p2, t3)
    else:
        # Pick either the start or end of the curve arbitrarily so it doesn't affect
        # the max/min point calculation
        p3 = p1

    if t4 > 0 and t4 < 1:
        p4 = quadratic_bezier(p1, control, p2, t4)
    else:
        # Pick either the start or end of the curve arbitrarily so it doesn't affect
        # the max/min point calculation
        p4 = p1

    return (
        min(p1[0], p2[0], p3[0], p4[0]),
        min(p1[1], p2[1], p3[1], p4[1]),
        max(p1[0], p2[0], p3[0], p4[0]),
        max(p1[1], p2[1], p3[1], p4[1]),
    )


# Notes:
#   * The <path>s produced by Animate's SVG export sometimes have slightly
#     different numbers (e.g. flooring or subtracting 1 from decimals before
#     dividing by 20). It's not clear how this works or if it's even intended,
#     so I gave up trying to replicate it.
#   * Animate prints round numbers as integers (e.g. "1" instead of "1.0"), but
#     it makes no difference for SVG.


# Now, we can parse the edge format. To join segments into shapes, though, we
# will need a way to reverse segments (for normalizing them so that the filled
# shape is always on the left). That is, if we have a segment like:
#
#                C
#              /   \
#             |     |
#    A ----- B       D ----- E
#
# which is represented by:
#
#    moveto A, lineto B, quadto C D, lineto E
#
# We should be able to reverse it and get:
#
#    moveto E, lineto D, quadto C B, lineto A
#
# The "point list" format (couldn't think of a better name) meets this
# requirement. The segment above would be represented as:
#
#    [A, B, (C,), D, E]
#
# The first point is always the destination of a "move to" command. Subsequent
# points are the destinations of "line to" commands. If a point is in a tuple
# like `(C,)`, then it's the control point of a quadratic Bézier curve, and the
# following point is the destination of the curve. (Tuples are just an easy way
# to mark points--there's nothing particular about the choice.)
#
# With this format, we can see that reversing the list gives us the same
# segment, but in reverse:
#
#    [E, D, (C,), B, A]
#
# In practice, each point is represented as a coordinate string, so the actual
# point list might look like:
#
#   ["0 0", "10 0", ("20 10",), "30 0", "40 0"]
#
# This next function converts the XFL edge format into point lists. Since each
# "edges" attribute can contain multiple segments, but each point list only
# represents one segment, this function can yield multiple point lists.


def edge_format_to_point_lists(edges: str) -> Iterator[list]:
    """Convert the XFL edge format to point lists.

    Args:
        edges: The "edges" attribute of an XFL <Edge> element

    Yields:
        One point list for each segment parsed out of `edges`
    """
    tokens = iter(EDGE_TOKENIZER.findall(edges))
    point_list = []
    bounding_box = None

    def next_point():
        x = parse_number(next(tokens))
        y = parse_number(next(tokens))
        return x, y

    assert next(tokens) == "!", "Edge format must start with moveto (!) command"

    prev_point = next_point()

    try:
        while True:
            command = next(tokens)
            curr_point = next_point()

            if command == "!":
                # Move to
                if curr_point != prev_point:
                    # If a move command doesn't change the current point, we
                    # ignore it. Otherwise, a new segment is starting, so we
                    # must yield the current point list and begin a new one.
                    yield point_list, bounding_box
                    prev_point = curr_point
                    bounding_box = None
            elif command in "|/":
                # Line to
                point_list.append(f"{prev_point[0]} {prev_point[1]}")
                point_list.append(f"{curr_point[0]} {curr_point[1]}")
                bounding_box = merge_bounding_boxes(
                    bounding_box, line_bounding_box(prev_point, curr_point)
                )
                prev_point = curr_point
            else:
                # Quad to. The control point (curr_point) is marked by putting
                # it in a tuple.
                end_point = next_point()
                point_list.append(f"{prev_point[0]} {prev_point[1]}")
                point_list.append((f"{curr_point[0]} {curr_point[1]}",))
                point_list.append(f"{end_point[0]} {end_point[1]}")
                bounding_box = merge_bounding_boxes(
                    bounding_box,
                    quadratic_bounding_box(prev_point, curr_point, end_point),
                )
                prev_point = end_point
    except StopIteration:
        yield point_list, bounding_box
        bounding_box = None


# Finally, we can convert XFL <Edge> elements into SVG <path> elements. The
# algorithm works as follows:

#   First, convert the "edges" attributes into segments. Then:
#
#   For filled shapes:
#     * For a given <Edge>, process each of its segments:
#         * If the <Edge> has "fillStyle0", associate the fill style ID
#           ("index" in XFL) with the segment.
#         * If the <Edge> has "fillStyle1", associate the ID with the segment,
#           reversed. This way, the fill of the shape is always to the left of
#           the segment (arbitrary choice--the opposite works too).
#     * For each fill style ID, consider its segments:
#         * Pick an unused segment. If it's already closed (start point equals
#           end point), convert it to the SVG path format.
#         * Otherwise, if it's open, randomly append segments (making sure to
#           match start and end points) until:
#             1. The segment is closed. Convert and start over with a new,
#                unused segment.
#             2. The segment intersects with itself (i.e. the current end point
#                equals the end point of a previous segment). Backtrack.
#             3. There are no more valid segments. Backtrack.
#         * When all segments have been joined into shapes and converted,
#           concatenate the path strings and put them in *one* SVG <path>
#           element. (This ensures that holes work correctly.) Finally, look up
#           the fill attributes from the ID and assign them to the <path>.
#
#   For stroked paths:
#     * Pair up segments with their stroke style IDs. There is only one
#       "strokeStyle" attribute, so we don't need to reverse any segments.
#     * For each stroke style ID, convert its segments into the SVG path
#       format. Concatenate all path strings and put them in an SVG <path>
#       element. Look up the stroke attributes and assign them to the <path>.
#
#
# This algorithm is split across the next two functions:
#   * `point_lists_to_shapes()` joins point lists into filled shapes.
#   * `xfl_edge_to_svg_path()` does everything else.
#
#
# Assumptions:
#   * Segments never cross. So, we only need to join them at their ends.
#   * For filled shapes, there's only one way to join segments such that no
#     segment is left out. So, we don't need to worry about making the wrong
#     decision when there are multiple segments to pick from.
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



class FasterList:
    def __init__(self):
        super().__init__()
        self.backing = []
        self.removed = defaultdict(lambda: 0)
        self.counts = defaultdict(lambda: 0)
        self.index = 0
        self.length = 0

    def append(self, item):
        self.backing.append(item)
        self.counts[item] += 1
        self.length += 1

    def remove(self, item):
        if self.counts[item]:
            self.removed[item] += 1
            self.counts[item] -= 1
            self.length -= 1

    def pop(self):
        result = self.backing[self.index]
        while self.removed[result]:
            self.removed[result] -= 1
            self.index += 1
            result = self.backing[self.index]
        self.counts[result] -= 1
        self.index += 1
        self.length -= 1
        return result

    def extend(self, items):
        for i in items:
            self.counts[i] += 1
        self.backing.extend(items)
        self.length += len(items)

    def __iter__(self):
        rem = self.removed.copy()
        for item in self.backing:
            if rem[item]:
                rem[item] -= 1
                continue
            yield item

    def __contains__(self, item):
        return self.counts[item] > 0

    def __len__(self):
        return self.length
    

class CircularList(list):
    def __init__(self):
        super().__init__()
        self.index = 0
        self.limit = 0
        self.count = 0
    
    def append(self, item):
        super().append(item)
        self.limit += 1
        self.count += 1

    def pop(self):
        if self.index >= self.limit:
            raise IndexError()

        result = self[self.index % len(self)]
        self.index += 1
        return result
    
    def raise_limit(self, count):
        self.limit += count



# finds cycles greedily
def point_lists_to_shapes(point_lists: List[Tuple[list, str]]) -> Dict[str, List[list]]:
    """Join point lists and fill style IDs into shapes.

    Args:
        point_lists: [(point_list, fill style ID), ...]

    Returns:
        {fill style ID: [shape point list, ...], ...}
    """
    graph = defaultdict(lambda: defaultdict(CircularList))
    shapes = defaultdict(list)
    points = defaultdict(FasterList)

    # The SVG is sensitive about the exact cycles used to create shapes. I don't know
    # why. It could be because of some issue with control points. If that's true, then
    # this code can be vastly simplified by using the get_cycles() function below after
    # figuring out how to make it handle control points properly.
    
    # Without the tree algorithm, it seems to work to try checking paths greedily, as
    # long as the path graph is set up properly. The order of vertices and edges
    # checked both matter.

    for point_list, fill_id in point_lists[::-1]:
        for source, target in zip(point_list[:-1], point_list[1:]):
            graph[fill_id][source].append(target)
        for point in point_list:
            # Ignore control points since we don't want paths to start or end with
            # them. They'll automatically get added to paths if they're needed.
            if not isinstance(point, tuple):
                points[fill_id].append(point)

    for fill_id in points:
        unused_edges = graph[fill_id]
        pending_assignments = points[fill_id]
        generated_shapes = shapes[fill_id]

        unused_points = set(pending_assignments)

        # Make sure all non-control points get assigned to at least one shape.
        while pending_assignments:
            start = pending_assignments.pop()

            if not unused_edges[start]:
                continue

            edge = unused_edges[start].pop()
            next_shape = [start, edge]
            # Keep track of non-start points found while looking for a shape so we
            # can undo if needed
            discovered_points = OrderedDict()

            try:
                while edge != start:
                    if edge in pending_assignments:
                        # Mark this point as assigned to a shape.
                        pending_assignments.remove(edge)
                        discovered_points[edge] = None
                    
                    edge = unused_edges[edge].pop()
                    next_shape.append(edge)
                
                generated_shapes.append(next_shape)
                unused_points.difference_update(next_shape)
            except IndexError:
                # Undo since we failed to find a cycle. Don't append the start since
                # we know it's a bad starting point.
                pending_assignments.extend(discovered_points.keys())
            
            for source, target in zip(next_shape[:-1], next_shape[1:]):
                unused_edges[source].raise_limit(1)
        
        if unused_points:
            warnings.warn("Failed to assign all points to a shape")
            print('failed on', len(unused_points), 'points')

    return shapes




# finds cycles using spanning trees
# def point_lists_to_shapes(point_lists: List[Tuple[list, str]]) -> Dict[str, List[list]]:
#     """Join point lists and fill style IDs into shapes.

#     Args:
#         point_lists: [(point_list, fill style ID), ...]

#     Returns:
#         {fill style ID: [shape point list, ...], ...}
#     """
#     graph = defaultdict(lambda: defaultdict(list))
#     shapes = defaultdict(list)
#     points = defaultdict(set)
#     controls = defaultdict(dict)

#     for point_list, fill_id in point_lists:
#         i = 0
#         while i < len(point_list)-1:
#             source = point_list[i]
#             if isinstance(point_list[i+1], tuple):
#                 control = point_list[i+1]
#                 target = point_list[i+2]
#                 print('setting control point')
#                 controls[fill_id][(source, target)] = control
#                 i += 2
#             else:
#                 target = point_list[i+1]
#                 i += 1
            
#             graph[fill_id][source].append(target)
#             points[fill_id].add(source)
#             points[fill_id].add(target)

#     for fill_id in points:
#         unused_edges = graph[fill_id]
#         unused_points = points[fill_id]
#         generated_shapes = shapes[fill_id]

#         # Make sure all non-control points get assigned to at least one shape.
#         while unused_points:
#             start = unused_points.pop()
#             cycle = get_cycle(graph[fill_id], start)
#             shape = []
#             for source, target in zip(cycle[:-1], cycle[1:]):
#                 shape.append(source)
#                 if (source, target) in controls[fill_id]:
#                     print('found control point')
#                     shape.append(controls[fill_id][(source, target)])
#             shape.append(target)

#             unused_points.difference_update(cycle)
#             generated_shapes.append(shape)



#     return shapes


# @dataclass(frozen=False)
# class SCCParams:
#     components: Set
#     s: List
#     p: List
#     counter: int
#     preorders: Dict
#     unassigned: Set
#     assignments: Dict

#     @classmethod
#     def new_instance(cls, graph):
#         unassigned = set(graph.keys())
#         for v in graph.values():
#             unassigned.update(v)
#         return cls(set(), [], [], 0, {}, unassigned)


# def graph_to_scc(graph, v=None, params=None):
#     if not graph:
#         return []
#     if params == None:
#         params = SCCParams.new_instance(graph)
#     if v == None:
#         while params.unassigned:
#             v = next(iter(params.unassigned))
#             graph_to_scc(graph, v, params)
#         return params.components, params.assignments
#     params.preorders[v] = params.counter
#     params.counter += 1
#     params.s.append(v)
#     params.p.append(v)
#     if v in graph:
#         for w in graph[v]:
#             if w not in params.preorders:
#                 graph_to_scc(graph, w, params)
#             elif w in params.unassigned:
#                 while params.preorders[params.p[-1]] > params.preorders[w]:
#                     params.p.pop()
#     if params.p[-1] == v:
#         new_component = set()
#         params.components.add(new_component)
#         while params.s[-1] != v:
#             next_vertex = params.s.pop()
#             params.unassigned.remove(next_vertex)
#             new_component.add(next_vertex)
#             params.assignments[next_vertex] = new_component
#         params.unassigned.remove(v)
#         new_component.add(params.s.pop())
#         params.assignments[v] = new_component
#         params.p.pop()



# def get_cycle(graph, v):
#     """ Find a cycle by building a spanning tree.

#     This function builds a spanning tree rooted in vertex v until it hits v again. It
#     then returns the discovered path from v to v.
#     """
#     parents = {}
#     pending = set()

#     for child in graph[v]:
#         parents[child] = v
#         pending.add(child)

#     while pending:
#         curr_vertex = pending.pop()
#         if curr_vertex == v:
#             break

#         for child in graph[curr_vertex]:
#             if child in parents:
#                 continue
#             parents[child] = curr_vertex
#             pending.add(child)
    
#     if v not in parents:
#         # Exhausted all possibilities without finding a cycle.
#         return []

#     result = [v]
#     next_node = parents[v]
#     while next_node != v:
#         result.insert(0, next_node)
#         next_node = parents[next_node]
    
#     return result


# class EdgeGraph:
#     """ This class represents a graph of edges.

#     Each edge is represented as a pair (source, target) with associated hashable data.
#     There exists an EdgeGraph edge from A to B is A's target is the same as B's source.

#     This class is used to find a set of cycles that covers all given edges.    
#     """

#     def __init__(self):
#         # Standard graph data
#         self.vertices = set()
#         self.edges = defaultdict(set)

#         # Vertices "behind" a given target node
#         self.tails = defaultdict(set)
#         # Vertices "in front of" a given source node
#         self.heads = defaultdict(set)

#     def add(self, source, target, data=None):
#         vertex = (source, target, data)
#         self.vertices.add(vertex)

#         self.heads[source].add(vertex)
#         self.tails[target].add(vertex)
        
#         for incoming in self.tails[source]:
#             self.edges[incoming].add(vertex)
        
#         for outgoing in self.heads[target]:
#             self.edges[vertex].add(outgoing)
        
    
#     def covering_cycles(self):
#         # Make sure every edge (vertex in the EdgeGraph) gets used at least once
#         pending = self.vertices.copy()

#         while pending:
#             start = pending.pop()
#             cycle = get_cycle(self.edges, start)
#             if not cycle:
#                 continue
            
#             yield cycle
#             for v in cycle:
#                 if v in pending:
#                     pending.remove(v)


# def point_lists_to_shapes(point_lists: List[Tuple[list, str]]) -> Dict[str, List[list]]:
#     """Join point lists and fill style IDs into shapes.

#     Args:
#         point_lists: [(point_list, fill style ID), ...]

#     Returns:
#         {fill style ID: [shape point list, ...], ...}
#     """
#     graphs = defaultdict(EdgeGraph)
#     shapes = defaultdict(list)
    
#     for point_list, fill_id in point_lists:
#         g = graphs[fill_id]
#         g.add(point_list[0], point_list[-1], tuple(point_list))

#     for fill_id, g in graphs.items():
#         for cycle in g.covering_cycles():
#             next_shape = []
#             for _, _, path in cycle:
#                 next_shape.extend(path)

#             shapes[fill_id].append(next_shape)

#     return shapes

        




def xfl_edge_to_shapes(
    edges_element: ET.Element,
    fill_styles: Dict[str, dict],
    stroke_styles: Dict[str, dict],
) -> Tuple[List[ET.Element], List[ET.Element]]:
    """Convert the XFL <edges> element into SVG <path> elements.

    Args:
        edges_element: The <edges> element of a <DOMShape>
        fill_styles: {fill style ID: style attribute dict, ...}
        stroke_styles: {stroke style ID: style attribute dict, ...}

    Returns a tuple of lists, each containing <path> elements:
        ([filled path, ...], [stroked path, ...])
    """
    fill_edges = []
    stroke_edges = []
    stroke_paths = defaultdict(list)
    fill_boxes = defaultdict(lambda: None)
    stroke_boxes = defaultdict(lambda: None)

    # Ignore the "cubics" attribute, as it's only used by Animate
    for edge in edges_element.iterfind(".//{*}Edge[@edges]"):
        edge_format = edge.get("edges")
        fill_id_left = edge.get("fillStyle0")
        fill_id_right = edge.get("fillStyle1")
        stroke_id = edge.get("strokeStyle")

        for point_list, bounding_box in edge_format_to_point_lists(edge_format):
            # Reverse point lists so that the fill is always to the left
            if fill_id_left is not None:
                fill_edges.append((point_list, fill_id_left))
                fill_boxes[fill_id_left] = merge_bounding_boxes(
                    fill_boxes[fill_id_left], bounding_box
                )
            if fill_id_right is not None:
                fill_edges.append((list(reversed(point_list)), fill_id_right))
                fill_boxes[fill_id_right] = merge_bounding_boxes(
                    fill_boxes[fill_id_right], bounding_box
                )

            # We don't need to join anything into shapes
            if stroke_id is not None and stroke_id in stroke_styles:
                stroke_paths[stroke_id].append(point_list)
                stroke_boxes[stroke_id] = merge_bounding_boxes(
                    stroke_boxes[stroke_id], bounding_box
                )

    fill_result = {}
    for fill_id, fill_shape in point_lists_to_shapes(fill_edges).items():
        fill_result[fill_id] = (fill_shape, fill_boxes[fill_id])

    stroke_result = {}
    for stroke_id, stroke_path in stroke_paths.items():
        stroke_result[stroke_id] = (stroke_path, stroke_boxes[stroke_id])

    return fill_result, stroke_result
