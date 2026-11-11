import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["frame", "animation"], default="frame")
    parser.add_argument("--frame", type=int, default=1)
    parser.add_argument("--frame-end", type=int)
    parser.add_argument("--camera-role", choices=["input", "robot_preview"], default="input")
    parser.add_argument("--fps", type=float)
    parser.add_argument("--resolution", nargs=2, type=int)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--fast-white-model", action="store_true")
    parser.add_argument("--ffmpeg", help="FFmpeg executable path for Blender builds without bundled FFmpeg")
    parser.add_argument("--validate-stage", choices=["scene_only", "robot_only", "composed"])
    return parser.parse_args(argv)


def ensure_robot_preview_camera():
    existing = bpy.data.objects.get("camera_robot_preview")
    if existing and existing.type == "CAMERA":
        return existing
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH" and (obj.get("role") == "robot" or not any(o.get("role") == "environment" for o in bpy.context.scene.objects))]
    if not meshes:
        raise RuntimeError("cannot create robot preview camera: no robot mesh objects")
    corners = []
    for obj in meshes:
        corners.extend([obj.matrix_world @ Vector(corner) for corner in obj.bound_box])
    min_corner = Vector((min(v.x for v in corners), min(v.y for v in corners), min(v.z for v in corners)))
    max_corner = Vector((max(v.x for v in corners), max(v.y for v in corners), max(v.z for v in corners)))
    center = (min_corner + max_corner) * 0.5
    span = max(max_corner.x - min_corner.x, max_corner.z - min_corner.z, 0.05)
    depth = max(max_corner.y - min_corner.y, 0.05)
    camera_data = bpy.data.cameras.new("camera_robot_preview_data")
    camera = bpy.data.objects.new("camera_robot_preview", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = span * 1.25
    camera.location = center + Vector((0.0, -(span + depth) * 1.8, span * 0.08))
    camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera["camera_role"] = "robot_preview_side_full_arm"
    return camera


def choose_camera(role: str):
    if role == "robot_preview":
        camera = ensure_robot_preview_camera()
        bpy.context.scene.camera = camera
        return camera
    names = ["camera_input", "Camera"] if role == "input" else ["camera_robot_preview", "RobotPreviewCamera"]
    for name in names:
        camera = bpy.data.objects.get(name)
        if camera and camera.type == "CAMERA":
            bpy.context.scene.camera = camera
            return camera
    raise RuntimeError(f"camera role {role!r} not found")


def main():
    args = parse_args()
    scene = bpy.context.scene
    if args.validate_stage:
        roles = {obj.get("role") for obj in scene.objects if obj.type == "MESH"}
        required = {"robot"} if args.validate_stage == "robot_only" else {"environment"}
        if args.validate_stage == "composed":
            required.add("robot")
        if not required.issubset(roles):
            raise RuntimeError(f"stage missing mesh roles: {required - roles}")
        camera_name = "camera_robot_preview" if args.validate_stage == "robot_only" else "camera_input"
        camera = bpy.data.objects.get(camera_name)
        if camera is None or camera.type != "CAMERA":
            raise RuntimeError(f"stage missing camera: {camera_name}")
        if args.validate_stage == "composed":
            if scene.frame_end <= scene.frame_start or not any(
                obj.animation_data and (obj.animation_data.action or obj.animation_data.nla_tracks)
                for obj in scene.objects
            ):
                raise RuntimeError("composed stage missing animation")
            if scene.render.fps / scene.render.fps_base < 30:
                raise RuntimeError("composed animation must be at least 30FPS")
    choose_camera(args.camera_role)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    scene.render.resolution_percentage = 100
    if args.fps is not None:
        if not math.isfinite(args.fps) or args.fps <= 0:
            raise ValueError("fps must be positive and finite")
        scene.render.fps = max(1, int(round(args.fps)))
        scene.render.fps_base = scene.render.fps / args.fps
    if args.resolution:
        if any(value < 2 or value % 2 for value in args.resolution):
            raise ValueError("resolution must contain positive even dimensions")
        scene.render.resolution_x, scene.render.resolution_y = args.resolution
    elif args.fast_white_model:
        width, height = scene.render.resolution_x, scene.render.resolution_y
        scale = max(1.0, 720 / min(width, height))
        scene.render.resolution_x = math.ceil(width * scale / 2) * 2
        scene.render.resolution_y = math.ceil(height * scale / 2) * 2
    if args.samples:
        if hasattr(scene, "eevee") and hasattr(scene.eevee, "taa_render_samples"):
            scene.eevee.taa_render_samples = args.samples
        if hasattr(scene, "cycles") and hasattr(scene.cycles, "samples"):
            scene.cycles.samples = args.samples
    if args.fast_white_model:
        # 后台模式的 RNA 枚举可能缺少已注册引擎，因此直接赋值。
        try:
            scene.render.engine = "BLENDER_WORKBENCH"
        except (TypeError, ValueError, RuntimeError) as exc:
            raise RuntimeError("Unable to enable Workbench for white-model rendering. Stopped to avoid rendering the original materials.") from exc
        shading = scene.display.shading
        shading.light = "STUDIO"
        shading.color_type = "SINGLE"
        shading.single_color = (0.8, 0.8, 0.8)
        shading.show_shadows = True
        shading.show_cavity = True
        shading.cavity_type = "WORLD"
        # 关闭工程自带的合成和视频序列，保证输出来自白模渲染。
        scene.render.use_compositing = False
        scene.render.use_sequencer = False
    if args.mode == "frame":
        print(f"CWM_RENDER_START mode=frame frame={args.frame} output={output}", flush=True)
        scene.frame_set(args.frame)
        scene.render.image_settings.file_format = "PNG"
        scene.render.filepath = str(output)
        bpy.ops.render.render(write_still=True)
    else:
        if args.frame_end is not None:
            scene.frame_end = args.frame_end
        if scene.frame_end < scene.frame_start:
            raise ValueError("animation end precedes its start")
        scene.frame_step = 1
        # Blender 未编译 FFmpeg 支持时，先渲染 PNG 序列再编码。
        ffmpeg_supported = True
        try:
            scene.render.image_settings.file_format = "FFMPEG"
        except (TypeError, ValueError):
            ffmpeg_supported = False
        if ffmpeg_supported:
            print(f"CWM_RENDER_START mode=animation frames={scene.frame_start}-{scene.frame_end} output={output}", flush=True)
            scene.render.ffmpeg.format = "MPEG4"
            scene.render.ffmpeg.codec = "H264"
            scene.render.filepath = str(output)
            bpy.ops.render.render(animation=True)
        else:
            frames_dir = output.with_name(output.stem + "_frames")
            frames_dir.mkdir(parents=True, exist_ok=True)
            print(f"CWM_RENDER_START mode=png_sequence frames={scene.frame_start}-{scene.frame_end} directory={frames_dir}", flush=True)
            scene.render.image_settings.file_format = "PNG"
            scene.render.filepath = str(frames_dir / "frame_####")
            bpy.ops.render.render(animation=True)
            ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("Blender has no FFmpeg support and ffmpeg was not found on PATH; PNG frames remain in " + str(frames_dir))
            if not Path(ffmpeg).is_file() and not shutil.which(ffmpeg):
                raise FileNotFoundError(f"FFmpeg executable not found: {ffmpeg}")
            command = [ffmpeg, "-y", "-framerate", str(scene.render.fps / scene.render.fps_base), "-start_number", str(scene.frame_start), "-i", str(frames_dir / "frame_%04d.png"), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)]
            print(f"CWM_ENCODE_START encoder={ffmpeg} output={output}", flush=True)
            completed = subprocess.run(command)
            if completed.returncode != 0:
                raise RuntimeError(f"ffmpeg encoding failed with exit code {completed.returncode}: {output}")
    print(f"CWM_RENDER_OK output={output}", flush=True)


if __name__ == "__main__":
    main()
