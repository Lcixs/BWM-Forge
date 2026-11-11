${common_prompt}
Current stage: ${stage}; round: ${number}. Complete only this round.
Accepted upstream projects (read-only): ${upstream}
Previous completed project for this stage (read-only): ${previous_blend}
Previous completed preview for this stage (read-only): ${previous_preview}
User revision feedback: ${feedback}
Only project output for this round: ${blend}
Round directory: ${folder}
Scripts directory (absolute path): ${scripts_dir}
Evidence and execution logs directory (absolute path): ${evidence_dir}
Full report path: ${report_path}
The current working directory is not the round directory. Do not resolve relative scripts/ paths to the top-level blender/scripts/ directory.
Save the first script as ${first_script}; save revisions as build_002.py and subsequent numbered files.
Argument list for the first execution: ${execution_args}
For subsequent executions, replace only the script path. Save stdout/stderr to this round's evidence/ directory, inspect tracebacks, and verify that the project file was created.
Use a new filename for each script revision. Preserve every script version executed during this round; do not overwrite earlier scripts.
If a previous-round project exists, edit a copy. Do not modify any completed-round or upstream project.
Do not reuse partial outputs from interrupted rounds. Restart from the previous completed round or the original inputs.
scene_only builds only the scene and camera_input; robot_only builds only the complete URDF model and camera_robot_preview (full-model side view).
composed combines copies of upstream projects, establishes a consistent metric scale, aligns the reference-frame pose, and creates task-appropriate animation.
Resolve scene scale, root placement and legal joint motion in the composed copy. Preserve target link dimensions and mesh scales; do not overwrite standalone projects.
Prioritize reference-frame consistency and plausible interaction; an unseen base may remain unsupported.
Preserve the reference aspect ratio for scene and composed previews, with a default minimum short side of 720 pixels. Show the complete robot in side view for its preview.
Set each mesh role to environment, task_object, or robot. Actually run Blender and save the project.
Do not publish final.blend or generate the final video. The host handles project validation, previews, and final rendering.
Disable Blender automatic backups (bpy.context.preferences.filepaths.save_version=0).
Write exactly one report.json for this round, with English prose and this structure:
{"changes":[{"reason":"reason","modification":"specific changes and parameters","result":"result",
"checks":["checks actually performed"],"unresolved":["unresolved issues"]}],
"checks":["checks for this round"],"limitations":["assumptions and unverified items"]}
Do not duplicate host-managed records such as stage_summary, session_summary, or artifacts.
Store detailed geometry and validation evidence in evidence/ and reference it in the report. Do not include entire numeric arrays in the report.
Do not generate cross-task experience; the host summarizes it after the stage ends.
<stage_rules>
${policy}
</stage_rules>${experience_text}
