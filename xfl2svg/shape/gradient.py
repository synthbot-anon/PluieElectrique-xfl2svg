"""Convert XFL gradients to SVG."""


from dataclasses import dataclass
import math
from typing import List, Tuple
import xml.etree.ElementTree as ET

from xfl2svg.util import check_known_attrib, get_matrix, Traceable


@dataclass(frozen=True)
class LinearGradient(Traceable):
    start: Tuple[float, float]
    end: Tuple[float, float]
    stops: Tuple[Tuple[float, str, float], ...]
    spread_method: str

    @classmethod
    def from_xfl(cls, element):
        """Create a LinearGradient from the XFL <LinearGradient> element.

        The start and end points of the gradient are defined by the <Matrix> M:

                   0%             100%
            start >---------o---------> end
           (M @ s)       midpoint     (M @ e)
                         (tx, ty)

        where

              |a c tx|        |-16384/20|        | 16384/20|
          M = |b d ty|    s = |    0    |    e = |    0    |
              |0 0  1|        |    1    |        |    1    |

        The magic constant of 16384/20 is weird, but it's likely related to how
        edge coordinates are precise to the nearest 1/20 (disregarding decimal
        coordinates, which are more precise).
        """

        a, b, _, _, tx, ty = map(float, get_matrix(element))
        start = (a * -16384/20 + tx, b * -16384/20 + ty)  # fmt: skip
        end   = (a *  16384/20 + tx, b *  16384/20 + ty)  # fmt: skip

        stops = []
        for entry in element.iterfind("{*}GradientEntry"):
            check_known_attrib(entry, {"ratio", "color", "alpha"})
            stops.append(
                (
                    float(entry.get("ratio")) * 100,
                    entry.get("color", "#000000"),
                    float(entry.get("alpha") or 1),
                )
            )

        check_known_attrib(element, {"spreadMethod", "interpolationMethod"})
        spread_method = element.get("spreadMethod", "pad")

        return cls(start, end, tuple(stops), spread_method)

    def to_xfl(self):
        # TODO: figure out how to calculate c and d matrix elements
        a = (self.end[0] - self.start[0]) / 2 / (16384 / 20)
        b = (self.end[1] - self.start[1]) / 2 / (16384 / 20)
        tx = (self.end[0] + self.start[0]) / 2
        ty = (self.end[1] + self.start[1]) / 2

        gradient_entries = []
        for ratio, color, alpha in self.stops:
            gradient_entries.append(
                f"""
                <GradientEntry color="{color}" alpha="{alpha}" ratio="{ratio/100}" />
            """
            )

        result = f"""
            <LinearGradient spreadMethod="{self.spread_method}">
                <matrix>
                    <Matrix a="{a}" b="{b}" tx="{tx}" ty="{ty}"/>
                </matrix>
                {''.join(gradient_entries)}

            </LinearGradient>
        """
        return result

    @classmethod
    def from_dict(cls, d):
        params = d["linearGradient"]
        stops = []
        for d in params["stop"]:
            stops.append((d["offset"], d["stop-color"], d["stop-opacity"]))

        return LinearGradient(
            (params["x1"], params["y1"]),
            (params["x2"], params["y2"]),
            tuple(stops),
            params["spreadMethod"],
        )

    def to_dict(self):
        result = {
            "linearGradient": {
                "x1": self.start[0],
                "y1": self.start[1],
                "x2": self.end[0],
                "y2": self.end[1],
                "spreadMethod": self.spread_method,
                "stop": [],
            }
        }

        for offset, color, alpha in self.stops:
            attrib = {"offset": offset, "stop-color": color}
            if alpha is not None:
                attrib["stop-opacity"] = alpha
            else:
                attrib["stop-opacity"] = 1
            result["linearGradient"]["stop"].append(attrib)

        return result

    def to_svg(self):
        """Create an SVG <linearGradient> element from a LinearGradient."""
        element = ET.Element(
            "linearGradient",
            {
                "id": self.id,
                "gradientUnits": "userSpaceOnUse",
                "x1": str(self.start[0]),
                "y1": str(self.start[1]),
                "x2": str(self.end[0]),
                "y2": str(self.end[1]),
                "spreadMethod": self.spread_method,
            },
        )
        for offset, color, alpha in self.stops:
            attrib = {"offset": f"{offset}%", "stop-color": color}
            if alpha != 1:
                attrib["stop-opacity"] = str(alpha)
            ET.SubElement(element, "stop", attrib)
        return element

    @property
    def id(self):
        """Unique ID used to dedup SVG elements in <defs>."""
        return f"Gradient_{hash(self) & 0xFFFF_FFFF:08x}"


@dataclass(frozen=True)
class RadialGradient(Traceable):
    matrix: Tuple[float, ...]
    radius: float
    focal_point: float
    stops: Tuple[Tuple[float, str, str], ...]
    spread_method: str

    @classmethod
    def from_xfl(cls, element):
        a, b, c, d, tx, ty = map(float, get_matrix(element))
        norm = (a**2 + b**2) ** 0.5
        radius = 16384 / 20 * norm

        # NOTE: this might require radius as calculated from the bounding box
        focal_point = float(element.get("focalPointRatio", 0)) * radius

        if norm == 0:
            svg_matrix = ("NaN", "NaN", "NaN", "NaN")
        else:
            svg_a = a / norm
            svg_b = b / norm
            svg_c = c / norm
            svg_d = d / norm
            svg_matrix = (svg_a, svg_b, svg_c, svg_d, tx, ty)

        stops = []
        for entry in element.iterfind("{*}GradientEntry"):
            check_known_attrib(entry, {"ratio", "color", "alpha"})
            stops.append(
                (
                    float(entry.get("ratio")) * 100,
                    entry.get("color", "#000000"),
                    float(entry.get("alpha") or 1),
                )
            )
        stops = sorted(stops, key=lambda x: x[0])

        # TODO: interpolationMethod
        check_known_attrib(
            element, {"spreadMethod", "focalPointRatio", "interpolationMethod"}
        )
        spread_method = element.get("spreadMethod", "pad")

        return cls(svg_matrix, radius, focal_point, tuple(stops), spread_method)

    def to_xfl(self):
        norm = self.radius / (16384 / 20)
        # TODO: figure out how to calculate c and d matrix elements
        a = self.matrix[0] * norm
        b = self.matrix[1] * norm
        c = self.matrix[2] * norm
        d = self.matrix[3] * norm
        tx = self.matrix[4]
        ty = self.matrix[5]

        gradient_entries = []
        for ratio, color, alpha in self.stops:
            gradient_entries.append(
                f"""
                <GradientEntry color="{color}" alpha="{alpha}" ratio="{ratio/100}" />
            """
            )

        result = f"""
            <RadialGradient focalPointRatio="{self.focal_point / self.radius}" spreadMethod="{self.spread_method}">
                <matrix>
                    <Matrix a="{a}" b="{b}" c="{c}" d="{d}" tx="{tx}" ty="{ty}"/>
                </matrix>
                {''.join(gradient_entries)}

            </RadialGradient>
        """
        return result

    @classmethod
    def from_dict(cls, d):
        params = d["radialGradient"]
        matrix = map(lambda x: x if x != None else "NaN", params["gradientTransform"])

        stops = []
        for d in params["stop"]:
            stops.append((d["offset"], d["stop-color"], d["stop-opacity"]))

        return RadialGradient(
            tuple(matrix),
            params["r"],
            params["fx"],
            tuple(stops),
            params["spreadMethod"],
        )

    def to_dict(self):
        matrix = map(lambda x: x if x != "NaN" else None, self.matrix)
        result = {
            "radialGradient": {
                "r": self.radius,
                "fx": self.focal_point,
                "gradientTransform": list(matrix),
                "spreadMethod": self.spread_method,
                "stop": [],
            }
        }

        for offset, color, alpha in self.stops:
            attrib = {"offset": offset, "stop-color": color}
            if alpha is not None:
                attrib["stop-opacity"] = alpha
            else:
                attrib["stop-opacity"] = 1
            result["radialGradient"]["stop"].append(attrib)

        return result

    def to_svg(self):
        """Create an SVG <linearGradient> element from a LinearGradient."""
        matrix = map(str, self.matrix)
        element = ET.Element(
            "radialGradient",
            {
                "id": self.id,
                "gradientUnits": "userSpaceOnUse",
                "cx": "0",
                "cy": "0",
                "r": str(self.radius),
                "fx": str(self.focal_point),
                "fy": "0",
                "gradientTransform": f"matrix({','.join(matrix)})",
                "spreadMethod": self.spread_method,
            },
        )
        for offset, color, alpha in self.stops:
            attrib = {"offset": f"{offset}%", "stop-color": color}
            if alpha != 1:
                attrib["stop-opacity"] = str(alpha)
            ET.SubElement(element, "stop", attrib)

        return element

    @property
    def id(self):
        """Unique ID used to dedup SVG elements in <defs>."""
        return f"Gradient_{hash(self) & 0xFFFF_FFFF:08x}"
