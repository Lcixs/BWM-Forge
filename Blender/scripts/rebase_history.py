from pathlib import Path
import shutil
import sys

import bpy


def main():
    source, destination, original_blend = [Path(value).resolve() for value in sys.argv[sys.argv.index("--") + 1:]]
    project = bpy.data.filepath
    # Blender 的 // 路径相对于源 blend 所在目录。
    for collection in (bpy.data.images, bpy.data.libraries, bpy.data.movieclips,
                       bpy.data.sounds, bpy.data.fonts, bpy.data.volumes, bpy.data.cache_files):
        for item in collection:
            value = getattr(item, "filepath", "")
            if not value or value.startswith("<"):
                continue
            path = Path(bpy.path.abspath(value, start=str(original_blend.parent))).resolve()
            if path.is_relative_to(source):
                copied = destination / path.relative_to(source)
                if path.is_file() and not copied.exists():
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, copied)
                item.filepath = str(copied)
            elif value.startswith("//"):
                # 任务外的共享资产改用绝对路径，避免随副本位置变化。
                item.filepath = str(path)
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=project)


if __name__ == "__main__":
    main()
