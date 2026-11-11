# scene_only: Scene and reference-frame camera

The host runs `scene_only -> robot_only -> composed -> white-model rendering`. In this round, create only the `scene.blend` specified by the stage instructions. The host renders a preview in a separate Blender process and collects feedback. Do not generate the final video or overwrite accepted projects.

Use the reference frame, task description, and available scene supplement in the common prompt to build a standalone scene: table surfaces, walls, supports, task objects, target regions, and other identifiable geometry. Preserve visible outlines, proportions, orientations, thickness, cavities, occlusion, and mutual support. The white model may omit textures and microscopic material detail. Leave the robot to `robot_only`; do not load robot meshes in this stage. Label every scene mesh `role=environment` or `role=task_object`.

Create `camera_input` so frame 1 matches the reference aspect ratio, crop, perspective, main object outlines, and relative positions. Do not force a square reference into 16:9 or claim alignment based only on a camera name or unchanged matrix. Compare object centers, principal axes, key outlines, pairwise directed offsets, depth order, and occlusion. Fix overall composition and task objects/target regions before local detail. Preserve the reference aspect ratio in render settings; the host handles preview rendering.

Distinguish visible evidence, occlusion completion constrained by boundaries/symmetry/support, and text-only inferences. If identity, task-relevant surfaces, target openings, or support remain uncertain, use clearly labeled diagnostic proxies without presenting them as measured geometry. Record key assumptions, unknown fields, and task-object mesh identities in this round's `evidence/` for `composed`. Do not describe unknown dimensions, camera calibration, or collision ground truth as measured.

## Type 0: First frame and task description

Use the input first frame directly to establish the initial scene and `camera_input`. Task text may help identify manipulated objects and targets, but cannot replace visible geometry. No video timeline or trajectory is available to establish additional scene facts.

## Type 1: First frame, task description, and trajectory

Use the input first frame directly to establish the initial scene and `camera_input`. A trajectory is motion evidence only; its positions must not override visible scene geometry. Do not process Parquet conversion outputs in this stage.

## Type 2: Demonstration video

Use the extracted first frame as the `camera_input` reference. Also inspect other key moments to identify objects, environment layout, changes in occlusion, and target states. Record observation timestamps. Determine retained, replaced or excluded source entities from the task. Exclude an entity only when the task requires its replacement or exclusion; do not import the selected target in the scene stage.

## Time constraint: 
Please aim to complete this round within 8 minutes. A slight extension is acceptable for highly complex scenes, but strive to finish as efficiently as possible.