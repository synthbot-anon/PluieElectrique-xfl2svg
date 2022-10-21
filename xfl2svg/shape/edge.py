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


from collections import defaultdict
import re
from typing import Iterator, List, Tuple
import xml.etree.ElementTree as ET


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
                    yield point_list
                    point_list = []
                    prev_point = curr_point
            elif command in "|/":
                # Line to
                point_list.append((prev_point[0], prev_point[1]))
                point_list.append((curr_point[0], curr_point[1]))
                prev_point = curr_point
            else:
                # Quad to. The control point (curr_point) is marked by putting
                # it in a tuple.
                end_point = next_point()
                point_list.append((prev_point[0], prev_point[1]))
                point_list.append(((curr_point[0], curr_point[1]),))
                point_list.append((end_point[0], end_point[1]))
                prev_point = end_point
    except StopIteration:
        yield point_list


def xfl_domshape_to_edges(domshape: ET.Element) -> List[Tuple]:
    """Convert the XFL <DOMShape> element into edges (path + color data).

    Args:
        domshape: The <DOMShape> element

    Returns a list of tuples, each containing a path, left fill, right fill, and stroke:
        [(path, fill_id_left, fill_id_right, stroke_id), ...]
    """
    fill_edges = []
    stroke_edges = []
    stroke_paths = defaultdict(list)
    edges_element = domshape.find("{*}edges")

    # Ignore the "cubics" attribute, as it's only used by Animate
    for edge in edges_element.iterfind(".//{*}Edge[@edges]"):
        edge_format = edge.get("edges")
        fill_id_left = edge.get("fillStyle0")
        fill_id_right = edge.get("fillStyle1")
        stroke_id = edge.get("strokeStyle")

        for path in edge_format_to_point_lists(edge_format):
            yield tuple(path), fill_id_left, fill_id_right, stroke_id


def xfl_domshape_to_visible_edges(domshape, known_fills, known_strokes):
    """Wrapper for xfl_domshape_to_edges to skip over unknown fills and strokes."""

    for shape_piece in xfl_domshape_to_edges(domshape):
        path, fill_id_left, fill_id_right, stroke_id = shape_piece
        if fill_id_left not in known_fills:
            fill_id_left = None
        if fill_id_right not in known_fills:
            fill_id_right = None
        if stroke_id not in known_strokes:
            stroke_id = None

        if fill_id_left or fill_id_right or stroke_id:
            yield path, fill_id_left, fill_id_right, stroke_id
