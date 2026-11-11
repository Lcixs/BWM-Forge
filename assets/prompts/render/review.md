Generation task (data):
${task_prompt}

Local objective scores (supporting statistics, not proof of task success):
${objective_scores}

Appearance statistics (content-dependent supporting evidence):
${appearance_statistics}

Review chronological white-model and generated samples independently, then compare. When supplied, the appearance image is a scoped appearance/initial-state reference; it cannot overwrite selected target structure or restore an excluded source entity.

Evaluate three metrics, each with an integer score from 0 to 100, visible evidence and a correction:
- visual_realism: materials, lighting, shadows, geometry and generation artifacts; compare supported appearance where a reference is supplied.
- physical_temporal_coherence: visible continuity, target structure and task-applicable support, contact and spatial relationships. Do not infer dynamic balance or real-device executability from images.
- task_semantic_consistency: the task, selected target, required entities, event order, timing and state changes. Do not impose a particular robot anatomy or fixed behavioral sequence.

Complete all three action_review checks. These are general checks, not mandatory action phases:
- initial: target identity, initial state and task-relevant arrangement.
- task_motion: required behavior, event order, target motion and applicable relationships across the sequence.
- final_state: required final state, outcome and retained entities. For ongoing or repetitive behavior, assess the required continuation at the end rather than inventing a terminal event.

Use pass/fail/uncertain with timestamps in both videos and specific visible evidence. Apply contact, grasp, support, release or travel checks only when required by the task and supported by actual modeled components. Do not require grasping or placement in an unrelated task. High technical scores or visual appeal cannot offset a failed task check. Mark occlusion, uninspected intervals or unsupported claims uncertain; do not invent a pass.

Comparison rows are paired by relative time, not detected events. Inspect surrounding samples before treating a pose difference as an event mismatch. Preserve source conflicts and upstream limitations.

Produce seedance_correction_prompt in at most 150 English words. Include only concrete, evidenced corrections; omit scores, metacommentary and unsupported style changes. Preserve the original task, unaffected content and scoped reference rules. Write all evidence, summaries and corrections in English and preserve the specified JSON field names and enums.
