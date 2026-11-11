import math
from pathlib import Path
import zipfile

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from config import (INPUT_MAX_MOTION_BYTES, INPUT_MOTION_MAX_FRAMES,
                    INPUT_MOTION_MAX_METADATA_BYTES, INPUT_MOTION_MAX_DATA_BYTES,
                    INPUT_MOTION_BATCH_SIZE)
from .assets.urdf_validation import MOVABLE

EEF_COLUMNS = ["x", "y", "z", "roll", "pitch", "yaw", "gripper"]


def validate_fps(fps):
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("Trajectory sampling rate fps must be a finite positive number in Hz.")
    return float(fps)


def trajectory_columns(action_space, joint_names):
    if action_space not in ("joint", "eef"):
        raise ValueError("Action space must be joint or eef.")
    if action_space == "eef":
        return EEF_COLUMNS.copy()
    if (not isinstance(joint_names, list) or not joint_names
            or any(not isinstance(n, str) or not n.strip() for n in joint_names)
            or len(joint_names) != len(set(joint_names))):
        raise ValueError("Provide an ordered list of unique URDF joint names for the trajectory columns.")
    return joint_names.copy()


def _check_shape(shape, width):
    if (len(shape) != 2 or shape[1] != width
            or not 1 <= shape[0] <= INPUT_MOTION_MAX_FRAMES):
        raise ValueError(f"Trajectory shape must be (T, {width}), with T between 1 and {INPUT_MOTION_MAX_FRAMES}.")
    if math.prod(shape) * 8 > INPUT_MOTION_MAX_DATA_BYTES:
        raise ValueError("Trajectory numeric data exceeds the memory budget.")


def read_trajectory(path, *, action_space, joint_names):
    path = Path(path).resolve()
    width = len(trajectory_columns(action_space, joint_names))
    suffix = path.suffix.lower()
    if (suffix not in {".npz", ".parquet"} or not path.is_file()
            or not 0 < path.stat().st_size <= INPUT_MAX_MOTION_BYTES):
        raise ValueError("The trajectory must be a non-empty Parquet or NPZ file within the size limit.")
    try:
        if suffix == ".npz":
            with zipfile.ZipFile(path) as archive:
                members = [member for member in archive.infolist() if member.filename == "action.npy"]
                if len(members) != 1:
                    raise ValueError("NPZ must contain a single action key holding a two-dimensional numeric array.")
                if members[0].file_size > INPUT_MOTION_MAX_DATA_BYTES + 10000:
                    raise ValueError("Uncompressed NPZ size exceeds the budget.")
                # 分配内存前检查数组头与解压大小，拒绝 pickle 对象数组。
                with archive.open(members[0]) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                    elif version == (2, 0):
                        shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                    else:
                        raise ValueError("Arrays within the NPZ must use NPY format version 1.0 or 2.0.")
                    _check_shape(shape, width)
                    if dtype.kind not in "iuf" or dtype.itemsize > 8:
                        raise ValueError("action must be a real-valued array; objects, booleans, complex numbers, and nested fields are not supported.")
                    if members[0].file_size - stream.tell() != math.prod(shape) * dtype.itemsize:
                        raise ValueError("NPZ array data length does not match its declared shape.")
                with np.load(path, allow_pickle=False) as data:
                    values = data["action"].astype(np.float64)
        else:
            with pq.ParquetFile(path, memory_map=False, pre_buffer=False,
                                arrow_extensions_enabled=False,
                                thrift_string_size_limit=INPUT_MOTION_MAX_METADATA_BYTES,
                                thrift_container_size_limit=INPUT_MOTION_MAX_FRAMES) as parquet:
                if parquet.schema_arrow.names.count("action") != 1:
                    raise ValueError("Parquet must contain a single action column with one action vector per row.")
                _check_shape((parquet.metadata.num_rows, width), width)
                dtype = parquet.schema_arrow.field("action").type
                if not (pa.types.is_list(dtype) or pa.types.is_large_list(dtype) or pa.types.is_fixed_size_list(dtype)):
                    raise ValueError("Each Parquet action row must be a numeric vector; scalars and dictionaries are not supported.")
                if not (pa.types.is_integer(dtype.value_type) or pa.types.is_floating(dtype.value_type)):
                    raise ValueError("Parquet action vector elements must be real numbers; booleans and nested fields are not supported.")
                if pa.types.is_fixed_size_list(dtype) and dtype.list_size != width:
                    raise ValueError(f"Each action row must contain {width} numeric values.")
                leaves = [i for i in range(parquet.metadata.num_columns)
                          if parquet.schema.column(i).path.startswith("action.")]
                blocks = [parquet.metadata.row_group(g).column(i)
                          for g in range(parquet.metadata.num_row_groups)
                          for i in leaves]
                if any(b.file_path for b in blocks) or sum(b.total_uncompressed_size for b in blocks) > INPUT_MOTION_MAX_DATA_BYTES:
                    raise ValueError("Parquet references external files or exceeds the uncompressed size budget.")
                values = np.empty((parquet.metadata.num_rows, width), dtype=np.float64)
                offset = 0
                for batch in parquet.iter_batches(batch_size=INPUT_MOTION_BATCH_SIZE, columns=["action"], use_threads=False):
                    if batch.schema.names != ["action"]:
                        raise ValueError("The Parquet action field contains overlapping column names.")
                    column = batch.column(0)
                    if column.null_count:
                        raise ValueError("action contains missing rows.")
                    if np.any(pc.list_value_length(column).to_numpy(zero_copy_only=False) != width):
                        raise ValueError(f"Each action row must contain {width} numeric values.")
                    flat = column.flatten()
                    if flat.null_count:
                        raise ValueError("action vectors contain missing values.")
                    values[offset:offset+batch.num_rows] = flat.to_numpy(zero_copy_only=False).reshape(-1, width)
                    offset += batch.num_rows
                if offset != len(values):
                    raise ValueError("The actual Parquet row count does not match the declared count.")
    except (pa.ArrowException, OSError, EOFError, zipfile.BadZipFile) as exc:
        raise ValueError("Unable to read the trajectory file. Check its format and integrity.") from exc
    if not np.isfinite(values).all():
        raise ValueError("The trajectory must not contain NaN or Inf.")
    return values


def check_action(path, asset, *, action_space, fps, joint_names=None):
    fps = validate_fps(fps)
    if not isinstance(asset, dict) or asset.get("passed") is not True:
        raise ValueError("Validate the robot asset first.")
    is_arm = asset.get("embodiment") == "robot_arm"
    if is_arm and not asset.get("arm_joint_names"):
        raise ValueError("Validate the robot arm chain first.")
    columns = trajectory_columns(action_space, joint_names)
    if action_space == "eef":
        if not is_arm:
            raise ValueError("EEF trajectories are supported only for robot_arm; use joint for other embodiments.")
        if joint_names is not None:
            raise ValueError("joint_names applies only to joint trajectories.")
    else:
        # joint 模式的夹爪位置也须显式列入 joint_names。
        independent = {name for name, j in asset["joints"].items()
                       if j["type"] in MOVABLE and j["mimic"] is None}
        if any(name not in independent for name in joint_names):
            raise ValueError("joint_names must reference independent revolute/continuous/prismatic URDF joints.")
    values = read_trajectory(path, action_space=action_space, joint_names=joint_names)
    duration = (len(values) - 1) / fps
    if not math.isfinite(duration):
        raise ValueError("The trajectory time range is not finite. Check fps.")
    motion = {
        "action_space": action_space, "fps": fps, "columns": columns,
        "joint_names": joint_names.copy() if action_space == "joint" else asset["arm_joint_names"].copy(),
    }
    if action_space == "joint":
        motion["joint_units"] = ["m" if asset["joints"][name]["type"] == "prismatic" else "rad"
                                 for name in joint_names]
        motion["unprovided_joint_names"] = sorted(independent - set(joint_names))
    else:
        motion.update({key: asset[key] for key in ("base_frame", "tool_frame", "tcp_xyz", "tcp_rpy")})
        motion["gripper_joint_names"] = asset["gripper_joint_names"]
    return {"passed": True, "trajectory": str(Path(path).resolve()),
            "asset_name": asset["asset_name"], "model_name": asset["model_name"],
            "motion": motion, "shape": list(values.shape), "duration_seconds": duration,
            "preview": values[:5].tolist()}
