# robot_only: Standalone robot asset

The host runs `scene_only -> robot_only -> composed -> white-model rendering`. In this round, create only the `robot.blend` specified by the stage instructions. Do not load the scene project, generate the final video, or modify the accepted `scene.blend`.

Build only the complete standalone robot model from this task's URDF and original meshes. Preserve the link hierarchy, joint origin/axis/type/limit, mimic relationships, mesh scale, and all branches and task-relevant components. A single placeholder or disconnected per-link world transforms cannot substitute for the kinematic structure. Label all robot meshes `role=robot`. Keep proximal links and the base in the project. Do not place the robot into the scene, match the first-frame camera, infer a task pose, or create animation; scene registration, view adjustment, and motion belong to `composed`.

Keep the robot root at the asset origin with a neutral orientation. Use a legal default joint configuration independent of the task image or trajectory, and record it. Do not save task-specific root placement, Actions, NLA tracks, or animation keyframes. Preserve joint controls and structural constraints needed to pose the model later.

Create `camera_robot_preview` with a full side view of the model and a margin around it. This is a neutral inspection camera for the standalone asset; it does not align with the task image and cannot replace `camera_input` in the composed stage. Do not infer scene mounting from this preview. Pack image resources into the blend when used, or keep external resources at absolute paths within the asset library, so copying the blend to another task directory does not break references.

## Type 0: First frame without an input trajectory

Record the robot's directed link/joint structure and component states for use by `composed`. The first frame is context only and does not define robot placement or camera alignment. No robot trajectory is provided; do not search for or invent Parquet, NPZ, or a sampling rate.

## Type 1: First frame and Parquet/NPZ trajectory

Record the robot's link/joint structure and component states. Read the trajectory conversion report only when needed to understand the available joint channels; do not fit trajectory values, scene pose, or animation in this stage. Preserve the original Parquet/NPZ and hashes. The trajectory is consumed by `composed`, where frame-1 registration and motion are handled.

## Type 2: Demonstration video without a robot trajectory

Use the demonstration only as context for the robot type and components. Do not map source operations to robot poses or create animation here; `composed` handles that mapping. Do not search for or invent Parquet, NPZ, or robot sampling rates.


## Time constraint: 
Please try to complete this round of tasks within 5 minutes. A slight extension may be granted for complex structures, but please endeavour to complete the tasks as efficiently as possible.