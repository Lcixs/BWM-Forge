# Quick review before user feedback

${common_prompt}

Review the saved result of stage ${stage}, round ${number}, before the host presents it to the user. This is one bounded check of the current round, not a new modeling stage or user acceptance.

Current project (the only blend you may modify): ${blend}
Current host-rendered frame-1 preview: ${preview}
Reference first frame: ${reference}
The attached images are the reference first frame, the current preview, then any user feedback images, in that order.
Accepted upstream projects (read-only): ${upstream}
Latest user revision feedback: ${feedback}
Scripts directory: ${scripts_dir}
Evidence directory: ${evidence_dir}
Existing round report to update: ${report_path}
Host preview command (JSON argument list): ${preview_command}

## Scope by stage

For an explicit camera-view revision, the requested view takes priority over original-frame matching. Check framing and visibility from that new view; preserve the saved scene, robot pose, object transforms and animation. Report pre-existing geometry or collision issues for user feedback rather than changing them during a camera-only revision. Do not restore the original camera. When feedback is incremental, retain the view already established in the previous composed project.

- `scene_only`: compare the preview with the reference for aspect ratio, crop, perspective, main outlines, object positions, depth order, and occlusion. Check obvious unintended intersections, missing support, and task-object geometry. The robot must remain absent. Do not move scene objects simply to hide a camera error.
- `robot_only`: check model completeness, link connections, legal default joint states, obvious intersections between nonadjacent links, and whether the inspection camera shows the complete model. Use URDF hierarchy and geometry as authority. Do not align to the task image, add a scene, infer task placement, or create animation. Keep the model reusable at its asset origin.
- `composed`: compare frame 1 with the reference for camera alignment, robot heading and pose, relative positions, and occlusion. Check robot self-intersections, robot/environment penetration, and plausible task contact and support. Inspect a few animation samples as described below; frame 1 alone cannot establish motion clearance.

Distinguish intended neighboring-link overlap and task contact from visible penetration through solid surfaces. Use saved geometry and transforms where needed; bounding-box overlap alone does not prove penetration, and a clear single view does not prove collision-free geometry. Do not invent precision or claim formal collision validation.

For `composed`, open the saved project to read its animation interval and inspect at most three additional frames: midpoint, final frame, and one task-relevant contact or transition frame if known. Use the supplied preview command with a different `--output` under the evidence directory and an added `--frame N` argument to render these diagnostic frames, then open and inspect the images with the available image tool. Keep the same camera, aspect ratio, and white-model settings. Do not render a full video. For video input, distinguish observed source behavior from inferred target motion; do not force a different target anatomy to match the source actor.

## One review and one small correction pass

1. Inspect the current preview and saved project, focusing on clear defects within this stage's scope and the latest user feedback. Keep uncertain issues explicit.
2. If there is a clear, locally correctable defect, make one focused correction pass in the current project. Preserve URDF structure, link dimensions, original resources, accepted upstream projects, completed rounds, and the asset-library cache. Do not rebuild the stage, change task requirements, or advance the workflow. Leave broad or uncertain changes for user feedback.
3. Save inspection scripts as `review_check_001.py` and any correction as `review_fix_001.py` in the scripts directory. Use new numbered filenames for execution fixes and do not overwrite earlier scripts. Run scripts with the supplied Blender executable and capture execution logs in the evidence directory. If you edit the project, save it to the exact current project path, reopen it, and check the affected view or animation sample. Do not start another correction cycle; record any remaining issue.
4. If no clear defect is found, leave the blend unchanged. Never rewrite a project merely to record a successful review. The host-managed preview file is read-only; put all diagnostic images in the evidence directory. After a project change, the host will validate and regenerate the user preview.
5. Read and update the existing round report, preserving its changes, checks, and limitations. Append checks actually performed, any corrections, sampled frame numbers, and unresolved issues. Create the same report structure if it is missing. Keep the final response brief and in English: findings, changes made or no changes, and remaining limitations. User acceptance remains pending.

## Time constraint: 
Please aim to complete this review within 4 minutes. A slight extension is acceptable for highly complex scenes, but strive to finish as efficiently as possible.

The following current-stage rules also apply:
<stage_rules>
${policy}
</stage_rules>
