"""Convert XFL fill and stroke styles to SVG attributes."""

import xml.etree.ElementTree as ET
import warnings

from xfl2svg.shape.gradient import LinearGradient, RadialGradient
from xfl2svg.util import check_known_attrib


def xml_str(element):
    return ET.tostring(element, encoding="unicode")


def update(d, keys, values):
    """Update the dict `d` with non-None values."""
    for k, v in zip(keys, values):
        if v is not None:
            d[k] = v


def parse_solid_color(style):
    """Parse an XFL <SolidColor> element.

    Returns a tuple:
        color: Hex color code
        alpha: Optional alpha value
    """
    check_known_attrib(style, {"color", "alpha"})
    return style.get("color", "#000000"), style.get("alpha")


def get_radius(bounding_box):
    width = bounding_box[2] - bounding_box[0]
    height = bounding_box[3] - bounding_box[1]
    return (width**2 + height**2) ** 0.5 / 2


def parse_fill_style(style):
    """Parse an XFL <FillStyle> element.

    Returns a tuple:
        attrib: Dict of SVG style attributes
        extra_defs: Dict of {element_id: SVG element to put in <defs>}
    """
    attrib = {"stroke": "none"}
    # extra_defs = {}
    # attrib, extra_defs = parse_stroke_style(style, bounding_box)
    # attrib["stroke-width"] = "0.05"

    if style.tag.endswith("SolidColor"):
        update(attrib, ("fill", "fill-opacity"), parse_solid_color(style))
        # update(attrib, ("stroke", "stroke-opacity"), parse_solid_color(style))
    elif style.tag.endswith("LinearGradient"):
        gradient = LinearGradient.from_xfl(style)
        attrib["fill"] = gradient  # f"url(#{gradient.id})"
        # attrib["stroke"] = f"url(#{gradient.id})"
        # extra_defs[gradient.id] = gradient.to_svg()
    elif style.tag.endswith("RadialGradient"):
        gradient = RadialGradient.from_xfl(style)
        attrib["fill"] = gradient  # f"url(#{gradient.id})"
        # attrib["stroke"] = f"url(#{gradient.id})"
        # extra_defs[gradient.id] = gradient.to_svg()
    else:
        warnings.warn(f"Unknown fill style: {xml_str(style)}")

    return attrib


def parse_stroke_style(style):
    """Parse an XFL <StrokeStyle> element.

    Returns a dict of SVG style attributes.
    """
    if not style.tag.endswith("SolidStroke"):
        if not style.tag.endswith("RadialGradient"):  # TODO?
            warnings.warn(f"Unknown stroke style: {xml_str(style)}")
            return {"fill": "none"}

    check_known_attrib(
        style,
        {
            "scaleMode",
            "weight",
            "joints",
            "miterLimit",
            "caps",
            "solidStyle",
            "pixelHinting",
            "sharpCorners",
            "focalPointRatio",
            "spreadMethod",
            "interpolationMethod",
        },
    )
    if style.get("scaleMode") != "normal":
        warnings.warn(f"Unknown `scaleMode` value: {style.get('scaleMode')}")
        return {"fill": "none"}

    cap = style.get("caps", "round")
    if cap == "none":
        cap = "butt"

    attrib = {
        "stroke-linecap": cap,
        "stroke-width": style.get("weight", "1"),
        "stroke-linejoin": style.get("joints", "round"),
        "fill": "none",
    }
    # extra_defs = {}

    solid = style.get("solidStyle")
    if solid:
        if solid != "hairline":
            warnings.warn(f"Unknown `solidStyle` value: {style.get('solidStyle')}")
        else:
            # A hairline solidStyle overrides the 'weight' attribute.
            attrib["stroke-width"] = "0.05"

    if attrib["stroke-linejoin"] == "miter":
        # Other projects* use a default stroke-miterlimit of 3, but that doesn't seem
        # to work reliably, and it produces strokes that alternate between jarringly
        # alternate between flat and sharp. It seems to work better to use a round
        # join when the miter is under-specified.
        # [*]: https://github.com/ruffle-rs/ruffle/blob/d3becd9/core/src/avm1/globals/movie_clip.rs#L283-L290
        if "miterLimit" in style:
            attrib["stroke-miterlimit"] = style.get("miterLimit", "3")
        else:
            attrib["stroke-linejoin"] = "round"

    fill = style[0][0]
    if fill.tag.endswith("RadialGradient"):
        gradient = RadialGradient.from_xfl(fill)
        attrib["stroke"] = gradient  # f"url(#{gradient.id})"
        # extra_defs[gradient.id] = gradient.to_svg()
    elif fill.tag.endswith("SolidColor"):
        update(attrib, ("stroke", "stroke-opacity"), parse_solid_color(fill))
    elif fill.tag.endswith("LinearGradient"):
        gradient = LinearGradient.from_xfl(fill)
        attrib["stroke"] = gradient
    else:
        warnings.warn(f"Unknown stroke fill: {xml_str(fill)}")
        return attrib

    return attrib


def parse_json_style(style):
    result = {}
    for key, value in style.items():
        if not isinstance(value, dict):
            result[key] = value
            continue

        if len(value) != 1:
            result[key] = value
            continue

        if "radialGradient" in value:
            result[key] = RadialGradient.from_dict(value)
            continue

        if "linearGradient" in value:
            result[key] = LinearGradient.from_dict(value)
            continue

    return result
