from pathlib import Path
import math
import os
import re
import xml.etree.ElementTree as ET
from urllib.parse import unquote

from config import INPUT_MAX_ASSET_XML_BYTES

MOVABLE = {"revolute", "continuous", "prismatic"}


def joint_chain(joints, base, tool):
    by_child = {j["child"]: j for j in joints.values()}
    chain, seen = [], set()
    while tool != base:
        if tool not in by_child or tool in seen:
            raise ValueError("The tool link is not a descendant of the selected base.")
        seen.add(tool)
        joint = by_child[tool]
        chain.append(joint)
        tool = joint["parent"]
    return list(reversed(chain))


def _local_file(root, parent, reference):
    reference = str(reference).strip().replace("\\", "/")
    if not reference or reference.startswith("/") or ":" in reference or "\x00" in reference:
        raise ValueError("Resources must use relative paths within the asset directory: " + reference)
    path = Path(os.path.abspath(parent / reference))
    # 软链接和目录联接会改变复制后的资源位置，因此要求普通文件。
    if path.resolve() != path:
        raise ValueError("Asset resources must not use symlinks or directory junctions: " + reference)
    if ((root is not None and not path.is_relative_to(root))
            or not path.is_file() or path.stat().st_size == 0):
        raise ValueError("Resource is missing, empty, or outside the asset directory: " + reference)
    return path


def _read_xml(path):
    if path.stat().st_size > INPUT_MAX_ASSET_XML_BYTES:
        raise ValueError("URDF/DAE XML exceeds 16 MiB.")
    raw = path.read_bytes()
    if re.search(br"<!\s*(DOCTYPE|ENTITY)\b", raw.replace(b"\x00", b""), re.I):
        raise ValueError("Model XML must not contain DTD or entity declarations.")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("Unable to read model XML: " + path.name) from exc


def _one(node, tag, required=False):
    values = node.findall(tag)
    if len(values) > 1 or (required and not values):
        raise ValueError(f"{node.tag} {node.get('name', '')} has a missing or duplicate {tag}.")
    return values[0] if values else None


def _numbers(text, size, label):
    try:
        values = [float(v) for v in text.split()]
    except (ValueError, AttributeError):
        raise ValueError(label + " requires numeric values.") from None
    if len(values) != size or not all(math.isfinite(v) for v in values):
        raise ValueError(label + " has an invalid length or contains NaN/Inf.")
    return values


def _check_dae_references(document: ET.Element, path: Path) -> None:
    uri_attributes = {
        "input": ("source",),
        "accessor": ("source",),
        "skin": ("source",),
        "morph": ("source",),
        "channel": ("source",),
        "instance_material": ("target",),
        "instance_rigid_body": ("target",),
        "render": ("camera_node",),
        "convex_mesh": ("convex_hull_of",),
    }
    for element in document.iter():
        name = element.tag.rsplit("}", 1)[-1]
        references = [(attribute, element.attrib[attribute])
                      for attribute in ("url", *uri_attributes.get(name, ()))
                      if attribute in element.attrib]
        if name == "skeleton":
            references.append(("text", element.text or ""))
        for attribute, reference in references:
            if not reference.strip().startswith("#"):
                raise ValueError(
                    f"External DAE references are unsupported: {path} "
                    f"({name}/{attribute}: {reference!r})"
                )


def validate_urdf(package_root, urdf, *, asset_name=None):
    root = Path(package_root).resolve() if package_root is not None else None
    if root is None:
        entry = Path(os.path.abspath(Path(urdf).expanduser()))
        if entry.suffix.lower() != ".urdf" or not entry.is_file():
            raise ValueError("Select an existing, fully expanded .urdf file.")
        entry = _local_file(None, entry.parent, entry.name)
    else:
        entry = _local_file(root, root, urdf)
    robot = _read_xml(entry)
    if robot.tag != "robot" or any("xacro" in node.tag or any(
            "${" in value or "$(" in value for value in [*node.attrib.values(), node.text or "", node.tail or ""])
            for node in robot.iter()):
        raise ValueError("Provide a fully expanded URDF robot model.")
    for node in robot.iter():
        if node.tag == "origin":
            _numbers(node.get("xyz", "0 0 0"), 3, "origin.xyz")
            _numbers(node.get("rpy", "0 0 0"), 3, "origin.rpy")
        if node.tag in {"visual", "collision"}:
            geometry = _one(node, "geometry", True)
            if len(geometry) != 1 or geometry[0].tag not in {"mesh", "box", "sphere", "cylinder"}:
                raise ValueError("visual/collision requires one valid geometry shape.")
        for tag, key, size in (("mesh", "scale", 3), ("box", "size", 3), ("sphere", "radius", 1),
                               ("cylinder", "radius", 1), ("cylinder", "length", 1)):
            if node.tag == tag:
                values = _numbers(node.get(key, "1 1 1" if tag == "mesh" else ""), size, tag + "." + key)
                if any(v == 0 if tag == "mesh" else v <= 0 for v in values):
                    raise ValueError("Geometry dimensions must be positive, and mesh scale must be nonzero.")
    links = [node.get("name", "").strip() for node in robot.findall("link")]
    if not links or any(not name for name in links) or len(set(links)) != len(links):
        raise ValueError("URDF link names must be non-empty and unique.")
    joints, parents = {}, {}
    for node in robot.findall("joint"):
        name, kind = node.get("name", "").strip(), node.get("type")
        parent, child = _one(node, "parent", True), _one(node, "child", True)
        if (not name or name in joints or kind not in MOVABLE | {"fixed", "planar", "floating"}
                or parent is None or child is None):
            raise ValueError("Joint name, type, or parent-child connection is incomplete.")
        parent, child = parent.get("link"), child.get("link")
        if parent not in links or child not in links or parent == child or child in parents:
            raise ValueError("Invalid joint parent-child connection, or a link has multiple parent joints: " + name)
        parents[child] = parent
        origin, axis = _one(node, "origin"), _one(node, "axis")
        xyz = _numbers(origin.get("xyz", "0 0 0") if origin is not None else "0 0 0", 3, name + ".xyz")
        rpy = _numbers(origin.get("rpy", "0 0 0") if origin is not None else "0 0 0", 3, name + ".rpy")
        direction = _numbers(axis.get("xyz", "1 0 0") if axis is not None else "1 0 0", 3, name + ".axis")
        if kind in MOVABLE | {"planar"} and not any(direction):
            raise ValueError("A movable joint axis must be nonzero: " + name)
        limit = _one(node, "limit", kind in {"revolute", "prismatic"})
        limits = {key: _numbers(value, 1, name + ".limit." + key)[0]
                  for key, value in limit.attrib.items()} if limit is not None else {}
        required = {"effort", "velocity"} if kind in MOVABLE and limit is not None else set()
        if kind in {"revolute", "prismatic"}:
            required |= {"lower", "upper"}
        if required - limits.keys() or ("lower" in limits and "upper" in limits and limits["lower"] > limits["upper"]):
            raise ValueError("Joint limits are missing or the lower limit exceeds the upper limit: " + name)
        if any(limits.get(key, 0) < 0 for key in ("effort", "velocity", "acceleration", "deceleration", "jerk")):
            raise ValueError("Joint effort, velocity, and other limits must not be negative: " + name)
        mimic = _one(node, "mimic")
        relation = None if mimic is None else {"joint": mimic.get("joint", ""),
                    "multiplier": _numbers(mimic.get("multiplier", "1"), 1, name + ".mimic.multiplier")[0],
                    "offset": _numbers(mimic.get("offset", "0"), 1, name + ".mimic.offset")[0]}
        joints[name] = {"name": name, "type": kind, "parent": parent, "child": child,
                        "origin": {"xyz": xyz, "rpy": rpy}, "axis": direction, "limits": limits, "mimic": relation}
    roots = set(links) - parents.keys()
    if len(roots) != 1:
        raise ValueError("URDF must have exactly one root, with all links connected as a tree.")
    root_link = next(iter(roots))
    for link in links:
        visited, cursor = set(), link
        while cursor in parents and cursor not in visited:
            visited.add(cursor)
            cursor = parents[cursor]
        if cursor != root_link:
            raise ValueError("URDF contains a cycle or disconnected components.")
    for joint in joints.values():
        current, seen, multiplier, offset = joint, set(), 1.0, 0.0
        while current["mimic"] is not None:
            relation = current["mimic"]
            target = joints.get(relation["joint"])
            if current["name"] in seen or target is None:
                raise ValueError("mimic references a missing joint or creates a self-reference/cycle: " + joint["name"])
            if (current["type"] not in MOVABLE or target["type"] not in MOVABLE
                    or (current["type"] == "prismatic") != (target["type"] == "prismatic")):
                raise ValueError("mimic must reference a movable joint with compatible units.")
            seen.add(current["name"])
            offset += multiplier * relation["offset"]
            multiplier *= relation["multiplier"]
            current = target
        if not all(math.isfinite(v) for v in (multiplier, offset)):
            raise ValueError("Overflow in the chained mimic calculation.")
        joint.update(driver=current["name"], driver_multiplier=multiplier, driver_offset=offset)

    resources, meshes = {entry}, set()
    for node in robot.iter():
        if node.tag in {"mesh", "texture"}:
            path = _local_file(root, entry.parent, node.get("filename", ""))
            resources.add(path)
            if node.tag == "mesh":
                if path.suffix.lower() not in {".stl", ".dae"}:
                    raise ValueError("Supported asset mesh formats are STL and DAE.")
                meshes.add(path)
    pending = [p for p in meshes if p.suffix.lower() == ".dae"]
    inspected = set()
    while pending:
        path = pending.pop()
        if path in inspected:
            continue
        inspected.add(path)
        document = _read_xml(path)
        if document.tag.rsplit("}", 1)[-1] != "COLLADA":
            raise ValueError("The DAE file is missing a COLLADA root element.")
        _check_dae_references(document, path)
        for node in document.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "image":
                for item in node:
                    if item.tag.rsplit("}", 1)[-1] == "init_from":
                        reference = (item.text or "").strip() or next(
                            ((part.text or "").strip() for part in item
                             if part.tag.rsplit("}", 1)[-1] == "ref"), "")
                        resource = _local_file(root, path.parent, unquote(reference))
                        resources.add(resource)
                        if resource.suffix.lower() == ".dae":
                            pending.append(resource)

    # 共同父目录用于保留 urdf/、meshes/ 等资源的相对结构。
    if root is None:
        root = Path(os.path.commonpath([str(path.parent) for path in resources]))
    asset_name = root.name if asset_name is None else asset_name
    if not isinstance(asset_name, str) or not asset_name.strip():
        raise ValueError("Asset name must be non-empty text.")
    mesh_root = root
    if meshes:
        conventional = root / "meshes"
        mesh_root = conventional if all(p.is_relative_to(conventional) for p in meshes) else Path(
            os.path.commonpath([str(p.parent) for p in meshes]))
    return {"passed": True, "asset_name": asset_name,
            "model_name": robot.get("name", "").strip() or asset_name,
            "assets": {"root": str(root), "urdf": str(entry), "meshes": str(mesh_root)},
            "mesh_files": [str(p) for p in sorted(meshes)],
            "resource_files": [str(p) for p in sorted(resources)],
            "links": links, "root_link": root_link, "joints": joints}
