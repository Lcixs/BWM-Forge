# Blender workflow: Revision feedback

Use this file only for the next modeling round after the user selects `revise` on a `scene_only`, `robot_only`, or `composed` preview. The host supplies feedback text, reference images, the previous project for the current stage, accepted upstream projects, and this round's output paths. Work at those exact paths. The host advances on `accept` and saves progress and pauses on `quit`; do not advance stages yourself.

Separate feedback into observable facts, requested changes, necessary inferences, and remaining unknowns. Compare the current preview with task references and identify objects or image regions to change and accepted upstream content to preserve. Additional images may clarify shapes and components. Unless the user explicitly supplies camera calibration or requests camera changes, they cannot override original composition, scale, and occlusion evidence. Keep uncertain identity, dimensions, task-relevant surfaces, and support labeled as inferred.

- For `scene_only`, continue from the previous `scene.blend` when revising scene/task objects, targets, or camera-to-reference relationships. Change only the geometry or composition implicated by feedback and retain other verified objects and relationships.
- For `robot_only`, continue from the previous `robot.blend` when revising URDF import, links/joints, components, or the side-view preview. Do not load or overwrite the scene. Leave scene-relative robot placement to `composed`.
- For `composed`, continue from the previous `composed.blend` when revising robot orientation, scene-robot registration, motion evidence, contact, or animation. Accepted `scene.blend` and `robot.blend` are read-only. Check the directed link/joint structure, entry side, occlusion, task contact, and collisions, then recompute affected animation frames. If feedback reveals an upstream scene or robot asset error, state it explicitly instead of silently rewriting accepted upstream projects.

For a camera-only request, preserve scene geometry, robot placement and joint states, task-object transforms, animation, timing and output resolution. Adjust only the camera to the requested view. The new view overrides original-frame alignment; do not modify geometry or motion to compensate for the changed camera, and do not return to the original view during review.

## Type 0: First-frame feedback

Compare the input first frame, current preview, and new reference images. No trajectory or video evidence is available. For motion changes, record what is inferred from task text and the static scene.

## Type 1: First-frame and trajectory feedback

Compare the input first frame, current preview, and trajectory conversion report. For motion changes, record affected trajectory segments and why they were preserved, retargeted, or ignored. Do not call interpolated or modified motion original samples.

## Type 2: Video feedback

Compare the demonstration first frame, current preview, and relevant video timestamps. When changing source-to-target motion mapping, document video observations, robot kinematic constraints, and inferences. Do not call source poses observed robot joint poses.

## Final checks

Preserve original images/video, URDF, available trajectories, and accepted upstream projects. If new evidence resolves an unknown, update only supported fields and rebuild affected meshes or motion. A request to continue reviewing previews does not turn assumptions into measurements. Save revisions to this round's sole project path so the host can reopen, validate, and render a new preview. Briefly record changes, unresolved issues, and visible results the user can inspect. Previous previews or conclusions cannot replace checks for the current round.

## Time constraint: 
Please aim to complete this review within 6 minutes. A slight extension is acceptable for highly complex scenes, but strive to finish as efficiently as possible.