"""FloorPlanSpec → IFC4（ifcopenshell）。純函式，不得呼叫 LLM。

需要選用相依 `ifcopenshell`（`uv pip install -e ".[ifc]"`）。

實體對應（單位：長度 mm、面積 m²）
- IfcProject（Name = spec.title）→ IfcSite → IfcBuilding → IfcBuildingStorey（IfcRelAggregates）
- 外牆：外框每一邊一道 IfcWall（Name `EW-<i>`，轉角斜接的梯形輪廓），Pset_WallCommon.IsExternal = True
- 內牆：每一段內牆中心線直線段一道 IfcWall（Name `IW-<j>`，中心線兩側各半牆厚），IsExternal = False
- IfcSpace：每房一個（Name = 房名、LongName = 用途），輪廓 = 淨空多邊形擠出牆高，
  Qto_SpaceBaseQuantities.NetFloorArea = 淨面積（m²，兩位小數）、GrossFloorArea = 房間多邊形面積；
  聚合於樓層
- 門 / 窗：IfcOpeningElement（放置點 = 開口起點 a，貫穿牆厚）以 IfcRelVoidsElement 開在所在 IfcWall，
  IfcDoor / IfcWindow（Name = id、OverallWidth / OverallHeight）以 IfcRelFillsElement 填入
- 牆 / 門 / 窗以 IfcRelContainedInSpatialStructure 包含於樓層
- 未產生 IfcRelSpaceBoundary（選用；verifier 不檢查）

幾何以 IfcExtrudedAreaSolid（IfcArbitraryClosedProfileDef）自行建立，高度取 spec.ifc（示意值）。
"""

from __future__ import annotations

from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Polygon

from draftagent.schema.floorplan import (
    FloorPlanSpec,
    LocateError,
    Placement,
    door_placement,
    exterior_edges,
    interior_wall_lines,
    window_placement,
)


def _require():
    try:
        import ifcopenshell
        import ifcopenshell.api
    except ImportError as e:  # pragma: no cover - 依安裝而定
        raise RuntimeError("floor-plan 的 IFC 輸出需要 ifcopenshell：uv pip install -e '.[ifc]'") from e
    return ifcopenshell, ifcopenshell.api


class _Builder:
    def __init__(self, spec: FloorPlanSpec):
        ifcopenshell, api = _require()
        self.api = api
        self.spec = spec
        f = ifcopenshell.file(schema="IFC4")
        self.f = f
        run = api.run
        self.project = run("root.create_entity", f, ifc_class="IfcProject", name=spec.title or "floor-plan")
        units = [
            f.createIfcSIUnit(None, "LENGTHUNIT", "MILLI", "METRE"),
            f.createIfcSIUnit(None, "AREAUNIT", None, "SQUARE_METRE"),
            f.createIfcSIUnit(None, "VOLUMEUNIT", None, "CUBIC_METRE"),
            f.createIfcSIUnit(None, "PLANEANGLEUNIT", None, "RADIAN"),
        ]
        run("unit.assign_unit", f, units=units)
        model = run("context.add_context", f, context_type="Model")
        self.body = run(
            "context.add_context", f, context_type="Model", context_identifier="Body",
            target_view="MODEL_VIEW", parent=model,
        )  # fmt: skip
        self.site = run("root.create_entity", f, ifc_class="IfcSite", name="Site")
        self.building = run("root.create_entity", f, ifc_class="IfcBuilding", name="Building")
        self.storey = run("root.create_entity", f, ifc_class="IfcBuildingStorey", name=spec.ifc.storey_name)
        run("aggregate.assign_object", f, products=[self.site], relating_object=self.project)
        run("aggregate.assign_object", f, products=[self.building], relating_object=self.site)
        run("aggregate.assign_object", f, products=[self.storey], relating_object=self.building)
        self.origin = self._placement((0.0, 0.0, 0.0), None)
        self.storey.ObjectPlacement = self.origin
        for el in (self.site, self.building):
            el.ObjectPlacement = self.origin

    # -------------------------------------------------- helpers

    def _placement(self, xyz, rel):
        f = self.f
        loc = f.createIfcCartesianPoint([float(v) for v in xyz])
        axis = f.createIfcAxis2Placement3D(loc, None, None)
        return f.createIfcLocalPlacement(rel, axis)

    def _extrusion(self, pts, z0: float, height: float, origin=(0.0, 0.0)):
        """平面多邊形（世界座標）→ 相對 origin 的 IfcExtrudedAreaSolid，底面 z0（相對物件放置點）。"""
        f = self.f
        ring = [(float(x - origin[0]), float(y - origin[1])) for x, y in pts]
        ring = [*ring, ring[0]]
        curve = f.createIfcPolyline([f.createIfcCartesianPoint(list(p)) for p in ring])
        prof = f.createIfcArbitraryClosedProfileDef("AREA", None, curve)
        pos = f.createIfcAxis2Placement3D(f.createIfcCartesianPoint([0.0, 0.0, float(z0)]), None, None)
        d = f.createIfcDirection([0.0, 0.0, 1.0])
        return f.createIfcExtrudedAreaSolid(prof, pos, d, float(height))

    def _shape(self, element, solid) -> None:
        f = self.f
        rep = f.createIfcShapeRepresentation(self.body, "Body", "SweptSolid", [solid])
        element.Representation = f.createIfcProductDefinitionShape(None, None, [rep])

    def _contain(self, products) -> None:
        self.api.run("spatial.assign_container", self.f, products=products, relating_structure=self.storey)

    # -------------------------------------------------- build

    def walls(self) -> tuple[list, list]:
        run, f, h = self.api.run, self.f, self.spec.ifc.wall_height
        ext, intr = [], []
        for i, (p0, p1, q1, q0) in enumerate(exterior_edges(self.spec)):
            w = run("root.create_entity", f, ifc_class="IfcWall", name=f"EW-{i + 1}")
            w.ObjectPlacement = self._placement((0.0, 0.0, 0.0), self.origin)
            self._shape(w, self._extrusion([p0, p1, q1, q0], 0.0, h))
            pset = run("pset.add_pset", f, product=w, name="Pset_WallCommon")
            run("pset.edit_pset", f, pset=pset, properties={"IsExternal": True})
            ext.append((w, LineString([q0, q1])))
        ti = self.spec.wall_thickness.interior
        for j, s in enumerate(interior_wall_lines(self.spec)):
            w = run("root.create_entity", f, ifc_class="IfcWall", name=f"IW-{j + 1}")
            w.ObjectPlacement = self._placement((0.0, 0.0, 0.0), self.origin)
            ux, uy = s.u
            nx, ny = -uy, ux
            half = ti / 2
            pts = [
                (s.start[0] - nx * half, s.start[1] - ny * half),
                (s.end[0] - nx * half, s.end[1] - ny * half),
                (s.end[0] + nx * half, s.end[1] + ny * half),
                (s.start[0] + nx * half, s.start[1] + ny * half),
            ]
            self._shape(w, self._extrusion(pts, 0.0, h))
            pset = run("pset.add_pset", f, product=w, name="Pset_WallCommon")
            run("pset.edit_pset", f, pset=pset, properties={"IsExternal": False})
            intr.append((w, LineString([s.start, s.end])))
        self._contain([w for w, _ in ext + intr])
        return ext, intr

    def spaces(self, nets: dict[str, object]) -> None:
        run, f, h = self.api.run, self.f, self.spec.ifc.wall_height
        products = []
        for r in self.spec.rooms:
            sp = run("root.create_entity", f, ifc_class="IfcSpace", name=r.name)
            sp.LongName = r.use
            sp.ObjectPlacement = self._placement((0.0, 0.0, 0.0), self.origin)
            net = nets[r.name]
            poly = max(net.geoms, key=lambda g: g.area) if isinstance(net, MultiPolygon) else net
            if isinstance(poly, Polygon) and not poly.is_empty:
                self._shape(sp, self._extrusion(list(poly.exterior.coords)[:-1], 0.0, h))
            q = run("pset.add_qto", f, product=sp, name="Qto_SpaceBaseQuantities")
            run(
                "pset.edit_qto", f, qto=q,
                properties={
                    "NetFloorArea": round(net.area / 1e6, 2),
                    "GrossFloorArea": round(Polygon(r.outline()).area / 1e6, 2),
                },
            )  # fmt: skip
            products.append(sp)
        run("aggregate.assign_object", f, products=products, relating_object=self.storey)

    def openings(self, ext: list, intr: list) -> None:
        run, f, ifc = self.api.run, self.f, self.spec.ifc
        items = [(d, "IfcDoor") for d in self.spec.doors] + [(w, "IfcWindow") for w in self.spec.windows]
        fillings = []
        for it, cls in items:
            try:
                pl = door_placement(self.spec, it) if cls == "IfcDoor" else window_placement(self.spec, it)
            except (LocateError, KeyError):
                continue  # verifier 會報 error
            host = _host(ext if pl.exterior else intr, pl)
            if host is None:
                continue
            z0, height = (0.0, ifc.door_height) if cls == "IfcDoor" else (ifc.window_sill, ifc.window_height)
            op = run("root.create_entity", f, ifc_class="IfcOpeningElement", name=f"OP-{it.id}")
            op.ObjectPlacement = self._placement((pl.a[0], pl.a[1], 0.0), self.origin)
            self._shape(op, self._extrusion(_through(pl), z0, height, origin=pl.a))
            run("feature.add_feature", f, feature=op, element=host)
            el = run("root.create_entity", f, ifc_class=cls, name=it.id)
            el.ObjectPlacement = self._placement((pl.a[0], pl.a[1], 0.0), self.origin)
            el.OverallWidth = float(it.width)
            el.OverallHeight = float(height)
            self._shape(el, self._extrusion(_panel(pl), z0, height, origin=pl.a))
            run("feature.add_filling", f, opening=op, element=el)
            fillings.append(el)
        if fillings:
            self._contain(fillings)


def _mv(p, v, s):
    return (p[0] + v[0] * s, p[1] + v[1] * s)


def _through(pl: Placement):
    n = pl.n_out
    lo, hi = (-1.0, pl.thickness + 1.0) if pl.exterior else (-pl.thickness / 2 - 1.0, pl.thickness / 2 + 1.0)
    return [_mv(pl.a, n, lo), _mv(pl.b, n, lo), _mv(pl.b, n, hi), _mv(pl.a, n, hi)]


def _panel(pl: Placement, t: float = 40.0):
    """門窗本體示意：牆厚中央一片厚 t 的板。"""
    n = pl.n_out
    mid = pl.thickness / 2 if pl.exterior else 0.0
    return [_mv(pl.a, n, mid - t / 2), _mv(pl.b, n, mid - t / 2), _mv(pl.b, n, mid + t / 2), _mv(pl.a, n, mid + t / 2)]


def _host(walls: list, pl: Placement):
    """開口所在牆：外牆找內緣線、內牆找中心線，含開口線段（1 mm）者。"""
    seg = LineString([pl.a, pl.b])
    for w, line in walls:
        if line.buffer(1.0).contains(seg):
            return w
    return None


def render_ifc(spec: FloorPlanSpec):
    from draftagent.render.floorplan import net_polygons

    b = _Builder(spec)
    ext, intr = b.walls()
    b.spaces(net_polygons(spec))
    b.openings(ext, intr)
    return b.f


def write_ifc(spec: FloorPlanSpec, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    render_ifc(spec).write(str(path))
    return path

