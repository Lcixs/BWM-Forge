# composed: Reference-frame registration, task animation, and white-model handoff

The host runs `scene_only -> robot_only -> composed`. Compose and animate only in the `composed.blend` specified for this round. Do not overwrite accepted scene or robot projects, publish `final.blend`, or render the final MP4. After automatic review, the host renders a complete white-model video for this composed round in a separate Blender process. The user reviews that video for scene and motion feedback. On acceptance, the host publishes the accepted composition and reuses its video without rendering again.

For revisions, edit a copy of the previous composed project instead of merging upstream projects again. If the user explicitly requests a new viewpoint, that request overrides the original-frame camera matching below: change only the camera and retain the saved geometry, robot pose, object placement, animation and timing unless other changes are requested. Evaluate the requested framing and visibility, not similarity to the old view.

Read the accepted `scene.blend` and `robot.blend` and merge them once in a composed copy. Preserve the reference composition of scene objects and `camera_input`. Use a consistent metric world for the scene, robot, and motion while preserving URDF mesh scales, hierarchy, and legal joints. Prefer evidence-based, continuous scale/pose/joint corrections. Do not fake alignment through mirroring, stretching target links, hidden meshes, disconnected per-link movement, or moving a camera or object contrary to the task. An unseen base may remain unsupported; unknown mounting structures must not dictate the visible pose.

The robot project may be a reusable asset from an earlier task. Its default placement, joint configuration, and `camera_robot_preview` are only for model inspection. In this composed copy, establish the task-specific robot root position/orientation, initial joint states, and motion. Use the scene's `camera_input` as the starting view and refine its view here only when reference evidence requires it; do not inherit the robot preview camera as the task view. Keep accepted standalone projects and the asset-library cache unchanged.

Frame 1 must reproduce the reference composition when evaluated from the final Action/F-Curves, not merely before animation is assigned. Check main object outlines, pairwise positions, depth and occlusion, overall robot orientation, the directed link/joint relationships and task-relevant component states, and task state. An unchanged camera matrix, matching bounding boxes, or a single component alignment cannot replace these checks. Correct composition assumptions if the target enters from the wrong side, appears mirrored, or passes through walls. Report scene geometry errors that cannot reasonably be resolved in the composed copy; do not silently edit accepted upstream projects. Preserve the reference aspect ratio and set a valid animation interval at a minimum of 30 FPS.

Keep the host-rendered white-model video at or under 30 seconds. Set the saved scene's frame range and effective FPS so `(frame_end - frame_start + 1) / (render.fps / render.fps_base) <= 30`. Include the complete task-relevant action within that interval; do not meet the limit by silently dropping required transitions or changing the timing of supplied motion. If the full supplied motion cannot fit, document the selected segment or unresolved timing conflict in this round's evidence.

Build only task-applicable states and transitions using actual modeled mechanisms. Coordinate world-root translation/orientation and legal joint motion when both are required. Maintain applicable support, contact and clearance. Do not impose a fixed behavioral sequence or invent missing components. An attachment constraint alone is not contact evidence. Apply motion evidence according to input type below.

## Type 0: Infer motion from the first frame

Infer root placement and initial joint states from directed link/joint relationships, target heading, crop boundaries, occlusion and task-relevant scene relationships. No trajectory is supplied. Design explainable subsequent motion from the task description and static scene, record assumptions, and do not call it a measured robot trajectory.

## Type 1: Use the first frame and trajectory

Register the initial configuration using the visible robot chain, task objects, and target region. Preserve the original Parquet/NPZ, conversion report, and hashes. Treat the trajectory as a low-weight motion reference: first-frame correspondence, URDF legality, task contact/support, and continuous motion take priority. Record which segments were used, retargeted, or ignored and why. Do not describe interpolated frames as original samples.

## Type 2: Map demonstrations to the robot

Inspect the reference frame and other key moments for visible action order, contact, and object-state changes. Map observed behavior to kinematically valid robot motion and document the basis for the mapping. If the target robot is absent, infer its initial configuration and mounting position only from the scene and task. Frame 1 must match the scene and observable task state; do not claim the video validated an unseen robot pose. No robot trajectory is supplied, so do not call generated motion measured.

## Common checks and handoff

Evaluate the saved and reopened animation at the first frame, task-relevant transitions, motion extrema and final frame, with sparse samples elsewhere. Default to no more than 24 total motion samples, adding local samples in risky intervals. Check joint limits, abnormal velocity/acceleration, task-applicable contact points and normals, support, obstacle clearance, and swept paths between adjacent samples. Only designed adjacent-link seams and intended task contact/support patches may have bounded shallow local intersection. Robot bodies crossing a wall or barrier's full thickness, entering closed objects, penetrating collision cores, or tunneling between samples are not minor errors. Do not judge penetration from IoU, bounding boxes, overlap volume, or nearest distance alone. Mark unproven results unknown and explicitly state that no formal continuous-collision proof was obtained.

Verify that task-object meshes remain consistent with the scene stage and carry forward their evidence classifications. If identity, task-relevant surfaces, or target containment/support still depend on speculative proxies, describe contact and completion only as assumption-based diagnostics, not validation of the real task. Store reference alignment, motion provenance, keyframe/contact/collision checks, and unresolved issues in this round's `evidence/`. Add supporting first-frame or contact images there when feasible. Save this round's `composed.blend` for host preview rendering and feedback.

## Time constraint: 
Please try to complete this round of tasks within 10 minutes. A slight extension is permitted for complex movements, but please endeavour to complete them as efficiently as possible.
