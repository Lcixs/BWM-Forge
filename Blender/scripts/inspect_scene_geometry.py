import argparse
import json
import sys
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--blend", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--camera", default="camera_input")
    parser.add_argument("--object", action="append", default=[])
    parser.add_argument("--prefix", action="append", default=[])
    return parser.parse_args(argv)


def matrix_rows(matrix):
    return [[float(value) for value in row] for row in matrix]


def world_bounds(obj):
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    return {
        "min": [min(point[axis] for point in points) for axis in range(3)],
        "max": [max(point[axis] for point in points) for axis in range(3)],
        "center": [sum(point[axis] for point in points) / len(points) for axis in range(3)],
    }


def selected_meshes(names, prefixes):
    meshes = [obj for obj in bpy.data.objects if obj.type == "MESH"]
    if not names and not prefixes:
        return meshes
    wanted = set(names)
    return [obj for obj in meshes if obj.name in wanted or any(obj.name.startswith(prefix) for prefix in prefixes)]


def inspect_object(scene, camera, depsgraph, obj):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    front_count = 0
    in_frame_count = 0
    camera_points = []
    ndc_points = []
    try:
        camera_inverse = camera.evaluated_get(depsgraph).matrix_world.inverted()
        for vertex in mesh.vertices:
            world = evaluated.matrix_world @ vertex.co
            camera_local = camera_inverse @ world
            ndc = world_to_camera_view(scene, camera, world)
            in_front = float(-camera_local.z) > float(camera.data.clip_start)
            inside = in_front and 0.0 <= ndc.x <= 1.0 and 0.0 <= ndc.y <= 1.0
            front_count += int(in_front)
            in_frame_count += int(inside)
            camera_points.append(tuple(float(value) for value in camera_local))
            ndc_points.append((float(ndc.x), float(ndc.y)))
    finally:
        evaluated.to_mesh_clear()

    result = {
        "name": obj.name,
        "parent": obj.parent.name if obj.parent else None,
        "hide_render": bool(obj.hide_render),
        "matrix_world": matrix_rows(obj.matrix_world),
        "bounds_world": world_bounds(obj),
        "vertex_projection_diagnostic": {
            "vertex_count": len(camera_points),
            "front_vertex_count": front_count,
            "in_frame_vertex_count": in_frame_count,
            "acceptance_evidence": False,
            "note": "Sparse vertex projection cannot replace raster/object-ID visibility.",
        },
    }
    if camera_points:
        result["vertex_projection_diagnostic"].update(
            {
                "camera_xyz_min": [min(point[axis] for point in camera_points) for axis in range(3)],
                "camera_xyz_max": [max(point[axis] for point in camera_points) for axis in range(3)],
                "ndc_xy_min": [min(point[axis] for point in ndc_points) for axis in range(2)],
                "ndc_xy_max": [max(point[axis] for point in ndc_points) for axis in range(2)],
            }
        )
    return result


def main():
    args = parse_args()
    blend_path = Path(args.blend).resolve()
    output_path = Path(args.output).resolve()
    bpy.ops.wm.open_mainfile(filepath=str(blend_path))
    scene = bpy.context.scene
    scene.frame_set(1)
    camera = bpy.data.objects.get(args.camera)
    if camera is None or camera.type != "CAMERA":
        raise RuntimeError(f"camera not found or not a camera: {args.camera}")
    depsgraph = bpy.context.evaluated_depsgraph_get()
    objects = selected_meshes(args.object, args.prefix)
    payload = {
        "schema_version": "1.0",
        "blend": str(blend_path),
        "frame": int(scene.frame_current),
        "camera": {
            "name": camera.name,
            "matrix_world": matrix_rows(camera.matrix_world),
            "lens": float(camera.data.lens),
            "sensor_width": float(camera.data.sensor_width),
            "clip": [float(camera.data.clip_start), float(camera.data.clip_end)],
        },
        "selection": {"objects": args.object, "prefixes": args.prefix},
        "objects": [inspect_object(scene, camera, depsgraph, obj) for obj in objects],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
