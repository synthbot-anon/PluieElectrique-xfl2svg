"""Convert XFL gradients to SVG."""

# TODO: Support RadialGradient


from dataclasses import dataclass
import math
from typing import List, Tuple
import xml.etree.ElementTree as ET

from xfl2svg.util import check_known_attrib, get_matrix, Traceable

# @dataclass(frozen=True)
# class SolidColor(Traceable):
#     color: str
#     alpha: float

#     def from_xfl(cls, element):
#         color = element.get('color', '#000000')
#         alpha = element.get('alpha', 1)
#         return SolidColor(color, alpha)
    
#     def from_dict(cls, dict):
#         return SolidColor(dict['color'], dict['alpha'])

#     def to_svg(self, key, body, defs):
#         body[key] = self.color
#         body[f'{key}-opacity'] = str(self.alpha)
    
#     def to_dict(self):
#         return {
#             'color': self.color,
#             'alpha': self.alpha
#         }

def split_colors(color):
    if not color:
        return 0, 0, 0
    if not color.startswith("#"):
        raise Exception(f"invalid color: {color}")
    assert len(color) == 7
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return r, g, b

def interpolate_value(x, y, frac):
    return (1 - frac) * x + frac * y


def interpolate_color(colx, ax, coly, ay, t):
    rx, gx, bx = split_colors(colx)
    ry, gy, by = split_colors(coly)
    ai = interpolate_value(ax, ay, t)

    if ai == 0:
        return '#FFFFFF', 0

    ri = round(interpolate_value(rx*ax, ry*ay, t)/ai)
    gi = round(interpolate_value(gx*ax, gy*ay, t)/ai)
    bi = round(interpolate_value(bx*ax, by*ay, t)/ai)

    return "#%02X%02X%02X" % (ri, gi, bi), ai


@dataclass(frozen=True)
class LinearGradient(Traceable):
    start: Tuple[float, float]
    end: Tuple[float, float]
    stops: Tuple[Tuple[float, str, float], ...]
    spread_method: str

    @classmethod
    def interpolate(cls, x, y, t):
        # if x.start and y.start:
        #     start = [interpolate_value(p1, p2, t) for p1,p2 in zip(x.start, y.start)]
        # else:
        #     start = x.start or y.start
        
        # if x.end and y.end:
        #     end = [interpolate_value(p1, p2, t) for p1,p2 in zip(x.end, y.end)]
        # else:
        #     end = x.end or y.end

        xvec = (x.end[0]-x.start[0], x.end[1]-x.start[1])
        yvec = (y.end[0]-y.start[0], y.end[1]-y.start[1])

        xrot = math.atan2(xvec[1], xvec[0])
        yrot = math.atan2(yvec[1], yvec[0])
        xdist = math.sqrt(xvec[0]**2 + xvec[1]**2)
        ydist = math.sqrt(yvec[0]**2 + yvec[1]**2)
        xmid = (x.start[0]+xvec[0]/2, x.start[1]+xvec[1]/2)
        ymid = (y.start[0]+yvec[0]/2, y.start[1]+yvec[1]/2)

        rot = interpolate_value(xrot, yrot, t)
        mid = (interpolate_value(xmid[0], ymid[0], t), interpolate_value(xmid[1], ymid[1], t))
        dist = interpolate_value(xdist, ydist, t)

        start = (-math.cos(rot)*dist/2 + mid[0], -math.sin(rot)*dist/2 + mid[1])
        end = (math.cos(rot)*dist/2 + mid[0], math.sin(rot)*dist/2 + mid[1])

        all_stops = set([p[0] for p in x.stops] + [p[0] for p in y.stops])
        new_stops = []
        for ratio in all_stops:
            colx, ax = x.calculate_color(ratio)
            coly, ay = y.calculate_color(ratio)
            new_color, new_alpha = interpolate_color(colx, ax, coly, ay, t)
            new_stops.append([ratio, new_color, new_alpha])
        
        new_stops = sorted(new_stops, key=lambda x: x[0])
        prev_color = new_stops[0][1]
        for stop in new_stops[1:]:
            if stop[1] == None:
                stop[1] = prev_color
            prev_color = stop[1]
        
        new_stops = tuple(tuple(x) for x in new_stops)
        
        return LinearGradient(start, end, new_stops, x.spread_method)
        # return LinearGradient(start, end, x.stops, x.spread_method)
    
    def interpolate_color(self, color, alpha, t):
        new_stops = []
        for ratio, scol, salpha in self.stops:
            new_color, new_alpha = interpolate_color(scol, salpha, color, alpha, t)
            new_stops.append((ratio, new_color, new_alpha))
        
        new_stops = tuple(new_stops)
        return LinearGradient(self.start, self.end, new_stops, self.spread_method)

    def calculate_color(self, ratio):
        before = None
        after = None
        for stop in self.stops:
            if stop[0] == ratio:
                before = stop
                after = stop
                break
            
            if stop[0] > ratio:
                if after == None:
                    after = stop
                    continue
                if (after[0] - ratio) > (stop[0] - ratio):
                    after = stop
                continue
            
            if stop[0] < ratio:
                if before == None:
                    before = stop
                    continue
                if (ratio - before[0]) > (ratio - stop[0]):
                    before = stop
                continue
        
        if after == None:
            # The gradient part is over, just return the last color
            return self.stops[-1][1], self.stops[-1][2]
        
        if before == None:
            # The gradient part hasn't begun, just return the first color
            return self.stops[0][1], self.stops[0][2]

        if before[0] == after[0]:
            return before[1], before[2]

        t = (ratio - before[0]) / (after[0] - before[0])
        color, alpha = interpolate_color(before[1], before[2], after[1], after[2], t)

        return color, alpha
    
    @classmethod
    def from_color(cls, color, alpha):
        return LinearGradient(None, None, ((0, color, alpha),), None)

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

        check_known_attrib(element, {"spreadMethod"})
        spread_method = element.get("spreadMethod", "pad")

        return cls(start, end, tuple(stops), spread_method)
    
    def to_xfl(self):
        # TODO: figure out how to calculate c and d matrix elements
        a = (self.end[0] - self.start[0]) / 2 / (16384/20)
        b = (self.end[1] - self.start[1]) / 2 / (16384/20)
        tx = (self.end[0] + self.start[0]) / 2
        ty = (self.end[1] + self.start[1]) / 2

        gradient_entries = []
        for ratio, color, alpha in self.stops:
            gradient_entries.append(f"""
                <GradientEntry color="{color}" alpha="{alpha}" ratio="{ratio/100}" />
            """)

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

        # TODO: interpolationMethod
        check_known_attrib(
            element, {"spreadMethod", "focalPointRatio", "interpolationMethod"}
        )
        spread_method = element.get("spreadMethod", "pad")

        return cls(svg_matrix, radius, focal_point, tuple(stops), spread_method)

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

    @property
    def id(self):
        """Unique ID used to dedup SVG elements in <defs>."""
        return f"Gradient_{hash(self) & 0xFFFF_FFFF:08x}"
