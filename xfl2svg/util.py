"""Utility functions."""

import math
import re
import warnings

CHARACTER_ENTITY_REFERENCE = re.compile(r"&#(\d+)")
IDENTITY_MATRIX = ["1", "0", "0", "1", "0", "0"]


def unescape_entities(s):
    """Unescape XML character entity references."""
    return CHARACTER_ENTITY_REFERENCE.sub(lambda m: chr(int(m[1])), s)


def check_known_attrib(element, known):
    """Ensure that an XML element doesn't have unknown attributes."""
    if not set(element.keys()) <= known:
        unknown = set(element.keys()) - known
        # Remove namespace, if present
        tag = re.match(r"(\{[^}]+\})?(.*)", element.tag)[2]
        warnings.warn(
            f"Unknown <{tag}> attributes: {element.attrib}\n"
            f"  Known keys:   {known}\n"
            f"  Unknown keys: {unknown}"
        )
        raise Exception()


def get_matrix(element):
    """Get a transformation matrix from an XFL element."""
    # If this element has a <matrix>, it will be the first child. This is
    # faster than find() and also prevents us getting the matrix of a different
    # element (e.g. a <LinearGradient> nested inside a <DOMShape>).
    if len(element) and element[0].tag.endswith("matrix"):
        # element -> <matrix> -> <Matrix>
        matrix = element[0][0]
        # Column-major order, the same as in SVG
        #   a c tx
        #   b d ty
        #   0 0  1
        return [
            matrix.get("a") or "1",
            matrix.get("b") or "0",
            matrix.get("c") or "0",
            matrix.get("d") or "1",
            matrix.get("tx") or "0",
            matrix.get("ty") or "0",
        ]

    return IDENTITY_MATRIX


def line_bounding_box(p1, p2):
    return (min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]))


def quadratic_bezier(p1, p2, p3, t):
    x = (1 - t) * ((1 - t) * p1[0] + t * p2[0]) + t * ((1 - t) * p2[0] + t * p3[0])
    y = (1 - t) * ((1 - t) * p1[1] + t * p2[1]) + t * ((1 - t) * p2[1] + t * p3[1])
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


def merge_bounding_boxes(original, addition):
    if addition == None:
        return original

    if original == None:
        return addition

    return (
        min(original[0], addition[0]),
        min(original[1], addition[1]),
        max(original[2], addition[2]),
        max(original[3], addition[3]),
    )


def expanding_bounding_box(box, width):
    return (
        box[0] - width / 2,
        box[1] - width / 2,
        box[2] + width / 2,
        box[3] + width / 2,
    )


class Traceable:
    def to_dict(self):
        raise NotImplementedError()

    def to_svg(self):
        raise NotImplementedError()

    @property
    def id(self):
        raise NotImplementedError()
