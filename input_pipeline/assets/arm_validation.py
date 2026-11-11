import math

from .urdf_validation import MOVABLE, joint_chain


def validate_arm(joints, root_link, *, base_frame=None, tool_frame=None,
                 tcp_xyz=None, tcp_rpy=None):
    links = {root_link} | {j["child"] for j in joints.values()}
    paths = {link: joint_chain(joints, root_link, link) for link in links}
    moving = [j for j in joints.values() if j["type"] != "fixed"]
    if not moving:
        raise ValueError("A robot arm must contain movable joints.")
    if tool_frame is None:
        # 主链比较只考虑活动末端，固定相机等旁支不参与。
        tips = [j["child"] for j in moving if not any(
            j in paths[other["child"]][:-1] for other in moving)]
        common = paths[tips[0]].copy()
        for tip in tips[1:]:
            path = paths[tip]
            length = 0
            for left, right in zip(common, path):
                if left["name"] != right["name"]:
                    break
                length += 1
            common = common[:length]
        tool_frame = common[-1]["child"] if common else root_link
    if not isinstance(tool_frame, str) or tool_frame not in links:
        raise ValueError("tool_frame must be a URDF link.")
    upstream = paths[tool_frame]
    if base_frame is None:
        base_frame = next((j["parent"] for j in upstream if j["type"] != "fixed"), root_link)
    if not isinstance(base_frame, str) or base_frame not in links:
        raise ValueError("base_frame must be a URDF link.")
    chain = joint_chain(joints, base_frame, tool_frame)
    arm = [j["name"] for j in chain if j["type"] in MOVABLE and j["mimic"] is None]
    if (not arm or any(j["type"] not in MOVABLE | {"fixed"} for j in chain)
            or any(j["mimic"] and j["driver"] not in arm for j in chain)):
        raise ValueError("Cannot identify a scalar arm chain; specify base_frame and tool_frame explicitly.")

    downstream = {name: j for name, j in joints.items()
                  if any(step["parent"] == tool_frame for step in paths[j["child"]])
                  and j["type"] != "fixed"}
    for name, joint in downstream.items():
        if joint["type"] not in MOVABLE:
            raise ValueError("Arm tool mechanisms must use scalar joints: " + name)
        if joint["mimic"] and (joint["driver"] not in downstream or joint["driver_multiplier"] == 0):
            raise ValueError("Tool mimic joints must have a nonzero drive within the tool mechanism: " + name)
        limits = joint["limits"]
        if joint["type"] in {"revolute", "prismatic"} and limits["lower"] >= limits["upper"]:
            raise ValueError("Tool joint has no usable motion range: " + name)
    tcp = {}
    for key, values in (("tcp_xyz", tcp_xyz), ("tcp_rpy", tcp_rpy)):
        values = [0., 0., 0.] if values is None else values
        if (not isinstance(values, (list, tuple)) or len(values) != 3
                or any(type(v) not in {int, float} or not math.isfinite(v) for v in values)):
            raise ValueError(key + " requires three finite real numbers.")
        tcp[key] = list(values)
    return {"arm_joint_names": arm,
            "gripper_joint_names": [name for name, j in downstream.items() if j["mimic"] is None],
            "base_frame": base_frame, "tool_frame": tool_frame, **tcp}
